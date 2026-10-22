"""[Role] Prepare the shared public-domain catalog for native installations.

[Invariants] Read feeds and contribution destinations are separate. Existing
settings and manual routes remain unchanged; new domains start with capture
disabled and refer to the profile's one consent authority.
[Timeouts & Locks] No network/model calls. Settings use the existing 5s profile
lock, stores use WAL/5s transactions; the adapter owns installation serialization.
[Side-Effect Policy] Prepare local engine files and empty stores before the
adapter publishes their mapping. Never activate sessions or write consent.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import time

from ..markdown import _atomic_write_text
from .cli import config_at, validate_config
from .documents import DOMAIN_RE
from .settings import CommunityWriteContext, check_repository
from .store import Store

SCHEMA = "mindie-domain-catalog/1"
DOMAINS = (
    "operators", "vllm-ascend", "infer-core", "infer-distributed",
    "infer-observability", "infer-ops-compat", "agent-engineering",
)
RUNTIME_KEYS = (
    "capture_mode", "transcript_adapter", "redactor_executable", "summary_command",
    "organizer_command", "worker_policy", "service_launch_context", "product_validation",
)


def catalog():
    """Semantic domains can grow independently; this is the shipped starting set."""
    return [dict(domain=domain, repository="mindie-agent/knowledge-" + domain,
                 ref="main", interval_seconds=300) for domain in DOMAINS]


def _absolute(value, label):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(label + " must be an absolute path")
    return Path(value)


def _settings(path, consent, feed):
    with CommunityWriteContext(path) as context:
        current = context.read(path)
        if current.state == "missing":
            current = context.configure(path, dict(
                enabled=False, repository=feed["repository"], branch=feed.get("ref", "main"),
                project_roots=[], idle_seconds=300, consent_config=str(consent), visibility="public"))
            return True
        if not current.valid:
            raise ValueError("existing domain community settings are unavailable; bytes preserved")
        if current.raw.get("consent_config") != str(consent):
            raise ValueError("existing domain settings refer to another consent authority; bytes preserved")
        return False


def prepare(adapter, engine, *, config_directory, authority_directory, consent_config,
            public_feeds=True, domains=None):
    """Return prepared routes; the native installer publishes them last.

    Config files created by this function carry its marker. Only those files
    inherit refreshed native worker/parser wiring on an update; explicit routes
    to separately managed engines are kept as the user registered them.
    """
    config_dir = _absolute(config_directory, "config_directory")
    authority_dir = _absolute(authority_directory, "authority_directory")
    consent = _absolute(consent_config, "consent_config")
    default_path = _absolute(adapter.get("engine_config"), "default engine_config")
    validate_config(engine)
    routes = dict(adapter.get("domain_configs") or {})
    for domain, path in routes.items():
        if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
            raise ValueError("registered domain is not canonical")
        selected_path = _absolute(path, "registered engine_config")
        if domain == engine["domain"] and selected_path == default_path:
            continue  # The adapter publishes this validated candidate last.
        selected = config_at(selected_path)
        if selected["domain"] != domain:
            raise ValueError("registered engine declares another domain")
    routes[engine["domain"]] = str(default_path)
    rows = catalog() if domains is None else domains
    if not isinstance(rows, list) or len({row["domain"] for row in rows}) != len(rows):
        raise ValueError("domain catalog must have unique domain records")
    prepared, authorities = [], []
    for feed in rows:
        domain = feed["domain"]
        if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
            raise ValueError("catalog domain is not canonical")
        check_repository(feed["repository"])
        if domain == engine["domain"]:
            continue
        old = config_at(routes[domain]) if domain in routes else None
        if old is not None and old.get("domain_catalog") != SCHEMA:
            continue
        if old is None and not public_feeds:
            continue
        if old is None:
            settings_path = authority_dir / domain / "community.json"
            created = _settings(settings_path, consent, feed)
            if created:
                authorities.append(domain)
            value = dict(root=engine["root"], domain=domain, domain_catalog=SCHEMA,
                         admission_path=str(Path(engine["root"]) / domain / "admission.sqlite3"),
                         community_config=str(settings_path), feeds=[dict(feed)])
        else:
            value = dict(old)
        for key in RUNTIME_KEYS:
            value.pop(key, None)
            if key in engine:
                value[key] = engine[key]
        validate_config(value)
        with closing(Store(value["root"], domain)) as store:
            store.db.assert_authority()
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        # An active service fingerprints its config bytes. Never rewrite those
        # bytes in place, including repair installs of the same plugin version.
        path = config_dir / hashlib.sha256(text.encode()).hexdigest()[:20] / (domain + ".engine.json")
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            _atomic_write_text(path, text)
        routes[domain] = str(path)
        prepared.append(domain)
    return dict(schema=SCHEMA, domain_configs=routes, prepared_domains=prepared,
                created_disabled_settings=authorities, consent_changed=False, activation="not_requested")


def restore_changed(report, *, timeout=5.0):
    """Restore exact secondary retirements when the adapter was not published."""
    from .lifecycle import inspect_retirement, retire_service, restore_service
    deadline = time.monotonic() + timeout
    outcomes = []
    for row in reversed(report.get('domains', [])):
        if not row.get('restore_required'):
            continue
        path = row['config_path']
        try:
            receipt = inspect_retirement(path)
            if receipt is None:
                outcomes.append(dict(domain=row['domain'], status='not-needed'))
                continue
            expected = row.get('retirement')
            if expected is None or any(
                    receipt[key] != expected[key] for key in ('operation_id', 'config_fingerprint')):
                raise ValueError('retirement ownership changed before rollback')
            if receipt is not None and receipt['status'] == 'retiring':
                # A lost local stop reply is reconciled against the same owner.
                # No task/model/publication operation is retried.
                result = retire_service(path, control_timeout=max(0, deadline - time.monotonic()))
                receipt = result.get('retirement') if result.get('idle') else None
            result = restore_service(path, receipt, control_timeout=max(0, deadline - time.monotonic()))
            outcomes.append(dict(domain=row['domain'], **result))
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            outcomes.append(dict(domain=row['domain'], status='incomplete', error_type=type(exc).__name__))
    return dict(status='incomplete' if any(row['status'] == 'incomplete' for row in outcomes) else 'restored', domains=outcomes)


def retire_changed(previous, candidate, *, timeout=30.0, include_default=False):
    """Retire changed secondary service owners before switching native routes.

    The adapter already handles its default engine. One total budget covers the
    extra engines; active work returns deferred through the existing lifecycle.
    """
    from .lifecycle import inspect_retirement, retire_service
    deadline = time.monotonic() + timeout
    retirement_deadline = deadline - min(5.0, timeout / 2)
    old_default = previous.get("engine_config")
    old, new = dict(previous.get("domain_configs", {})), dict(candidate.get("domain_configs", {}))
    if include_default and old_default:
        domain = config_at(old_default)['domain']
        old[domain], new[domain] = old_default, candidate.get('engine_config')
    outcomes, seen = [], set()
    for domain, path in old.items():
        if (path == old_default and not include_default) or path in seen or new.get(domain) == path:
            continue
        seen.add(path)
        row = dict(domain=domain, config_path=path, restore_required=True)
        outcomes.append(row)
        try:
            # The installer holds its binding lock and has verified that these
            # are still the published routes. Reconcile an earlier interrupted
            # attempt too, so another failed publication cannot strand them.
            previous_retirement = inspect_retirement(path)
            if previous_retirement is not None:
                row['retirement'] = previous_retirement
            remaining = retirement_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('domain retirement budget expired')
            row.update(retire_service(path, control_timeout=remaining))
        except TimeoutError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            row.update(idle=None, error_type=type(exc).__name__)
            if getattr(exc, 'mindie_retirement', None) is not None:
                row['retirement'] = exc.mindie_retirement
        if row.get("idle") is not True or row.get("cleanup_failed"):
            report = dict(status="deferred" if row.get("idle") is False else "incomplete", domains=outcomes)
            report['restoration'] = restore_changed(report, timeout=max(0, deadline - time.monotonic()))
            if report['restoration']['status'] != 'restored':
                report['status'] = 'incomplete'
            return report
    return dict(status="retired", domains=outcomes)


def main():
    request = json.load(sys.stdin.buffer)
    operation = request.pop("operation", "prepare")
    methods = {'prepare': prepare, 'retire': retire_changed, 'restore': restore_changed}
    if operation not in methods:
        raise ValueError("unsupported domain catalog operation")
    payload = json.dumps(methods[operation](**request), ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
