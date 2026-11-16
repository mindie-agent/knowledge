"""[Role] Silent retroactive reclamation of historical MindIE runtime baggage.

[Invariants]
- Deletion scan roots come only from MindIE's fixed owned storage directories
  (``<data_home>/{kimi,dsh,cc,wb-state,agy-state,oc-state,runtimes}``,
  ``<config_home>/{diagnostics.runtime,dsh/engines,generations}``,
  ``<codex_home>/plugins/data/mindie-agent-mindie-agent`` and
  ``~/.cache/mindie-agent-dsh/uv-cache``). ``source_root``/``package_dir`` and
  every other config value are protected pins only, never scan roots.
- No candidate is retired or file unlinked when its path or any ancestor up to
  the filesystem root contains ``.git``, or when it lies inside or around a
  protected user ``source_root``.
- A managed target whose own path, or an intermediate directory under its
  managed home, is a symlink/junction is skipped before any trash sweep or
  handler runs; the external target is never read or deleted.
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
- A caller-supplied ``config_home``/``data_home``/``codex_home`` scopes the
  sweep to those roots: the real user's ``~`` cache roots are added only in
  default production mode, never for an isolated test or IPC prune.
- A cleanup failure never becomes a primary-operation failure and no marker or
  tombstone suppresses a later honest retry.

[Timeouts & Locks]
- One absolute deadline bounds the sweep; every managed target gets a bounded
  slice, and every scan/deletion loop checks the deadline entry by entry and
  stops immediately on expiry, reporting ``partial``. No lock is waited on;
  busy leases and builder locks only cause a keep. The cooldown marker is
  advisory (600 s) and is bypassed by ``force=True`` or leftover ``.trash-*``.

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
    MARKER_SCHEMA, _check, _config_documents, _config_homes, _data_homes,
    _discover_targets, _entries, _env, _home, _read_json, _strings,
    collect_global_protected_roots, owning_dir, protected_source_roots,
)
from .lifecycle import (
    _builder_locked, _expired, has_git_ancestor, is_junction,
    is_own_runtime_path, is_protected_path, is_within_roots,
    prune_component_runtimes, retire_directory, sweep_trash,
)

COOLDOWN_SECONDS, STAGING_GRACE_SECONDS = 600.0, 1800.0
VERIFIED_RETENTION = 2
RECEIPT_RETENTION = SUPERSEDED_RETENTION = 8
LOG_GRACE_SECONDS, STATE_MARKER = 3600.0, ".storage-gc-state.json"
_SHA = re.compile(r"[0-9a-f]{40,64}")


# --------------------------------------------------------------------------- #
# Managed targets: only fixed MindIE-owned storage directories.
# --------------------------------------------------------------------------- #

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


def _target_has_link(path: Path, stop_at: Path | None = None) -> bool:
    """True when ``path`` or an intermediate directory up to ``stop_at`` is a link."""
    current = Path(os.path.abspath(os.fspath(path)))
    if stop_at is None:
        return _is_link(current)
    stop = Path(os.path.abspath(os.fspath(stop_at)))
    while True:
        if _is_link(current):
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


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
    return has_git_ancestor(entry)


def _protected_leaf(entry: Path, forbidden_roots) -> bool:
    """True when ``entry`` is a Git tree or inside a protected source root.

    Every leaf deletion in this module goes through this gate. ``has_git_ancestor``
    covers ordinary checkouts, ``lexists`` catches a dangling ``.git`` link, and
    the explicit ``.git``/``RemoteReceipts`` component checks keep the metadata
    tree itself (and retry receipts) undeletable even when passed directly.
    """
    candidate = Path(entry)
    if ".git" in candidate.parts or "RemoteReceipts" in candidate.parts:
        return True
    if os.path.lexists(filesystem_path(candidate / ".git")):
        return True
    if _has_git_ancestor(candidate):
        return True
    if not forbidden_roots:
        return False
    return (is_within_roots(candidate, forbidden_roots)
            or is_protected_path(candidate, forbidden_roots))


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
    if is_within_roots(entry, forbidden_roots) \
            or is_protected_path(entry, forbidden_roots):
        account["kept_dirs"] += 1
        account["warnings"].append("protected source root: " + entry.name[:32])
        return False
    if _has_git_ancestor(entry) or ".git" in entry.parts \
            or os.path.lexists(filesystem_path(entry / ".git")) \
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
                           and not is_protected_path(entry, forbidden_roots)
                           and not _has_git_ancestor(entry)
                           and not os.path.lexists(
                               filesystem_path(entry / ".git")))
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


def _unlink_file(entry: Path, account, deadline=None, forbidden_roots=()) -> None:
    if _expired(deadline):
        account["partial"] = True
        account["warnings"].append("budget exhausted")
        return
    if _protected_leaf(entry, forbidden_roots):
        account["warnings"].append("protected file: " + entry.name[:32])
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
    if _is_link(target["path"]):
        return
    directory = target["path"]
    recent = 1 if target.get("harness") == "kimi" else VERIFIED_RETENTION
    entries = [entry for entry in _entries(directory, deadline=deadline)
               if entry.is_dir() and entry.name != "bootstrap"]
    kept = _sweep_ranked(entries, protected, live, complete, deadline, account,
                         recent=recent, marker=_verified_generation,
                         forbidden_roots=forbidden_roots)
    for entry in kept:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        # A kept entry may itself be a symlink/junction selected by retention.
        # Never follow it into an external target to unlink its asset.
        if _is_link(entry) or not entry.is_dir():
            continue
        if _protected_leaf(entry, forbidden_roots):
            # A kept generation may itself be the configured source root or a
            # Git checkout: its extracted archive is not disposable either.
            account["warnings"].append(
                "protected generation: " + entry.name[:32])
            continue
        asset = entry / "asset.tgz"
        if is_junction(asset) or not (asset.is_symlink() or asset.is_file()):
            continue
        if (entry / "package").is_dir() or (entry / ".venv").is_dir():
            _unlink_file(asset, account, deadline, forbidden_roots)


def _sweep_launch(target, protected, live, complete, deadline, account,
                  forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    directory = target["path"]
    generations = directory.parent / "generations"
    shas = {entry.name for entry in _entries(generations, deadline=deadline)
            if entry.is_dir()}
    for entry in _entries(directory, deadline=deadline):
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
    if _is_link(target["path"]):
        return
    files = [entry for entry in _entries(target["path"], deadline=deadline) if entry.suffix == ".json"
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
        _unlink_file(entry, account, deadline, forbidden_roots)


def _sweep_runtimes(target, protected, live, complete, deadline, account,
                    forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    entries = [entry for entry in _entries(target["path"], deadline=deadline)
               if entry.is_dir() and entry.name.casefold() != "cc"]
    marker = lambda entry: _is_link(entry) or any(
        (entry / name).exists()
        for name in ("runtime.json", "venv", ".venv", "Scripts", "bin"))
    _sweep_ranked(entries, protected, live, complete, deadline, account, recent=1,
                  marker=marker, forbidden_roots=forbidden_roots)


def _sweep_cc_runtimes(target, protected, live, complete, deadline, account,
                       forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    entries = [entry for entry in _entries(target["path"], deadline=deadline) if entry.is_dir()]
    marker = lambda entry: entry.with_name(entry.name + ".ready.json").is_file()
    kept = {entry.name for entry in _sweep_ranked(
        entries, protected, live, complete, deadline, account,
        recent=VERIFIED_RETENTION, marker=marker,
        forbidden_roots=forbidden_roots)}
    for entry in _entries(target["path"], deadline=deadline):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if entry.suffix == ".json" and entry.name.endswith(".ready.json"):
            owner = entry.name[: -len(".ready.json")]
            if owner not in kept and not (entry.parent / owner).is_dir() \
                    and _mtime(entry) < time.time() - 86400:
                _unlink_file(entry, account, deadline, forbidden_roots)


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
    if _is_link(target["path"]):
        return
    directory = target["path"]
    active = target.get("active_versions") or set()
    entries = [entry for entry in _entries(directory, deadline=deadline) if entry.is_dir()
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
    for entry in _entries(directory, deadline=deadline):
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        if entry.is_file() and entry.name.startswith("prepare-") \
                and entry.suffix == ".log" \
                and _mtime(entry) < time.time() - LOG_GRACE_SECONDS:
            _unlink_file(entry, account, deadline, forbidden_roots)


def _sweep_newest(target, protected, live, complete, deadline, account,
                  forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    entries = [entry for entry in _entries(target["path"], deadline=deadline) if entry.is_dir()]
    _sweep_ranked(entries, protected, live, complete, deadline, account,
                  recent=VERIFIED_RETENTION, marker=lambda entry: True,
                  forbidden_roots=forbidden_roots)


def _sweep_engines(target, protected, live, complete, deadline, account,
                   runtime_names: set[str], configs_text: str,
                   forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    files = [entry for entry in _entries(target["path"], deadline=deadline) if entry.suffix == ".json"
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
        _unlink_file(entry, account, deadline, forbidden_roots)


def _sweep_oc_engines(target, protected, live, complete, deadline, account,
                      forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    files = [entry for entry in _entries(target["path"], deadline=deadline)
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
        _unlink_file(entry, account, deadline, forbidden_roots)


def _sweep_superseded(target, protected, live, complete, deadline, account,
                      forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    files = [entry for entry in _entries(target["path"], deadline=deadline)
             if entry.is_file() and entry.name.startswith("superseded-")]
    files.sort(key=lambda entry: (_mtime(entry), entry.name), reverse=True)
    for entry in files[SUPERSEDED_RETENTION:]:
        if _expired(deadline):
            account["partial"] = True
            account["warnings"].append("budget exhausted")
            return
        _unlink_file(entry, account, deadline, forbidden_roots)


def _sweep_uv_cache(target, protected, live, complete, deadline, account,
                    configs_text: str, forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
    entry = target["path"]
    if is_path_used_by_live_process(entry, live) or "uv-cache" in configs_text \
            or _mtime(entry) > time.time() - 86400:
        account["kept_dirs"] += 1
        return
    _retire_path(entry, deadline, account, protected=protected, live=live,
                 complete=complete, forbidden_roots=forbidden_roots)


def _sweep_component(target, protected, live, complete, deadline, account,
                     forbidden_roots=()) -> None:
    if _is_link(target["path"]):
        return
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
    if _is_link(target["path"]):
        return
    entry = target["path"]
    if _keep(entry, protected, live):
        account["kept_dirs"] += 1
        return
    if _protected_leaf(entry, forbidden_roots):
        # ``_retire_path`` repeats this gate; keeping it here means a single
        # target that is itself a source root or Git checkout is never even
        # considered for the two-phase rename.
        account["kept_dirs"] += 1
        account["warnings"].append("protected single: " + entry.name[:32])
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


def _leftover_trash(targets, deadline=None) -> bool:
    """Whether any target holds ``.trash-*``; raises ``TimeoutError`` on expiry."""
    for target in targets:
        try:
            with os.scandir(filesystem_path(target["path"])) as iterator:
                for item in iterator:
                    _check(deadline)
                    if item.name.startswith(".trash-"):
                        return True
        except TimeoutError:
            raise
        except OSError:
            continue
    return False


def _sweep_target_trash(target, deadline, account, forbidden_roots=()) -> None:
    result = sweep_trash(target["path"], deadline=deadline,
                         forbidden_roots=forbidden_roots)
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
    # Explicit scoped roots mean an isolated caller (tests, an IPC prune).
    # Only default production mode may scan the real user's ``~`` roots.
    include_home_roots = all(value is None for value in
                             (config_home, data_home, codex_home))
    stop = False
    try:
        targets = _discover_targets(config_homes, data_homes, codex,
                                    include_home_roots=include_home_roots,
                                    deadline=limit)
    except TimeoutError:
        targets = []
        account["partial"], account["inspection_complete"] = True, False
        account["warnings"].append("budget exhausted")
        stop = True
    marker = data_homes[0] / STATE_MARKER
    state = _read_json(marker)
    recent = isinstance(state, dict) and isinstance(state.get("at"), (int, float)) \
        and time.time() - state["at"] < COOLDOWN_SECONDS
    if not stop and not force and recent:
        try:
            leftover = _leftover_trash(targets, deadline=limit)
        except TimeoutError:
            leftover, stop = True, True
            account["partial"], account["inspection_complete"] = True, False
            account["warnings"].append("budget exhausted")
        if not leftover:
            account["status"] = "skipped_cooldown"
            account["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return account
    if not stop and _expired(limit):
        account["partial"] = True
        account["warnings"].append("budget exhausted")
        stop = True
    if not stop:
        try:
            live_paths, complete = live_process_prefixes(
                timeout=max(0.1, min(2.0, limit - time.monotonic())))
            account["inspection_complete"] = complete
            protected = collect_global_protected_roots(
                config_home=config_homes, data_home=data_homes, codex_home=codex,
                live_paths=live_paths, deadline=limit)
            forbidden_roots = protected_source_roots(config_home=config_homes,
                                                      deadline=limit)
            documents = _config_documents(config_homes, deadline=limit)
        except TimeoutError:
            account["partial"], account["inspection_complete"] = True, False
            account["warnings"].append("protected roots budget exhausted")
            stop = True
    if not stop:
        account["protected_roots"] = len(protected)
        configs_text = " ".join(" ".join(_strings(document[1]))
                                for document in documents)
        runtime_names: set[str] = set()
        try:
            for target in targets:
                if target["kind"] in {"runtimes", "cc-runtimes"}:
                    runtime_names.update(entry.name for entry in
                                         _entries(target["path"], deadline=limit))
        except TimeoutError:
            account["partial"] = True
            account["warnings"].append("budget exhausted")
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
            if is_within_roots(target["path"], forbidden_roots):
                account["kept_dirs"] += 1
                account["warnings"].append(
                    "protected source root: " + target["kind"])
                continue
            if _target_has_link(target["path"], target.get("root")) \
                    or has_git_ancestor(target["path"]):
                account["kept_dirs"] += 1
                account["warnings"].append("protected target: " + target["kind"])
                continue
            remaining = max(0.1, min(2.0, limit - time.monotonic()))
            target_deadline = min(limit, time.monotonic() + remaining)
            try:
                _sweep_target_trash(target, target_deadline, account,
                                    forbidden_roots)
                handler = handlers.get(target["kind"])
                if handler is not None:
                    handler(target, protected, live_paths, complete,
                            target_deadline, account, forbidden_roots)
            except TimeoutError:
                account["deferred_dirs"] += 1
                account["partial"] = True
                account["warnings"].append("timeout: " + target["kind"])
                break
            except Exception as exc:  # noqa: BLE001 - cleanup must never brick a caller
                account["warnings"].append(
                    target["kind"] + ": " + type(exc).__name__)
    try:
        account["trash_remaining"] = sum(
            1 for target in targets
            for entry in _entries(target["path"], deadline=limit)
            if entry.name.startswith(".trash-"))
    except TimeoutError:
        account["partial"] = True
        account["warnings"].append("budget exhausted")
    account["status"] = "partial" if account["partial"] else (
        "inspection_incomplete" if not account["inspection_complete"]
        else "completed")
    account["elapsed_seconds"] = round(time.monotonic() - started, 3)
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
