"""[Role] Build an immutable component runtime beside the active native runtime.

[Invariants] Published adapter inputs stay unchanged. A separate uv project
locks the selected component combination; only a verified candidate is returned.
Native adapters retain ownership of their existing idle switch and rollback.
A manifest published by ``os.replace`` is never removed if a later cleanup or
interrupt aborts the call; only a fresh, unlocked, unpublished generation is
discarded.
[Timeouts & Locks] The caller supplies one enclosing build budget measured from
entry: pre-build GC (<= 5 s or 10%), ``uv sync``, inventory verification and the
post-build sweep share one absolute deadline and reserve cleanup grace. A
nonblocking OS lock coalesces the same generation; process cleanup has an extra
10s allowance. A bounded pre-build GC runs before that lock and never blocks
the build.
[Side-Effect Policy] Writes only under the caller's runtime-generation directory
and a same-volume uv cache; reclaims only disposable sibling generations. No
knowledge service, corpus, model, transcript or native configuration is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import sysconfig
import time
import tomllib
from pathlib import Path

from .component_releases import runtime_identity
from .filesystem import filesystem_path
from .loop.locks import lock_file, unlock_file
from .loop.process import bounded_run
from .runtime_inventory import validate_targets
from .storage.lifecycle import configure_same_volume_uv_cache, prune_component_runtimes

WINDOWS_SHORT_KEY_LENGTH = 24
WINDOWS_FALLBACK_KEY_LENGTH = 32
_IS_WINDOWS = os.name == "nt"
PRE_BUILD_GC_CEILING_SECONDS = 5.0
BUILD_CLEANUP_GRACE_SECONDS = 10.0
POST_BUILD_SWEEP_CEILING_SECONDS = 3.0


_TEXT_SUFFIXES = frozenset({
    ".py", ".json", ".md", ".toml", ".svg", ".txt", ".yml", ".yaml", ".lock", ".cjs", ".js", ".sh",
})


def _read_source_bytes(path: Path, limit: int | None = None) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read() if limit is None else stream.read(limit)
    if path.suffix in _TEXT_SUFFIXES and b"\r\n" in raw:
        raw = raw.replace(b"\r\n", b"\n")
    return raw


def source_files(source):
    """Native source only; generated host bindings are mutable installation data."""
    source = Path(source)
    excluded = {".git", ".venv", "__pycache__", "node_modules", "dist", "build",
                "config", "host-package", ".pytest_cache"}
    result = []
    for directory, children, names in os.walk(source):
        children[:] = [name for name in children
                       if name not in excluded and not name.endswith(".egg-info")]
        for name in names:
            path = Path(directory) / name
            if path.suffix != ".pyc" and path.is_file():
                result.append(path.relative_to(source))
    return sorted(result, key=str)


def source_identity(source):
    digest, size = hashlib.sha256(), 0
    for relative in source_files(source):
        raw = _read_source_bytes(Path(source) / relative, 16 * 1024 * 1024 - size + 1)
        size += len(raw)
        if size > 16 * 1024 * 1024:
            raise ValueError("adapter runtime source exceeds 16 MiB")
        name = relative.as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(raw).to_bytes(8, "big") + raw)
    return digest.hexdigest()


def source_baseline(source, *, active_overrides=False, include_substrate=False):
    """Read bootstrap pins; native owners explicitly opt into active source floors.

    Legacy owners compare these pins with a candidate built from requirements.
    Active pins cannot imply that the candidate interpreter already has them.
    """
    from .runtime_inventory import COMPONENTS, COMPONENTS_V2
    registry = COMPONENTS_V2 if include_substrate else COMPONENTS
    requirements = Path(source) / "runtime-requirements.txt"
    if requirements.is_file():
        pins = {}
        paths = [requirements]
        if active_overrides:
            paths.append(Path(source) / "runtime" / "active-overrides.txt")
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                declared = re.match(r"([\w-]+)\s*@", line)
                if declared is None or declared[1] not in registry:
                    continue
                match = re.fullmatch(r"([\w-]+)\s*@\s*git\+https://github.com/([\w-]+/[\w-]+)@([0-9a-f]{40})", line)
                if match is None or registry[match[1]] != match[2]:
                    raise ValueError("adapter component source must pin an official repository and full SHA")
                pins[match[1]] = match[3]
    else:
        project = tomllib.loads((Path(source) / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {name: row["rev"] for name, row in project["tool"]["uv"]["sources"].items()
                if name in registry}
    if not pins:
        raise ValueError("adapter declares no component source pins")
    return dict(source_sha256=source_identity(source), runtime=pins)


def discover(python, minimum, *, force=False, catalog=None, required_components=None, include_substrate=False):
    """Select the runtime plus native-owner-declared external component CLIs.

    An external installation's package version is only a floor, never a source
    receipt. Newly managed components must come from verified formal releases.
    """
    from semantic_version import Version

    from .component_releases import Catalog, select
    from .runtime_inventory import COMPONENTS, COMPONENTS_V2
    registry = COMPONENTS_V2 if include_substrate else COMPONENTS
    current = read_inventory(python, include_substrate=True) if include_substrate else read_inventory(python)
    catalog = catalog or (Catalog(include_substrate=True) if include_substrate else Catalog())
    floor = {name: catalog.source(name, revision) for name, revision in minimum.items()}
    select_kw = {"baseline": floor, **({"include_substrate": True} if include_substrate else {})}
    targets = select(current, lambda name: catalog.latest(name, force=force), **select_kw)
    for name, version in (required_components or {}).items():
        if name not in registry:
            raise ValueError(f"unsupported required component: {name}")
        if name not in targets:
            targets[name] = catalog.latest(name, force=force)
            validate_targets({name: targets[name]}, include_substrate=include_substrate)
        if version is not None and Version(targets[name]["version"]) < Version(version):
            raise ValueError(
                f"formal component {name} {targets[name]['version']} is older than "
                f"the configured component {version}; current binding was not replaced")
    return dict(installed=current, runtime=targets, changed=targets != current)


def project_text(source, targets, *, adapter_distribution=None, include_substrate=False):
    validate_targets(targets, include_substrate=include_substrate)
    requirements = [f"{name} @ git+https://github.com/{row['repository']}@{row['revision']}"
                    for name, row in sorted(targets.items())]
    dependencies = list(requirements)
    if adapter_distribution:
        dependencies.append(f"{adapter_distribution} @ {Path(source).resolve().as_uri()}")
    return ("[project]\nname = \"mindie-component-runtime\"\nversion = \"0.0.0\"\n"
            "requires-python = \">=3.11\"\ndependencies = " + json.dumps(dependencies) + "\n\n"
            "[tool.uv]\npackage = false\noverride-dependencies = " + json.dumps(requirements) + "\n")


def generation_key(baseline, targets):
    """Full 64-hex content identity; external callers depend on this shape."""
    value = dict(source=baseline["source_sha256"], runtime=targets,
                 abi=sys.implementation.cache_tag, platform=sysconfig.get_platform())
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _manifest_generation_key(path: Path) -> str | None:
    """The full key a completed manifest belongs to, or ``None`` if unknown."""
    manifest = Path(path) / "components.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    declared = value.get("generation_key")
    if isinstance(declared, str):
        return declared
    source = value.get("adapter_source_sha256")
    targets = value.get("runtime")
    if not isinstance(source, str) or not isinstance(targets, dict):
        return None
    try:
        return generation_key({"source_sha256": source}, targets)
    except (TypeError, ValueError, KeyError):
        return None


def _generation_matches(path: Path, key: str) -> bool:
    manifest = Path(path) / "components.json"
    if not manifest.is_file():
        return False
    existing = _manifest_generation_key(path)
    # A legacy manifest without a derivable key was complete for every
    # practical purpose; only a positively different key is a collision.
    return existing is None or existing == key


def generation_dir(root: Path, key: str) -> Path:
    """Reuse a full-key directory; otherwise use a Windows-safe short key.

    ``<root>/<24hex>/runtime/.venv/Scripts/pythonw.exe`` stays well under the
    Windows 260-character Task Scheduler limit. A pre-existing 64-hex
    directory is always reused verbatim. A short directory with a completed
    manifest is reused only when its stored ``generation_key`` (or a key
    recomputed from ``adapter_source_sha256``/``runtime``) matches; a
    positively different key is a short-key collision, so 32 hex and then the
    full key are tried instead of crashing the build in the wrong tree.
    """
    root = Path(root)
    full = root / key
    if full.exists() or not _IS_WINDOWS:
        return full
    short = root / key[:WINDOWS_SHORT_KEY_LENGTH]
    if not short.exists():
        return short
    if _generation_matches(short, key) or (
            (short / "prepare.lock").is_file()
            and not (short / "components.json").is_file()):
        return short
    fallback = root / key[:WINDOWS_FALLBACK_KEY_LENGTH]
    if fallback.exists():
        if _generation_matches(fallback, key) or (
                (fallback / "prepare.lock").is_file()
                and not (fallback / "components.json").is_file()):
            return fallback
        return full
    return fallback


def _discard(path: Path) -> None:
    """Best-effort removal of a failed build; never masks the original error."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
            return
        path.unlink(missing_ok=True)
    except TimeoutError:
        raise
    except OSError:
        pass


