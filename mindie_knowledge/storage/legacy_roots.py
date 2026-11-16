"""[Role] Protected-pin discovery and bounded managed-target enumeration for
historical MindIE runtime baggage.

[Invariants]
- ``source_root``/``package_dir`` and every other configuration value are read
  only to add protected pins. They are never deletion scan roots.
- Structured host files are parsed with their real parser: JSON via
  ``json.loads``, macOS plists via ``plistlib``, Windows XML via
  ``ElementTree``, Python via ``ast`` string constants, and shells via
  ``shlex`` plus quoted/unquoted extraction. Whitespace regexes are only a
  syntax-error fallback, so spaces and CJK characters survive.
- Production defaults union the ``MINDIE_CONFIG_HOME``/``MINDIE_DATA_HOME``
  overrides with the standard platform roots, because a sibling harness may
  still write the default location. Explicit test roots stay scoped.
- Directory junctions are followed only for a read-only protection pin, never
  for a deletion target.

[Timeouts & Locks]
- Every reader accepts an absolute ``deadline``; expiry raises ``TimeoutError``
  so a caller can report a partial sweep instead of deleting with incomplete
  protection. Recursive scanning is an explicit iterative ``os.scandir`` walk
  that checks the deadline before every entry (no unbounded ``rglob`` /
  ``**`` glob), so a huge plugin tree cannot overrun a startup budget. No lock
  is acquired here.

[Side-Effect Policy]
- Read-only inspection of configuration, launcher and package metadata. Never
  creates, renames, or deletes a file.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import sys
import time
from xml.etree import ElementTree

from ..filesystem import filesystem_path
from .lifecycle import _expired, is_junction

MARKER_SCHEMA = "mindie-storage-gc/1"
HARNESSES = ("codex", "cc", "kimi", "dsh", "agy", "wb", "oc")
CONFIG_FILES = (
    "codex.json", "cc.json", "kimi.json", "agy.json", "wb.json", "oc.json",
    "diagnostics.json", "oc.engine.json", "codex.engine.json", "cc.engine.json",
    "kimi.engine.json", "agy.engine.json", "wb.engine.json", "dsh/config.json",
    "dsh/engine.json", "codex.updates/native.json")
_MARKERS = frozenset({
    "component-runtimes", "generations", "launch", "receipts", "installations",
    "releases", "runtimes", "engines"})
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s\"'`<>|;&,()\[\]]*")
_QUOTED = re.compile(r'"([^"\n]+)"|\'([^\'\n]+)\'')
_SHA = re.compile(r"[0-9a-f]{40,64}")
_PATH_PUNCTUATION = ".,;:'\")]}>"
_MAX_WALK_DEPTH = 12
_WORKBUDDY_FILES = ("mcp.json", ".mcp.json", "hooks.json")


def _check(deadline) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("protected-root scan budget exhausted")


def _scandir_walk(root: Path, *, deadline=None, max_depth: int = _MAX_WALK_DEPTH):
    """Yield every ``(path, is_dir)`` under ``root`` without recursion.

    This replaces unbounded ``Path.glob("**/*")`` recursion: the deadline is
    checked before every single entry, so a huge plugin or marketplace tree can
    never overrun the caller's protected-pin budget. Directory links/junctions
    are followed read-only (a protection pin may live behind one) with a
    visited ``(st_dev, st_ino)`` guard against cycles and a hard depth cap.
    A filesystem without usable inode identity (Windows ``DirEntry``, FAT/
    exFAT) relies on the depth cap instead of treating ``(0, 0)`` as a cycle.
    """
    base = Path(root)
    try:
        if not base.is_dir():
            return
        root_info = os.stat(filesystem_path(base))
    except TimeoutError:
        raise
    except OSError:
        return
    visited = {(root_info.st_dev, root_info.st_ino)}
    stack = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(filesystem_path(directory)) as iterator:
                for item in iterator:
                    _check(deadline)
                    child = directory / item.name
                    try:
                        is_dir = item.is_dir(follow_symlinks=True)
                    except TimeoutError:
                        raise
                    except OSError:
                        is_dir = False
                    yield child, is_dir
                    if not is_dir or depth + 1 >= max_depth:
                        continue
                    try:
                        # ``DirEntry.stat`` reports ``(0, 0)`` for every
                        # directory on Windows; a real ``os.stat`` populates
                        # the NTFS FileIndex/volume serial identity instead.
                        info = os.stat(filesystem_path(child))
                    except TimeoutError:
                        raise
                    except OSError:
                        continue
                    key = (info.st_dev, info.st_ino)
                    if key != (0, 0):
                        # FAT/exFAT may expose no usable inode identity. Never
                        # treat that as a cycle: bound the walk by max_depth.
                        if key in visited:
                            continue
                        visited.add(key)
                    stack.append((child, depth + 1))
        except TimeoutError:
            raise
        except OSError:
            continue


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _unique(paths) -> list[Path]:
    result, seen = [], set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key not in seen:
            seen.add(key)
            result.append(Path(path))
    return result


def _explicit_homes(value) -> list[Path]:
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return _unique(Path(item).expanduser() for item in items)


def _config_homes(config_home) -> list[Path]:
    if config_home is not None:
        return _explicit_homes(config_home)
    values = [_home() / ".config" / "mindie-agent"]
    override = _env("MINDIE_CONFIG_HOME")
    if override:
        values.insert(0, Path(override))
    if os.name == "nt":
        for name in ("APPDATA", "LOCALAPPDATA"):
            base = _env(name)
            if base:
                values.append(Path(base) / "mindie-agent")
        values.append(_home() / ".config" / "mindie-agent")
    return _unique(values)


def _data_homes(data_home) -> list[Path]:
    if data_home is not None:
        return _explicit_homes(data_home)
    values = [_home() / ".local" / "share" / "mindie-agent"]
    override = _env("MINDIE_DATA_HOME")
    if override:
        values.insert(0, Path(override))
    if os.name == "nt":
        base = _env("LOCALAPPDATA")
        if base:
            values.append(Path(base) / "mindie-agent")
        values.append(_home() / ".local" / "share" / "mindie-agent")
    return _unique(values)


def _state_roots(config_homes, data_homes, codex_home) -> list[tuple[str, Path, Path]]:
    roots: list[tuple[str, Path, Path]] = []
    for data in data_homes:
        roots.append(("data", data, data))
        for name in ("kimi", "cc", "dsh", "wb-state", "agy-state", "oc-state",
                     "runtimes"):
            roots.append((name, data / name, data))
        roots.extend((("kimi", data / "kimi" / "adapter-state", data),
                      ("cc", data / "cc" / "adapter-state", data)))
    for config in config_homes:
        roots.extend((("config", config, config), ("kimi", config / "state", config)))
        for name in ("kimi", "cc", "dsh", "agy", "wb", "oc"):
            roots.append((name, config / name, config))
        roots.extend((("dsh", config / "diagnostics.runtime", config),
                      ("dsh", config / "dsh" / "engines", config),
                      ("oc", config / "generations", config)))
    codex_data = Path(codex_home) / "plugins" / "data" / "mindie-agent-mindie-agent"
    roots.extend((("codex", codex_data, Path(codex_home)),
                  ("codex", codex_data / "codex.updates", Path(codex_home))))
    for config in config_homes:
        roots.append(("codex", config / "codex.updates", config))
    return roots


def _add_target(targets, seen, kind, path, harness=None, root=None) -> None:
    path = Path(os.path.normpath(str(path)))
    key = os.path.normcase(os.path.abspath(str(path)))
    if key not in seen:
        seen.add(key)
        targets.append({"kind": kind, "path": path, "harness": harness,
                        "root": Path(root) if root is not None else None})


def _discover_targets(config_homes, data_homes, codex_home, *,
                      include_home_roots: bool = True,
                      deadline: float | None = None) -> list[dict]:
    """Managed scan roots, bounded by ``deadline`` and scoped to explicit homes."""
    targets: list[dict] = []
    seen: set[str] = set()
    for harness, root, home in _state_roots(config_homes, data_homes, codex_home):
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
            _check(deadline)
            if (root / relative).is_dir():
                _add_target(targets, seen, kind, root / relative, harness, home)
        if root.name.casefold() == "dsh" and (root / "venv").is_dir():
            _check(deadline)
            _add_target(targets, seen, "dsh-venv", root / "venv", harness, home)
    for config in config_homes:
        _check(deadline)
        diagnostics = config / "diagnostics.runtime"
        if (diagnostics / "generations").is_dir():
            _add_target(targets, seen, "generations",
                        diagnostics / "generations", "diagnostics", config)
        updates = config / "codex.updates"
        if updates.is_dir():
            _add_target(targets, seen, "superseded", updates, "codex", config)
        generations = config / "generations"
        if generations.is_dir() and any(
                entry.name.startswith("oc.engine.") and entry.name.endswith(".json")
                for entry in _entries(generations, deadline=deadline)):
            _add_target(targets, seen, "oc-engines", generations, "oc", config)
    if include_home_roots:
        _check(deadline)
        if (uv_cache := _home() / ".cache" / "mindie-agent-dsh" / "uv-cache").is_dir():
            _add_target(targets, seen, "uv-cache", uv_cache, "dsh", _home())
    return targets


def _entries(directory, *, deadline=None) -> list[Path]:
    """Sorted direct children; every entry checks the caller deadline.

    A 10,000-entry generation directory must not materialize past the sweep
    budget, so expiry raises ``TimeoutError`` (the per-target handler reports
    it as ``partial``) instead of returning a truncated ranking.
    """
    base = Path(directory)
    entries: list[Path] = []
    try:
        with os.scandir(filesystem_path(base)) as iterator:
            for item in iterator:
                if _expired(deadline):
                    raise TimeoutError("storage scan budget exhausted")
                entries.append(base / item.name)
    except TimeoutError:
        raise
    except OSError:
        return []
    entries.sort(key=lambda item: item.name)
    return entries


def _read_value(path, *, limit=2 * 1024 * 1024, deadline=None):
    _check(deadline)
    try:
        if os.stat(filesystem_path(path)).st_size > limit:
            return None
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return None
    _check(deadline)
    return value


def _read_json(path, *, limit=2 * 1024 * 1024, deadline=None):
    value = _read_value(path, limit=limit, deadline=deadline)
    return value if isinstance(value, dict) else None


def _read_text(path, *, limit=4 * 1024 * 1024, deadline=None) -> str | None:
    _check(deadline)
    try:
        if os.stat(filesystem_path(path)).st_size > limit:
            return None
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except TimeoutError:
        raise
    except OSError:
        return None
    _check(deadline)
    return text


def _strings(value, depth=0):
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)


def _looks_absolute(text: str) -> bool:
    if not text:
        return False
    if os.name == "nt":
        return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"))
    return text.startswith("/")


def _quoted_tokens(text: str):
    for match in _QUOTED.finditer(text):
        value = match.group(1) or match.group(2)
        if value:
            yield value


def _path_tokens(text: str) -> list[str]:
    tokens = [text] if _looks_absolute(text) else []
    tokens.extend(match.group(0).rstrip(_PATH_PUNCTUATION)
                  for match in _ABSOLUTE.finditer(text))
    tokens.extend(value for value in _quoted_tokens(text)
                  if _looks_absolute(value.strip()))
    return [token for token in dict.fromkeys(tokens) if len(token) > 2]


def _shell_strings(text: str):
    """Shell tokens plus quoted/unquoted line extraction, never a GUI prompt."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parts = shlex.split(stripped, posix=True)
        except ValueError:
            parts = []
        for part in parts:
            yield part
        yield from _quoted_tokens(stripped)
        yield from _path_tokens(stripped)


