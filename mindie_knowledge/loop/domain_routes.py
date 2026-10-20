"""[Role] Select and explicitly register existing domain engines for native adapters.

[Invariants]
- Canonical domains match ``[a-z][a-z0-9-]{0,63}``.
- A default call with no domain and no reference returns the supplied default
  path without reading any configuration.
- A reference selects its authority domain; explicit domains must agree, and
  the destination service still validates the complete typed reference.
- A requested domain is never silently replaced by the default configuration.
- The selected configuration must be an existing absolute-or-resolved JSON
  object whose declared ``domain`` equals the requested domain.

[Timeouts & Locks]
- Local configuration reads and atomic replacement only. Explicit activation
  reuses Admission's existing WAL/5s write budget; no network or new retry loop.
- Native deactivation shares one 6s wait budget across registered stores.

[Side-Effect Policy]
- Selection is read-only. The register command changes only one mapping in an
  existing adapter JSON. Native activation requires that target's existing
  consent and scope, and binds a verified host identity through Admission.
- Never create engine configs, copy consent/tokens or infer native identity.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import time

from .documents import DOMAIN_RE

_REFERENCE_AUTHORITY = re.compile(r"mindie://(?P<domain>[a-z][a-z0-9-]{0,63})/")


def _canonical_domain(value, label):
    if not isinstance(value, str) or DOMAIN_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must match [a-z][a-z0-9-]{{0,63}}")
    return value


def _requested_domain(domain, ref):
    if ref is None:
        return None if domain is None else _canonical_domain(domain, "domain")
    match = _REFERENCE_AUTHORITY.match(ref) if isinstance(ref, str) else None
    if match is None:
        raise ValueError("reference must be a canonical mindie://DOMAIN/... URI")
    selected = match.group("domain")
    if domain is not None and _canonical_domain(domain, "domain") != selected:
        raise ValueError("explicit domain conflicts with the reference authority")
    return selected


def _selected_config(path, *, require_absolute):
    candidate = Path(path)
    if require_absolute and not candidate.is_absolute():
        raise ValueError(f"configured engine config path must be absolute: {path}")
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ValueError(f"selected engine config does not exist: {candidate}")
    try:
        with candidate.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"selected engine config is not valid JSON: {candidate}") from error
    if not isinstance(config, dict):
        raise ValueError(f"selected engine config must be a JSON object: {candidate}")
    declared = config.get("domain")
    if not isinstance(declared, str) or DOMAIN_RE.fullmatch(declared) is None:
        raise ValueError(f"selected engine config has no canonical domain: {candidate}")
    return candidate, declared


def select_engine_config(default_config_path, *, domain=None, ref=None, domain_configs=None) -> Path:
    """Return the existing engine config path for an explicit domain or reference."""
    requested = _requested_domain(domain, ref)
    default = Path(default_config_path)
    if requested is None:
        return default.resolve()
    if domain_configs is not None and not isinstance(domain_configs, Mapping):
        raise ValueError("domain_configs must be a mapping")
    if domain_configs is not None and requested in domain_configs:
        selected, declared = _selected_config(domain_configs[requested], require_absolute=True)
        if declared != requested:
            raise ValueError(
                f"selected engine config declares {declared!r}, not {requested!r}"
            )
        return selected
    selected, declared = _selected_config(default, require_absolute=False)
    if declared != requested:
        raise ValueError(f"unknown domain: {requested!r}")
    return selected


def register_domain_config(adapter_config_path, *, domain, engine_config_path):
    """Explicitly register one existing engine; this grants no activation."""
    from .cli import config_at
    from ..markdown import _atomic_write_text

    domain = _canonical_domain(domain, "domain")
    selected, declared = _selected_config(engine_config_path, require_absolute=True)
    if declared != domain:
        raise ValueError("selected engine config declares another domain")
    config_at(selected)
    adapter_path = Path(adapter_config_path).resolve(strict=True)
    adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
    if not isinstance(adapter, dict) or not isinstance(adapter.get("domain_configs", {}), dict):
        raise ValueError("adapter config and domain_configs must be JSON objects")
    mappings = dict(adapter.get("domain_configs", {}))
    mappings[domain] = str(selected)
    adapter["domain_configs"] = mappings
    _atomic_write_text(adapter_path, json.dumps(adapter, ensure_ascii=False, indent=2) + "\n")
    return dict(status="registered", domain=domain, engine_config=str(selected),
                adapter_config=str(adapter_path), activation="not_requested")


def activate_selected_domain(default_config_path, *, domain, domain_configs=None,
                             session, project_root, root_session=None):
    """Bind a selected domain after the native adapter verifies host identity.

    ``session``/``project_root``/``root_session`` come from the adapter's existing
    native binding. They are not model tool arguments. This helper has no CLI
    activation command and accepts no token from another domain.
    """
    from .activation import Admission
    from .cli import config_at
    from .settings import from_engine_config

    _canonical_domain(domain, "domain")
    selected = select_engine_config(default_config_path, domain=domain, domain_configs=domain_configs)
    config = config_at(selected)
    settings = from_engine_config(config)
    if not settings.allows_capture():
        raise ValueError("selected domain contribution is not authorized: " + settings.contribution_block_reason())
    if not settings.in_scope(project_root):
        raise ValueError("native project scope is outside the selected domain contribution authorization")
    admission_path = config.get("admission_path")
    if not isinstance(admission_path, str) or not Path(admission_path).is_absolute():
        raise ValueError("selected domain requires an explicit absolute admission_path")
    lease = Admission(admission_path).activate(session, project_root=project_root, root_session=root_session)
    return dict(domain=config["domain"], engine_config=str(selected), generation=settings.generation, lease=lease)


def deactivate_registered_domains(default_config_path, *, domain_configs=None, session, timeout=6.0):
    """Revoke this trusted native session across the adapter's existing engines.

    Disabled consent and changed project scope never prevent revocation. One
    total wait budget covers all stores; partial failures retain their domain
    identity and never masquerade as a successful global deactivation.
    """
    from .activation import Admission, AdmissionUnavailable

    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        raise ValueError("native session is required for domain deactivation")
    if domain_configs is not None and not isinstance(domain_configs, Mapping):
        raise ValueError("domain_configs must be a mapping")
    deadline = time.monotonic() + timeout
    targets = [(None, default_config_path), *list((domain_configs or {}).items())]
    outcomes, admissions = [], {}
    for position, (requested, path) in enumerate(targets):
        result = dict(domain=requested, engine_config=str(path))
        admission_identity = None
        try:
            selected, declared = _selected_config(path, require_absolute=requested is not None)
            result.update(domain=declared, engine_config=str(selected))
            if requested is not None and requested != declared:
                raise ValueError("registered engine declares another domain")
            config = json.loads(selected.read_text(encoding="utf-8"))
            admission_path = config.get("admission_path")
            if not isinstance(admission_path, str) or not Path(admission_path).is_absolute():
                raise ValueError("selected domain requires an explicit absolute admission_path")
            admission = Admission(admission_path)
            admission_identity = str(admission.path.resolve())
            if admission_identity in admissions:
                result.update(admissions[admission_identity])
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("domain deactivation exhausted its total budget")
                changed = admission.deactivate(
                    session, timeout=remaining / (len(targets) - position))
                outcome = dict(status="inactive", changed=changed)
                admissions[admission_identity] = outcome
                result.update(outcome)
        except TimeoutError as error:
            outcome = dict(status="timed_out", error_class="TimeoutError", detail=str(error))
            if admission_identity is not None:
                admissions[admission_identity] = outcome
            result.update(outcome)
        except (AdmissionUnavailable, OSError, ValueError) as error:
            outcome = dict(status="unavailable", error_class=type(error).__name__, detail=str(error))
            if admission_identity is not None:
                admissions[admission_identity] = outcome
            result.update(outcome)
        outcomes.append(result)
    return dict(status="inactive" if all(item["status"] == "inactive" for item in outcomes) else "partial",
                changed=any(item.get("changed", False) for item in outcomes), domains=outcomes)


def main(argv=None):
    """Register an explicit local mapping; native activation remains adapter-owned."""
    import argparse

    parser = argparse.ArgumentParser(description="Register an existing MindIE domain engine in an existing adapter config.")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--adapter-config", required=True)
    register.add_argument("--domain", required=True)
    register.add_argument("--engine-config", required=True)
    args = parser.parse_args(argv)
    result = register_domain_config(args.adapter_config, domain=args.domain, engine_config_path=args.engine_config)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