def _verify_generation_identity(generation: Path, expected) -> None:
    """Refuse a build whose locked generation was already moved to ``.trash-*``.

    A concurrent retirement renames the directory while a builder is between
    ``mkdir`` and the ``prepare.lock`` acquisition. The lock file would then
    belong to a detached trash copy; comparing the directory identity after the
    lock closes that window instead of publishing into a retired generation.
    """
    try:
        current = os.stat(filesystem_path(generation))
    except TimeoutError:
        raise
    except OSError:
        raise RuntimeError(
            "component generation moved while its builder lock was acquired") from None
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)):
        raise RuntimeError(
            "component generation moved while its builder lock was acquired")


def _post_build_sweep(deadline=None) -> None:
    """Silently reclaim cross-harness legacy baggage after a component update.

    This is how an installed user with only a background component auto-update
    still receives 0.13.21 reclamation before host plugins themselves update.
    The sweep owns a 600 s cooldown and a bounded slice of the build deadline;
    a non-timeout cleanup warning can never turn a verified build into a
    failure (or abort returning its already-published result).
    """
    from .storage.legacy import sweep_legacy_installations

    budget = POST_BUILD_SWEEP_CEILING_SECONDS
    if deadline is not None:
        budget = min(budget, max(0.0, deadline - time.monotonic()))
    if budget <= 0:
        return
    try:
        sweep_legacy_installations(budget_seconds=budget, deadline=deadline)
    except TimeoutError:
        return
    except Exception:  # noqa: BLE001 - best-effort cleanup after a verified build
        return