def _python_strings(text: str):
    """Every string literal in a Python launcher; quote fallback on syntax error."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        yield from _quoted_tokens(text)
        for line in text.splitlines():
            yield from _path_tokens(line)
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _plist_strings(text: str):
    try:
        value = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - malformed host plist is a text fallback
        yield from _quoted_tokens(text)
        return
    yield from _strings(value)


def _xml_strings(text: str):
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        yield from _quoted_tokens(text)
        return
    for element in root.iter():
        for value in element.attrib.values():
            yield value
        for value in (element.text, element.tail):
            if isinstance(value, str) and value.strip():
                yield value.strip()


def owning_dir(value) -> Path | None:
    """Map a referenced child path back to its owning disposable generation."""
    if not isinstance(value, str):
        return None
    text = value.strip().strip('"').strip("'")
    if not text or not _looks_absolute(text):
        return None
    parts = Path(os.path.normpath(text)).parts
    for index, part in enumerate(parts):
        lower = part.casefold()
        if lower in {"bootstrap-venv", "bootstrap-package"}:
            return Path(*parts[:index + 1])
        if lower in _MARKERS:
            end = index + 1
            if lower == "runtimes" and index + 2 < len(parts) \
                    and parts[index + 1].casefold() == "cc":
                end = index + 3
            elif index + 1 < len(parts):
                end = index + 2
            return Path(*parts[:end])
        if lower.endswith("-source") and index + 1 < len(parts):
            return Path(*parts[:index + 2])
        if lower == "venv" and index > 0 and parts[index - 1].casefold() == "dsh":
            return Path(*parts[:index + 1])
    return None


def _add_text(protected: set[Path], text: str) -> None:
    for token in _path_tokens(text):
        if (owner := owning_dir(token)) is not None:
            protected.add(owner)
        protected.add(Path(os.path.normpath(token)))
    if (whole := owning_dir(text)) is not None:
        protected.add(whole)


def _add_document(protected: set[Path], value) -> None:
    for text in _strings(value):
        _add_text(protected, text)


def _config_documents(config_homes, *, deadline=None) -> list[tuple[Path, dict]]:
    documents = []
    for base in config_homes:
        _check(deadline)
        for relative in CONFIG_FILES:
            if (value := _read_json(base / relative, deadline=deadline)) is not None:
                documents.append((base / relative, value))
    return documents


def _update_pointers(protected: set[Path], data_homes, config_homes, *,
                     deadline=None) -> None:
    candidates = []
    for data in data_homes:
        candidates.extend((data / "kimi" / "adapter-state" / "update",
                           data / "kimi" / "update", data / "dsh" / "update",
                           data / "update"))
    for config in config_homes:
        candidates.extend((config / "state" / "update", config / "dsh" / "update"))
    for update in _unique(candidates):
        _check(deadline)
        current = _read_json(update / "current.json", deadline=deadline)
        if current is None:
            continue
        _add_document(protected, current)
        sha = current.get("sha") or current.get("generation_id")
        if isinstance(sha, str) and _SHA.fullmatch(sha):
            protected.update((update / "generations" / sha, update / "launch" / sha))
        state = update.parent.parent
        for name in ("bootstrap-venv", "bootstrap-package"):
            if (state / name).is_dir():
                protected.add(state / name)
        previous = (_read_json(update / "receipts" / (str(sha) + ".json"),
                               deadline=deadline) or {}).get("previous") \
            if sha else None
        if previous is not None:
            _add_document(protected, previous)
            previous_sha = previous.get("sha") if isinstance(previous, dict) else None
            if isinstance(previous_sha, str) and _SHA.fullmatch(previous_sha):
                protected.update((update / "generations" / previous_sha,
                                  update / "launch" / previous_sha))
        # DSH's recovery path requires the immediately previous generation.
        previous = current.get("previous_generation")
        if isinstance(previous, str):
            protected.add(Path(os.path.normpath(previous)))
            if (owner := owning_dir(previous)) is not None:
                protected.add(owner)


def _diagnostics_pointers(protected: set[Path], config_homes, *,
                          deadline=None) -> None:
    for config in config_homes:
        for root in (config / "diagnostics.runtime", config / "diagnostics.json.runtime"):
            _check(deadline)
            current = _read_json(root / "current.json", deadline=deadline)
            if not current:
                continue
            for name in ("source_hash", "previous_source_hash"):
                value = current.get(name)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16,64}", value):
                    protected.add(root / "generations" / value)
            _add_document(protected, current)


def _add_file(protected: set[Path], path: Path, *, deadline=None) -> None:
    """Parse one host file by its real format instead of whitespace tokens."""
    _check(deadline)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if suffix == ".json":
        value = _read_value(path, deadline=deadline)
        if value is not None:
            _add_document(protected, value)
            return
    text = _read_text(path, deadline=deadline)
    if text is None:
        return
    if suffix == ".plist":
        _add_document(protected, list(_plist_strings(text)))
    elif suffix == ".xml":
        _add_document(protected, list(_xml_strings(text)))
    elif suffix in {".py", ".pyw"}:
        _add_document(protected, list(_python_strings(text)))
    elif name.endswith(".json"):
        _add_document(protected, list(_quoted_tokens(text)))
    else:
        _add_document(protected, list(_shell_strings(text)))


def _scan_claude(protected: set[Path], *, deadline=None) -> None:
    roots = [_home() / ".claude" / "plugins" / "data"]
    if explicit := _env("CLAUDE_PLUGIN_DATA"):
        roots.insert(0, Path(explicit).expanduser())
    for root in roots:
        _check(deadline)
        entries: list[Path] = []
        for path, is_dir in _scandir_walk(root, deadline=deadline, max_depth=5):
            if is_dir or path.name != "entry.py":
                continue
            parts = path.relative_to(root).parts
            if (len(parts) == 3 and parts[0] == "releases") \
                    or (len(parts) == 4 and parts[1] == "releases"):
                entries.append(path)
        for entry in entries:
            _add_file(protected, entry, deadline=deadline)


def _scan_kimi(protected: set[Path], config_homes, data_homes, *,
               deadline=None) -> None:
    home = _home()
    kimi_home = Path(_env("KIMI_CODE_HOME") or home / ".kimi-code").expanduser()
    paths = [
        kimi_home / "plugins" / "managed" / "mindie-agent" / "mindie-front",
        kimi_home / "plugins" / "managed" / "mindie-agent" / "kimi.plugin.json",
        kimi_home / "plugins" / "installed.json",
        home / "Library" / "LaunchAgents" / "agent.mindie.kimi-update.plist",
    ]
    for base in _unique([*data_homes, *config_homes,
                         *[item / "state" for item in config_homes]]):
        paths.extend(base / "update" / name
                     for name in ("MindIEKimiUpdate.pyw", "MindIEKimiUpdate.xml"))
    for _path, value in _config_documents(config_homes, deadline=deadline):
        if isinstance(value.get("state_dir"), str) and _looks_absolute(value["state_dir"]):
            state = Path(value["state_dir"])
            paths.extend(state / "update" / name
                         for name in ("MindIEKimiUpdate.pyw", "MindIEKimiUpdate.xml"))
    for path in paths:
        _add_file(protected, path, deadline=deadline)


def _scan_dsh(protected: set[Path], *, deadline=None) -> None:
    profiles = _home() / ".dsh" / "profiles"
    for path, is_dir in _scandir_walk(profiles, deadline=deadline, max_depth=4):
        relative = path.relative_to(profiles).parts
        if not is_dir and len(relative) == 2 and relative[1] == "package.json":
            value = _read_value(path, deadline=deadline)
            if value is not None:
                _add_document(protected, value)
        elif 3 <= len(relative) <= 4 and relative[1] == "node_modules":
            _add_dsh_link(protected, path)


def _add_dsh_link(protected: set[Path], link: Path) -> None:
    try:
        if not (link.is_symlink() or is_junction(link)):
            return
        target = Path(os.path.realpath(link))
    except TimeoutError:
        raise
    except OSError:
        return
    parts = {part.casefold() for part in target.parts} if target else set()
    if {"mindie-agent", "dsh"} <= parts:
        protected.add(target)
        if (owner := owning_dir(str(target))) is not None:
            protected.add(owner)


def _scan_workbuddy(protected: set[Path], *, deadline=None) -> None:
    root = _home() / ".workbuddy" / "plugins"
    paths: list[Path] = []
    for area in ("cache", "marketplaces"):
        base = root / area
        for path, is_dir in _scandir_walk(base, deadline=deadline):
            if is_dir or path.name not in _WORKBUDDY_FILES:
                continue
            if "mindie-agent" in path.relative_to(base).parts[:-1]:
                paths.append(path)
    if (known := root / "known_marketplaces.json").is_file():
        paths.append(known)
    for path in paths:
        _add_file(protected, path, deadline=deadline)


def _config_protected_roots(config_homes, *, deadline=None) -> set[Path]:
    """Config values are protected pins only; they are never scan roots."""
    roots: set[Path] = set()
    for path, value in _config_documents(config_homes, deadline=deadline):
        for name in ("state_dir", "root", "venv_dir", "package_dir", "source_root"):
            item = value.get(name)
            if isinstance(item, str) and _looks_absolute(item):
                roots.add(Path(os.path.normpath(item)))
        engine = value.get("engine_config")
        if isinstance(engine, str) and _looks_absolute(engine):
            engine_value = _read_json(engine, deadline=deadline) or {}
            if isinstance(engine_value.get("root"), str):
                roots.add(Path(os.path.normpath(engine_value["root"])))
    return roots


def protected_source_roots(*, config_home=None, deadline=None) -> set[Path]:
    """Configured source/package trees that no candidate may be retired inside.

    ``source_root`` and ``package_dir`` are user pins. A candidate *equal to or
    inside* one of these trees is protected even when the tree has no ``.git``
    marker: an exported source tree with ``component-runtimes/old`` inside it
    must never be reclaimed.
    """
    roots: set[Path] = set()
    for _path, value in _config_documents(_config_homes(config_home),
                                          deadline=deadline):
        for name in ("source_root", "package_dir"):
            item = value.get(name)
            if isinstance(item, str) and _looks_absolute(item):
                roots.add(Path(os.path.normpath(item)))
    return roots


def collect_global_protected_roots(*, config_home=None, data_home=None,
                                   codex_home=None, live_paths=None,
                                   deadline=None):
    """Union every authority that may still reference a disposable generation."""
    config_homes = _config_homes(config_home)
    data_homes = _data_homes(data_home)
    codex = Path(codex_home or _env("CODEX_HOME")
                 or _home() / ".codex").expanduser()
    protected: set[Path] = {
        Path(sys.executable), Path(sys.prefix),
        Path(getattr(sys, "base_prefix", sys.prefix)),
    }
    for document in _config_documents(config_homes, deadline=deadline):
        _add_document(protected, document[1])
    protected.update(_config_protected_roots(config_homes, deadline=deadline))
    bases = _unique([*config_homes, *data_homes, codex,
                     *[item / "state" for item in config_homes],
                     *[data / name for data in data_homes for name in HARNESSES]])
    for base in bases:
        _check(deadline)
        for name in ("bootstrap-venv", "bootstrap-package"):
            if (base / name).is_dir():
                protected.add(base / name)
    _update_pointers(protected, data_homes, config_homes, deadline=deadline)
    _diagnostics_pointers(protected, config_homes, deadline=deadline)
    _scan_claude(protected, deadline=deadline)
    _scan_kimi(protected, config_homes, data_homes, deadline=deadline)
    _scan_dsh(protected, deadline=deadline)
    _scan_workbuddy(protected, deadline=deadline)
    return {Path(os.path.normpath(str(item))) for item in protected}


__all__ = [
    "CONFIG_FILES",
    "HARNESSES",
    "MARKER_SCHEMA",
    "collect_global_protected_roots",
    "owning_dir",
    "protected_source_roots",
]
