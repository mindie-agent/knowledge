"""[Role] Silent retroactive reclamation of historical MindIE runtime baggage.

[Invariants]
- Deletion scan roots come only from MindIE's fixed owned storage directories
  (``<data_home>/{kimi,dsh,cc,wb-state,agy-state,oc-state,runtimes}``,
  ``<config_home>/{diagnostics.runtime,dsh/engines,generations}``,
  ``<codex_home>/plugins/data/mindie-agent-mindie-agent`` and
  ``~/.cache/mindie-agent-dsh/uv-cache``). ``source_root``/``package_dir`` and
  every other config value are protected pins only, never scan roots.
- No candidate is retired when its path or any ancestor up to the filesystem
  root contains ``.git``, or when it lies inside a protected user
  ``source_root``.
- Nothing is retired until every protection source is checked: the running
  interpreter/prefix, every MindIE config pin, current/previous update
  pointers, the immediate rollback receipt, hidden host launcher pins, live OS
  processes, ``.inuse.lock`` leases, builder ``prepare.lock``/``.lock`` holes,
  and the staging grace. Unverified staging and verified excess obey exactly
  the same gate.
- Only MindIE-owned disposable directories are swept. A Git checkout,
  ``RemoteReceipts`` database, consent store or authoritative knowledge state
  is never a target.
- Incomplete POSIX process inspection keeps verified executable generations;
  only trash and stale broken staging are reclaimed.
- A cleanup failure never becomes a primary-operation failure and no marker or
  tombstone suppresses a later honest retry.

[Timeouts & Locks]
- One absolute deadline bounds the sweep; every managed target gets a bounded
  slice, and every scan/deletion loop stops immediately on expiry and reports
  ``partial``. No lock is waited on; busy leases and builder locks only cause a
  keep. The cooldown marker is advisory (600 s) and is bypassed by
  ``force=True`` or leftover ``.trash-*``.

[Side-Effect Policy]
- Deletes only disposable managed directories and writes the cooldown marker
  ``<data_home>/.storage-gc-state.json``. No network, model or config rewrite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

from ..filesystem import filesystem_path, path_identity
from .leases import is_lease_held, is_path_used_by_live_process, live_process_prefixes
from .legacy_roots import (
    MARKER_SCHEMA, _config_documents, _config_homes, _data_homes,
    _env, _home, _read_json, _strings, collect_global_protected_roots,
    owning_dir, protected_source_roots,
)
from .lifecycle import (
    _builder_locked, _expired, is_junction, is_own_runtime_path,
    is_protected_path, is_within_roots, prune_component_runtimes,
    retire_directory, sweep_trash,
)

COOLDOWN_SECONDS, STAGING_GRACE_SECONDS = 600.0, 1800.0
VERIFIED_RETENTION = 2
RECEIPT_RETENTION = SUPERSEDED_RETENTION = 8
LOG_GRACE_SECONDS, STATE_MARKER = 3600.0, ".storage-gc-state.json"
_SHA = re.compile(r"[0-9a-f]{40,64}")


# --------------------------------------------------------------------------- #
# Managed targets: only fixed MindIE-owned storage directories.
# --------------------------------------------------------------------------- #

def _state_roots(config_homes, data_homes, codex_home) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for data in data_homes:
        roots.append(("data", data))
        for name in ("kimi", "cc", "dsh", "wb-state", "agy-state", "oc-state",
                     "runtimes"):
            roots.append((name, data / name))
        roots.extend((("kimi", data / "kimi" / "adapter-state"),
                      ("cc", data / "cc" / "adapter-state")))
    for config in config_homes:
        roots.extend((("config", config), ("kimi", config / "state")))
        for name in ("kimi", "cc", "dsh", "agy", "wb", "oc"):
            roots.append((name, config / name))
        roots.extend((("dsh", config / "diagnostics.runtime"),
                      ("dsh", config / "dsh" / "engines"),
                      ("oc", config / "generations")))
    codex_data = Path(codex_home) / "plugins" / "data" / "mindie-agent-mindie-agent"
    roots.extend((("codex", codex_data),
                  ("codex", codex_data / "codex.updates")))
    for config in config_homes:
        roots.append(("codex", config / "codex.updates"))
    return roots


def _add_target(targets, seen, kind, path, harness=None) -> None:
    path = Path(os.path.normpath(str(path)))
    key = os.path.normcase(os.path.abspath(str(path)))
    if key not in seen:
        seen.add(key)
        targets.append({"kind": kind, "path": path, "harness": harness})


def _discover_targets(config_homes, data_homes, codex_home) -> list[dict]:
    targets: list[dict] = []
    seen: set[str] = set()
    for harness, root in _state_roots(config_homes, data_homes, codex_home):
        for kind, relative in (
            ("component-runtimes", "component-runtimes"),
            ("component-runtimes", "update/component-runtimes"),
            ("generations", "update/generations"),
            ("launch", "update/launch"),
            ("receipts", "update/receipts"),
            ("cc-runtimes", "runtimes/cc"),
            ("runtimes", "runtimes"),
            ("installations", "adapter-state/installations"),
            ("installations", "installations"),
            ("sources", "wb-source"), ("sources", "agy-source"),
            ("sources", "oc-source"), ("engines", "engines"),
            ("releases", "releases"),
        ):
            if (root / relative).is_dir():
                _add_target(targets, seen, kind, root / relative, harness)
        if root.name.casefold() == "dsh" and (root / "venv").is_dir():
            _add_target(targets, seen, "dsh-venv", root / "venv", harness)
    for config in config_homes:
        diagnostics = config / "diagnostics.runtime"
        if (diagnostics / "generations").is_dir():
            _add_target(targets, seen, "generations",
                        diagnostics / "generations", "diagnostics")
        updates = config / "codex.updates"
        if updates.is_dir():
            _add_target(targets, seen, "superseded", updates, "codex")
        generations = config / "generations"
        if generations.is_dir() and any(generations.glob("oc.engine.*.json")):
            _add_target(targets, seen, "oc-engines", generations, "oc")
    if (uv_cache := _home() / ".cache" / "mindie-agent-dsh" / "uv-cache").is_dir():
        _add_target(targets, seen, "uv-cache", uv_cache, "dsh")
    return targets


def _entries(directory) -> list[Path]:
    try:
        return sorted(Path(directory).iterdir(), key=lambda item: item.name)
    except TimeoutError:
        raise
    except OSError:
        return []


def _mtime(path: Path) -> float:
    try:
        return os.stat(filesystem_path(path)).st_mtime
    except TimeoutError:
        raise
    except OSError:
        return 0.0


def _protected_name(entry: Path, protected) -> bool:
    if is_protected_path(entry, protected):
        return True
    try:
        key = path_identity(entry)
    except TimeoutError:
        raise
    except (OSError, RuntimeError):
        key = os.path.normcase(os.path.abspath(os.fspath(entry)))
    for keep in protected:
        try:
            spelling = path_identity(keep)
        except TimeoutError:
            raise
        except (OSError, RuntimeError):
            spelling = os.path.normcase(os.path.abspath(os.fspath(keep)))
        if spelling == key:
            return True
    return False


def _is_link(entry: Path) -> bool:
    return entry.is_symlink() or is_junction(entry)


def _keep(entry: Path, protected, live) -> bool:
    if _protected_name(entry, protected) or is_own_runtime_path(entry):
        return True
    if _is_link(entry):
        # Never inspect inside a junction/symlink target for a lease or builder
        # lock; only a live command line may still keep the link itself.
        return is_path_used_by_live_process(entry, live)
    return (is_lease_held(entry) or _builder_locked(entry)
            or is_path_used_by_live_process(entry, live))


def _has_git_ancestor(entry: Path) -> bool:
    probe = Path(os.path.abspath(os.fspath(entry)))
    while True:
        try:
            if (probe / ".git").exists():
                return True
        except TimeoutError:
            raise
        except OSError:
            return True  # Unknown inspection is a keep.
        if probe.parent == probe:
            return False
        probe = probe.parent


def _recent(entry: Path, grace_seconds: float) -> bool:
    if grace_seconds <= 0:
        return False
    return time.time() - _mtime(entry) < grace_seconds


def _retire_path(entry: Path, deadline, account, *, protected, live, complete,
                 verified=False, grace_seconds: float = STAGING_GRACE_SECONDS,
                 forbidden_roots=()) -> bool:
    """Run every protection check before one candidate may be retired."""
    if _keep(entry, protected, live):
        account["kept_dirs"] += 1
        return False
    if is_within_roots(entry, forbidden_roots):
        account["kept_dirs"] += 1
        account["warnings"].append("protected source root: " + entry.name[:32])
        return False
    if _has_git_ancestor(entry) or ".git" in entry.parts \
            or "RemoteReceipts" in entry.parts:
        account["kept_dirs"] += 1
        account["warnings"].append("protected checkout: " + entry.name[:32])
        return False
    if _recent(entry, grace_seconds):
        account["kept_dirs"] += 1
        return False
    if not complete and verified:
        account["deferred_dirs"] += 1
        return False
    if _expired(deadline):
        account["deferred_dirs"] += 1
        account["partial"] = True
        account["warnings"].append("budget exhausted")
        return False
    linked = _is_link(entry)
    recheck = None
    if not linked:
        recheck = lambda: (not _protected_name(entry, protected)
                           and not is_own_runtime_path(entry)
                           and not is_path_used_by_live_process(entry, live)
                           and not is_within_roots(entry, forbidden_roots)
                           and not _has_git_ancestor(entry))
    outcome = retire_directory(entry, deadline=deadline, recheck=recheck)
    if outcome["status"] in {"retired", "trashed_partial"}:
        account["reclaimed_dirs"] += 1
        if outcome.get("partial"):
            account["partial"] = True
        return True
    if outcome["status"] == "deferred_in_use":
        account["deferred_dirs"] += 1
    elif outcome.get("error"):
        account["warnings"].append("retire " + entry.name[:32] + ": " + outcome["error"])
    if outcome.get("partial"):
        account["partial"] = True
    return False


def _unlink_file(entry: Path, account, deadline=None) -> None:
    if _expired(deadline):
        account["partial"] = True
        account["warnings"].append("budget exhausted")
        return
    try:
        entry.unlink(missing_ok=True)
        account["reclaimed_files"] += 1
    except TimeoutError:
        raise
    except OSError as exc:
        account["warnings"].append("file " + entry.name[:32] + ": " + type(exc).__name__)


def _sweep_ranked(entries, protected, live, complete, deadline, account, *,
                  recent, marker=None, forbidden_roots=()):
    """Keep protected roots plus the newest ``recent`` entries.

    Protection never consumes a retention slot; an older host pin cannot
    displace a newer executable generation from its own retention window.
    """
    ranked, unverified = [], []
    for entry in entries:
        if entry.name.startswith(".trash-"):
            continue
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            break
        if marker is None or marker(entry):
            ranked.append(entry)
        else:
            unverified.append(entry)
    for entry in unverified:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            break
        _retire_path(entry, deadline, account, protected=protected, live=live,
                     complete=complete, verified=False,
                     forbidden_roots=forbidden_roots)
    ranked.sort(key=lambda item: (_mtime(item), item.name), reverse=True)
    newest = ranked[:recent]
    kept = []
    for entry in ranked:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            break
        if entry in newest:
            account["kept_dirs"] += 1
            kept.append(entry)
            continue
        if _keep(entry, protected, live):
            account["kept_dirs"] += 1
            kept.append(entry)
            continue
        if _retire_path(entry, deadline, account, protected=protected, live=live,
                        complete=complete, verified=True,
                        forbidden_roots=forbidden_roots):
            continue
        kept.append(entry)
    return kept


def _verified_generation(entry: Path) -> bool:
    if _is_link(entry):
        return True  # Ranked, then removed as a link without reading its target.
    return any((entry / name).is_dir() for name in (".venv", "venv", "package"))


def _sweep_generations(target, protected, live, complete, deadline, account,
                       forbidden_roots=()) -> None:
    directory = target["path"]
    recent = 1 if target.get("harness") == "kimi" else VERIFIED_RETENTION
    entries = [entry for entry in _entries(directory)
               if entry.is_dir() and entry.name != "bootstrap"]
    kept = _sweep_ranked(entries, protected, live, complete, deadline, account,
                         recent=recent, marker=_verified_generation,
                         forbidden_roots=forbidden_roots)
    for entry in kept:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        asset = entry / "asset.tgz"
        if asset.is_file() and ((entry / "package").is_dir() or (entry / ".venv").is_dir()):
            _unlink_file(asset, account, deadline)


def _sweep_launch(target, protected, live, complete, deadline, account,
                  forbidden_roots=()) -> None:
    directory = target["path"]
    generations = directory.parent / "generations"
    try:
        shas = {entry.name for entry in generations.iterdir() if entry.is_dir()}
    except TimeoutError:
        raise
    except OSError:
        shas = set()
    for entry in _entries(directory):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if entry.name == "bootstrap" or entry.name in shas \
                or _keep(entry, protected, live):
            account["kept_dirs"] += 1
            continue
        _retire_path(entry, deadline, account, protected=protected, live=live,
                     complete=complete, forbidden_roots=forbidden_roots)


def _sweep_receipts(target, protected, live, complete, deadline, account,
                    forbidden_roots=()) -> None:
    files = [entry for entry in _entries(target["path"]) if entry.suffix == ".json"
             and entry.is_file()]
    files.sort(key=lambda entry: (_mtime(entry), entry.name), reverse=True)
    protected_names = {item.name for item in protected}
    protected_shas = {item.name for item in protected if _SHA.fullmatch(item.name)}
    for index, entry in enumerate(files):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if index < RECEIPT_RETENTION or entry.name in protected_names \
                or entry.stem in protected_shas:
            account["kept_dirs"] += 1
            continue
        _unlink_file(entry, account, deadline)


def _sweep_runtimes(target, protected, live, complete, deadline, account,
                    forbidden_roots=()) -> None:
    entries = [entry for entry in _entries(target["path"])
               if entry.is_dir() and entry.name.casefold() != "cc"]
    marker = lambda entry: _is_link(entry) or any(
        (entry / name).exists()
        for name in ("runtime.json", "venv", ".venv", "Scripts", "bin"))
    _sweep_ranked(entries, protected, live, complete, deadline, account, recent=1,
                  marker=marker, forbidden_roots=forbidden_roots)


def _sweep_cc_runtimes(target, protected, live, complete, deadline, account,
                       forbidden_roots=()) -> None:
    entries = [entry for entry in _entries(target["path"]) if entry.is_dir()]
    marker = lambda entry: entry.with_name(entry.name + ".ready.json").is_file()
    kept = {entry.name for entry in _sweep_ranked(
        entries, protected, live, complete, deadline, account,
        recent=VERIFIED_RETENTION, marker=marker,
        forbidden_roots=forbidden_roots)}
    for entry in _entries(target["path"]):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if entry.suffix == ".json" and entry.name.endswith(".ready.json"):
            owner = entry.name[: -len(".ready.json")]
            if owner not in kept and not (entry.parent / owner).is_dir() \
                    and _mtime(entry) < time.time() - 86400:
                _unlink_file(entry, account, deadline)


def _version_key(name: str):
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"[.+-]", name))


def _active_native_versions(config_homes, codex_home) -> set[str]:
    versions = set()
    roots = [codex_home / "plugins" / "data" / "mindie-agent-mindie-agent" / "codex.updates",
             codex_home / "codex.updates", *[item / "codex.updates" for item in config_homes]]
    for root in roots:
        value = _read_json(root / "native.json") or {}
        for name in ("native_version", "version"):
            item = value.get(name)
            if isinstance(item, str) and item:
                versions.add(item)
    return versions


def _sweep_releases(target, protected, live, complete, deadline, account,
                    forbidden_roots=()) -> None:
    directory = target["path"]
    active = target.get("active_versions") or set()
    entries = [entry for entry in _entries(directory) if entry.is_dir()
               and re.match(r"^[0-9]+(?:\.[0-9]+)+", entry.name)]
    entries.sort(key=lambda entry: _version_key(entry.name), reverse=True)
    newest = entries[0] if entries else None
    previous_kept = not active
    for entry in entries:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        keep = _keep(entry, protected, live) or entry.name in active \
            or (not active and entry is newest)
        if not keep and not previous_kept and all(
                _version_key(entry.name) < _version_key(version) for version in active):
            keep, previous_kept = True, True
        if keep:
            account["kept_dirs"] += 1
        else:
            _retire_path(entry, deadline, account, protected=protected, live=live,
                         complete=complete, verified=True, grace_seconds=0.0,
                         forbidden_roots=forbidden_roots)
    for entry in _entries(directory):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if entry.is_file() and entry.name.startswith("prepare-") \
                and entry.suffix == ".log" \
                and _mtime(entry) < time.time() - LOG_GRACE_SECONDS:
            _unlink_file(entry, account, deadline)


def _sweep_newest(target, protected, live, complete, deadline, account,
                  forbidden_roots=()) -> None:
    entries = [entry for entry in _entries(target["path"]) if entry.is_dir()]
    _sweep_ranked(entries, protected, live, complete, deadline, account,
                  recent=VERIFIED_RETENTION, marker=lambda entry: True,
                  forbidden_roots=forbidden_roots)


def _sweep_engines(target, protected, live, complete, deadline, account,
                   runtime_names: set[str], configs_text: str,
                   forbidden_roots=()) -> None:
    files = [entry for entry in _entries(target["path"]) if entry.suffix == ".json"
             and entry.is_file()]
    files.sort(key=lambda entry: (_mtime(entry), entry.name), reverse=True)
    for index, entry in enumerate(files):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        stem = entry.stem.casefold()
        referenced = any(stem in name.casefold() for name in runtime_names) \
            or stem in configs_text.casefold() or _protected_name(entry, protected)
        if index < 2 or referenced or _mtime(entry) > time.time() - LOG_GRACE_SECONDS:
            account["kept_dirs"] += 1
            continue
        _unlink_file(entry, account, deadline)


def _sweep_oc_engines(target, protected, live, complete, deadline, account,
                      forbidden_roots=()) -> None:
    files = [entry for entry in _entries(target["path"])
             if entry.is_file() and entry.name.startswith("oc.engine.")]
    files.sort(key=lambda entry: (_mtime(entry), entry.name), reverse=True)
    for index, entry in enumerate(files):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if index < VERIFIED_RETENTION or _protected_name(entry, protected):
            account["kept_dirs"] += 1
            continue
        _unlink_file(entry, account, deadline)


def _sweep_superseded(target, protected, live, complete, deadline, account,
                      forbidden_roots=()) -> None:
    files = [entry for entry in _entries(target["path"])
             if entry.is_file() and entry.name.startswith("superseded-")]
    files.sort(key=lambda entry: (_mtime(entry), entry.name), reverse=True)
    for entry in files[SUPERSEDED_RETENTION:]:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        _unlink_file(entry, account, deadline)


def _sweep_uv_cache(target, protected, live, complete, deadline, account,
                    configs_text: str, forbidden_roots=()) -> None:
    entry = target["path"]
    if is_path_used_by_live_process(entry, live) or "uv-cache" in configs_text \
            or _mtime(entry) > time.time() - 86400:
        account["kept_dirs"] += 1
        return
    _retire_path(entry, deadline, account, protected=protected, live=live,
                 complete=complete, forbidden_roots=forbidden_roots)


def _sweep_component(target, protected, live, complete, deadline, account,
                     forbidden_roots=()) -> None:
    result = prune_component_runtimes(
        target["path"], keep_paths=protected,
        keep_recent_verified=VERIFIED_RETENTION,
        staging_grace_seconds=STAGING_GRACE_SECONDS,
        budget_seconds=max(0.0, deadline - time.monotonic()),
        live_paths=live, inspection_complete=complete,
        include_global_roots=False, forbidden_roots=forbidden_roots,
        deadline=deadline)
    for name in ("reclaimed_dirs", "deferred_dirs", "kept_dirs", "reclaimed_trash"):
        account[name] += result.get(name, 0)
    account["warnings"].extend(result.get("warnings", []))
    if result.get("partial"):
        account["partial"] = True
    if result.get("status") == "inspection_incomplete":
        account["status"] = "inspection_incomplete"


def _sweep_single(target, protected, live, complete, deadline, account,
                  forbidden_roots=()) -> None:
    entry = target["path"]
    if _keep(entry, protected, live):
        account["kept_dirs"] += 1
        return
    _retire_path(entry, deadline, account, protected=protected, live=live,
                 complete=complete, verified=True,
                 forbidden_roots=forbidden_roots)


def _write_marker(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass


def _leftover_trash(targets) -> bool:
    for target in targets:
        try:
            if any(entry.name.startswith(".trash-") for entry in target["path"].iterdir()):
                return True
        except TimeoutError:
            raise
        except OSError:
            continue
    return False


def _sweep_target_trash(target, deadline, account) -> None:
    result = sweep_trash(target["path"], deadline=deadline)
    account["reclaimed_trash"] += result["removed"]
    account["warnings"].extend(result["warnings"])
    if result.get("partial"):
        account["partial"] = True


def sweep_legacy_installations(*, config_home=None, data_home=None, codex_home=None,
                               budget_seconds: float = 8.0, force: bool = False,
                               deadline: float | None = None) -> dict:
    """Reclaim historical baggage across every MindIE harness on this machine."""
    started = time.monotonic()
    limit = started + max(0.0, float(budget_seconds))
    if deadline is not None:
        limit = min(limit, deadline)
    config_homes = _config_homes(config_home)
    data_homes = _data_homes(data_home)
    codex = Path(codex_home or _env("CODEX_HOME") or _home() / ".codex").expanduser()
    account = dict(schema=MARKER_SCHEMA, status="completed", reclaimed_dirs=0,
                   deferred_dirs=0, kept_dirs=0, reclaimed_trash=0,
                   reclaimed_files=0, warnings=[], inspection_complete=True,
                   protected_roots=0, partial=False, elapsed_seconds=0.0)
    targets = _discover_targets(config_homes, data_homes, codex)
    marker = data_homes[0] / STATE_MARKER
    state = _read_json(marker)
    recent = isinstance(state, dict) and isinstance(state.get("at"), (int, float)) \
        and time.time() - state["at"] < COOLDOWN_SECONDS
    if not force and recent and not _leftover_trash(targets):
        account["status"] = "skipped_cooldown"
        return account
    if _expired(limit):
        account["status"] = "partial"
        account["partial"] = True
        account["warnings"].append("budget exhausted")
        return account
    live_paths, complete = live_process_prefixes(
        timeout=max(0.1, min(2.0, limit - time.monotonic())))
    account["inspection_complete"] = complete
    try:
        protected = collect_global_protected_roots(
            config_home=config_homes, data_home=data_homes, codex_home=codex,
            live_paths=live_paths, deadline=limit)
        forbidden_roots = protected_source_roots(config_home=config_homes,
                                                  deadline=limit)
    except TimeoutError:
        account["status"] = "partial"
        account["partial"] = True
        account["inspection_complete"] = False
        account["warnings"].append("protected roots budget exhausted")
        account["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return account
    account["protected_roots"] = len(protected)
    documents = _config_documents(config_homes, deadline=limit)
    configs_text = " ".join(" ".join(_strings(document[1])) for document in documents)
    runtime_names: set[str] = set()
    for target in targets:
        if target["kind"] in {"runtimes", "cc-runtimes"}:
            runtime_names.update(entry.name for entry in _entries(target["path"]))
    active_versions = _active_native_versions(config_homes, codex)
    handlers = {
        "component-runtimes": _sweep_component,
        "generations": _sweep_generations, "launch": _sweep_launch,
        "receipts": _sweep_receipts, "runtimes": _sweep_runtimes,
        "cc-runtimes": _sweep_cc_runtimes, "installations": _sweep_newest,
        "sources": _sweep_newest, "oc-engines": _sweep_oc_engines,
        "superseded": _sweep_superseded, "dsh-venv": _sweep_single,
        "releases": lambda t, p, l, c, d, a, f: _sweep_releases(
            dict(t, active_versions=active_versions), p, l, c, d, a, f),
        "engines": lambda t, p, l, c, d, a, f: _sweep_engines(
            t, p, l, c, d, a, runtime_names, configs_text, f),
        "uv-cache": lambda t, p, l, c, d, a, f: _sweep_uv_cache(
            t, p, l, c, d, a, configs_text, f),
    }
    for target in targets:
        if _expired(limit):
            account["partial"] = True
            account["deferred_dirs"] += 1
            account["warnings"].append("budget exhausted")
            break
        remaining = max(0.1, min(2.0, limit - time.monotonic()))
        target_deadline = min(limit, time.monotonic() + remaining)
        try:
            _sweep_target_trash(target, target_deadline, account)
            handler = handlers.get(target["kind"])
            if handler is not None:
                handler(target, protected, live_paths, complete, target_deadline,
                        account, forbidden_roots)
        except TimeoutError:
            account["deferred_dirs"] += 1
            account["partial"] = True
            account["warnings"].append("timeout: " + target["kind"])
        except Exception as exc:  # noqa: BLE001 - cleanup must never brick a caller
            account["warnings"].append(target["kind"] + ": " + type(exc).__name__)
    if not complete:
        account["status"] = "inspection_incomplete"
    if account["partial"] and account["status"] == "completed":
        account["status"] = "partial"
    account["elapsed_seconds"] = round(time.monotonic() - started, 3)
    account["trash_remaining"] = sum(
        1 for target in targets
        for entry in _entries(target["path"]) if entry.name.startswith(".trash-"))
    try:
        _write_marker(marker, dict(schema=MARKER_SCHEMA, at=time.time(),
                                   reclaimed_dirs=account["reclaimed_dirs"],
                                   status=account["status"]))
    except TimeoutError:
        raise
    except OSError as exc:
        account["warnings"].append("marker: " + type(exc).__name__)
    return account


__all__ = [
    "COOLDOWN_SECONDS",
    "MARKER_SCHEMA",
    "collect_global_protected_roots",
    "owning_dir",
    "sweep_legacy_installations",
]