def _pre_build_prune(root: Path, generation: Path, budget: float,
                     deadline: float | None = None) -> None:
    """Free space before uv sync; a cleanup warning never blocks the build."""
    from .storage.legacy import collect_global_protected_roots
    from .storage.legacy_roots import protected_source_roots
    from .storage.leases import live_process_prefixes

    if deadline is None:
        deadline = time.monotonic() + max(0.0, budget)
    budget = min(max(0.0, budget), max(0.0, deadline - time.monotonic()))
    if budget <= 0:
        return
    stop = min(deadline, time.monotonic() + budget)
    try:
        live_paths, complete = live_process_prefixes(
            timeout=min(1.0, max(0.05, stop - time.monotonic())))
        protected = collect_global_protected_roots(live_paths=live_paths,
                                                   deadline=stop)
        forbidden = protected_source_roots(deadline=stop)
        protected.add(generation)
        prune_component_runtimes(
            root, keep_paths=protected, keep_recent_verified=2,
            staging_grace_seconds=1800.0,
            budget_seconds=max(0.0, stop - time.monotonic()),
            live_paths=live_paths, inspection_complete=complete,
            include_global_roots=False, forbidden_roots=forbidden,
            deadline=stop)
    except TimeoutError:
        return  # The bounded budget expired; the build keeps whatever space it has.
    except Exception:  # noqa: BLE001 - best-effort cleanup never blocks a build
        return


def read_inventory(python, *, timeout=15, include_substrate=False):
    script = Path(__file__).with_name("runtime_inventory.py")
    extra = ["--include-substrate"] if include_substrate else []
    return json.loads(bounded_run([str(python), "-I", "-X", "utf8", str(script), *extra], "", timeout=timeout,
                                  max_output=64 * 1024))


def read_versions(python, names, *, timeout=15, include_substrate=False):
    """Read explicitly selected external CLI versions without asserting VCS origin."""
    script = Path(__file__).with_name("runtime_inventory.py")
    extra = ["--include-substrate"] if include_substrate else []
    return json.loads(bounded_run([str(python), "-I", "-X", "utf8", str(script), *extra, "--versions", *names],
                                  "", timeout=timeout, max_output=64 * 1024))


def build(source, root, baseline, targets, *, python=sys.executable,
          adapter_distribution=None, timeout=420, env=None, files=None,
          include_substrate=False):
    """Prepare and verify; publication of a native binding stays with its owner.

    One absolute deadline covers ``source_files``, pre-build GC, ``uv sync``,
    inventory verification and the post-build sweep. Every inner budget is
    derived from the same remaining time and reserves cleanup grace, so
    ``outer_timeout >= sum(inner_budgets) + cleanup_grace``.
    """
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout))
    source, root = Path(source).resolve(), Path(root).resolve()
    files = source_files(source) if files is None else [Path(item) for item in files]
    validate_targets(targets, include_substrate=include_substrate)
    if not set(baseline["runtime"]) <= set(targets):
        raise ValueError("component selection omits an adapter dependency")
    key = generation_key(baseline, targets)
    generation = generation_dir(root, key)
    if generation.is_relative_to(source):
        raise ValueError("component generations must be outside the adapter source")
    # Free legacy and failed-build space before uv sync starts on a nearly full
    # volume. GC holds no generation lock, so a slow/unavailable cleanup cannot
    # coalesce or delay the real build.
    _pre_build_prune(
        root, generation,
        min(PRE_BUILD_GC_CEILING_SECONDS, max(0.0, float(timeout) * 0.1)),
        deadline)
    generation.mkdir(parents=True, exist_ok=True)
    try:
        before = os.stat(filesystem_path(generation))
    except TimeoutError:
        raise
    except OSError:
        raise RuntimeError(
            "component generation disappeared before its build") from None
    runtime = generation / "runtime"
    manifest_file = generation / "components.json"
    temporary = manifest_file.with_suffix(".pending.json")
    descriptor = os.open(filesystem_path(generation / "prepare.lock"),
                         os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    fresh = False
    published = False
    try:
        lock_file(descriptor, exclusive=True)
        locked = True
        # A retirement that renamed this generation while the lock was being
        # taken must be detected before any retained manifest is trusted.
        _verify_generation_identity(generation, before)
        selected_python = runtime / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python")
        fresh = not manifest_file.is_file()
        if not fresh:
            components = json.loads(manifest_file.read_text(encoding="utf-8"))
            if components.get("generation_key") not in (None, key):
                raise ValueError("completed component runtime belongs to a different generation")
            if (components["adapter_source_sha256"] != baseline["source_sha256"]
                    or components["runtime"] != targets):
                raise ValueError("completed component runtime differs from its generation")
        else:
            retained = generation / "source"
            if retained.exists():
                shutil.rmtree(retained)
            size = 0
            for relative in files:
                relative = Path(relative)
                src_file = source / relative
                size += src_file.stat().st_size
                if size > 16 * 1024 * 1024:
                    raise ValueError("adapter runtime source exceeds 16 MiB")
                destination = retained / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, destination)
                if src_file.suffix in _TEXT_SUFFIXES:
                    destination.write_bytes(_read_source_bytes(src_file))
            runtime.mkdir(exist_ok=True)
            (runtime / "pyproject.toml").write_text(
                project_text(retained, targets, adapter_distribution=adapter_distribution,
                             include_substrate=include_substrate),
                encoding="utf-8", newline="\n")
            from ._host_exec import resolve_uv
            uv = shutil.which("uv") if shutil.which.__module__ != "shutil" else resolve_uv()
            if uv is None:
                raise FileNotFoundError("uv is required to prepare a component runtime")
            environment = dict(os.environ if env is None else env)
            environment.pop("PYTHONPATH", None)
            environment.update(UV_PROJECT_ENVIRONMENT=str(runtime / ".venv"), UV_NO_PROGRESS="1",
                               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
            if "UV_SYSTEM_CERTS" not in environment and "UV_NATIVE_TLS" not in environment:
                environment["UV_SYSTEM_CERTS"] = "true"
                environment["UV_NATIVE_TLS"] = "true"
            elif "UV_SYSTEM_CERTS" not in environment:
                environment.setdefault("UV_NATIVE_TLS", "true")
            if "GIT_CONFIG_COUNT" not in environment:
                git_pairs = (
                    ("core.longpaths", "true"),
                    ("core.autocrlf", "false"),
                    ("credential.https://github.com.provider", "github"),
                )
                environment["GIT_CONFIG_COUNT"] = str(len(git_pairs))
                for idx, (k, v) in enumerate(git_pairs):
                    environment[f"GIT_CONFIG_KEY_{idx}"] = k
                    environment[f"GIT_CONFIG_VALUE_{idx}"] = v
            # A cross-device uv cache silently turns clonefile/hardlinks into a
            # full 250 MB copy per generation. An explicit caller value wins.
            environment = configure_same_volume_uv_cache(root, environment)
            sync_budget = deadline - time.monotonic() - BUILD_CLEANUP_GRACE_SECONDS
            if sync_budget <= 0:
                raise TimeoutError("component runtime build budget exhausted")
            bounded_run([uv, "sync", "--project", str(runtime), "--no-editable", "--no-dev",
                         "--python", str(python)], "", timeout=sync_budget,
                        max_output=256 * 1024, child_env=environment)
            schema_ver = "mindie-component-runtime/2" if "mindie-harness" in targets else "mindie-component-runtime/1"
            components = dict(schema=schema_ver,
                              adapter_source_sha256=baseline["source_sha256"],
                              runtime=targets, generation_key=key,
                              lock_sha256=hashlib.sha256((runtime / "uv.lock").read_bytes()).hexdigest())
            temporary.write_text(json.dumps(components, sort_keys=True), encoding="utf-8", newline="\n")
        # A completed marker is not proof that the retained adapter stayed
        # intact. Compare only source inputs; native generated config is outside
        # this selection and belongs to its existing binding transaction.
        retained = generation / "source"
        for relative in files:
            if _read_source_bytes(source / relative) != _read_source_bytes(retained / relative):
                raise ValueError("retained component adapter source changed")
        remaining = deadline - time.monotonic()
        verify_budget = min(15.0, max(0.0, remaining - POST_BUILD_SWEEP_CEILING_SECONDS))
        if verify_budget <= 0:
            raise TimeoutError("component runtime verification budget exhausted")
        script = Path(__file__).with_name("runtime_inventory.py")
        candidate_manifest = manifest_file if manifest_file.exists() else temporary
        extra = ["--include-substrate"] if include_substrate else []
        actual = json.loads(bounded_run([str(selected_python), "-I", "-X", "utf8", str(script), *extra, str(candidate_manifest)], "",
                                       timeout=verify_budget, max_output=64 * 1024))
        if candidate_manifest != manifest_file:
            os.replace(candidate_manifest, manifest_file)
            published = True
        else:
            published = manifest_file.is_file()
        # A post-publish cleanup warning (or any ordinary cleanup error) must
        # never hide the already-verified result.
        try:
            _post_build_sweep(deadline)
        except TimeoutError:
            pass
        except Exception:  # noqa: BLE001 - the build result is already verified
            pass
        verified_components = {name: value for name, value in components.items()
                               if name != "generation_key"}
        return dict(generation=str(generation), source=str(generation / "source"),
                    python=str(selected_python), components=components,
                    identity=runtime_identity(baseline, verified_components,
                                              include_substrate=include_substrate),
                    installed=actual)
    except BaseException:
        # A failed fresh build must not leave a 250 MiB unverified .venv without
        # components.json behind. A previously completed generation is
        # untouched, and a just-published manifest is never removed.
        if locked and fresh and not published and not manifest_file.is_file():
            _discard(runtime)
            _discard(generation / "source")
            _discard(temporary)
        raise
    finally:
        # Closing also releases a lock when acquisition itself failed.
        try:
            if locked:
                unlock_file(descriptor)
        finally:
            os.close(descriptor)


def main():
    """Native bootstraps can delegate to the already-installed shared runtime."""
    raw = sys.stdin.buffer.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("component update request exceeds its byte limit")
    request = json.loads(raw)
    if request["operation"] == "discover":
        result = discover(request["python"], request.get("minimum", {}),
                          force=request.get("force", False),
                          required_components=request.get("required_components"),
                          include_substrate=bool(request.get("include_substrate", False)))
    elif request["operation"] == "build":
        result = build(request["source"], request["root"], request["baseline"], request["runtime"],
                       python=request["python"], adapter_distribution=request.get("adapter_distribution"),
                       timeout=request.get("timeout", 420), files=request.get("files"),
                       include_substrate=bool(request.get("include_substrate", False)))
    elif request["operation"] == "prune":
        # ``prune_component_runtimes`` inspects the live process table when no
        # snapshot is supplied and unions the caller's keep paths with every
        # global host binding unless isolation is explicit.
        keep = [Path(item) for item in request.get("keep_paths", ())]
        result = prune_component_runtimes(
            Path(request["root"]), keep_paths=keep,
            keep_recent_verified=request.get("keep_recent_verified", 2),
            budget_seconds=request.get("budget_seconds", 5.0),
            include_global_roots=request.get("include_global_roots", True))
    elif request["operation"] == "sweep_legacy":
        from .storage.legacy import sweep_legacy_installations

        result = sweep_legacy_installations(
            config_home=request.get("config_home"), data_home=request.get("data_home"),
            codex_home=request.get("codex_home"),
            budget_seconds=request.get("budget_seconds", 8.0),
            force=request.get("force", False))
    else:
        raise ValueError("unsupported component update operation")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
