"""Bounded, explicit source inspection; no service, network or model lifecycle."""
from __future__ import annotations

import hashlib
from collections import defaultdict
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

SCHEMA = 1
PARSER_VERSION = 5
EXTENSIONS = {".py": "python", ".pyi": "python", ".c": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".h": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".cuh": "cpp"}
SKIP_DIRS = {".git", ".mindie-local", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
DEFAULT_LIMITS = {"max_files": 3000, "max_bytes": 64 * 1024 * 1024,
                  "max_file_bytes": 2 * 1024 * 1024, "max_seconds": 30.0}


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".map-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _git(root: Path, *args: str, check=True) -> bytes:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if check:
            raise ValueError(f"Local Git source unavailable: {exc}") from exc
        return b""
    if check and result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()[:500])
    return result.stdout if result.returncode == 0 else b""


def _fingerprint(language: str) -> str:
    versions = [str(PARSER_VERSION), language]
    if language == "python":
        versions.append(f"{sys.version_info.major}.{sys.version_info.minor}")
    else:
        # Pre-guard C++ caches must not bypass the native-version policy.
        # Keep Python fingerprints unchanged so their valid work is reused.
        versions.append("native-version-policy-1")
        for package in ("tree-sitter", "tree-sitter-cpp"):
            try:
                version = importlib.metadata.version(package)
                versions.append(version if isinstance(version, str) and version else "unavailable")
            except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
                versions.append("unavailable")
    return "/".join(versions)


def _scope(paths):
    result = []
    for path in paths or ():
        raw = str(path).replace("\\", "/").strip("/")
        if not raw or raw == ".":
            continue
        if ".." in raw.split("/") or ":" in raw or str(path).startswith(("/", "\\")):
            raise ValueError("paths must be relative paths within the explicit source root")
        result.append(raw.rstrip("/"))
    return sorted(set(result))


def _wanted(path, scopes):
    return not scopes or any(path == scope or path.startswith(scope + "/") for scope in scopes)


def _repositories(root):
    """Only public-shaped repository coordinates, never arbitrary remote URLs."""
    raw = _git(root, "config", "--get-regexp", r"^remote\..*\.url$", check=False).decode("utf-8", errors="replace")
    names = set()
    for line in raw.splitlines():
        value = line.split(None, 1)[-1]
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", value)
        if match:
            names.add(match.group(1))
    return sorted(names)


def _sources(root, commit, scopes, max_files, deadline):
    files, gaps = [], []
    if commit:
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        prefix = root.relative_to(top).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        raw = _git(top, "ls-tree", "-r", "-l", "-z", commit, "--", prefix or ".")
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, encoded = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.decode().split()
            path = encoded.decode("utf-8", errors="surrogateescape")
            if prefix and not path.startswith(prefix):
                continue
            path = path[len(prefix):]
            if not _wanted(path, scopes) or Path(path).suffix.lower() not in EXTENSIONS:
                continue
            if mode == "120000" or kind != "blob":
                gaps.append({"path": path, "kind": "non_regular_source"})
                continue
            if len(files) >= max_files or time.monotonic() >= deadline:
                gaps.append({"kind": "discovery_budget", "detail": "Source list is incomplete; use a narrower paths scope or a larger explicit maintenance budget."})
                break
            files.append({"path": path, "object_id": oid, "size": int(size)})
    else:
        def fail(exc):
            gaps.append({"kind": "unavailable_directory", "detail": str(exc)[:300]})
        stop = False
        for base, dirs, names in os.walk(root, followlinks=False, onerror=fail):
            if time.monotonic() >= deadline:
                gaps.append({"kind": "discovery_budget"})
                break
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink()
                and (not scopes or _wanted((Path(base) / d).relative_to(root).as_posix(), scopes)
                     or any(scope.startswith((Path(base) / d).relative_to(root).as_posix() + "/") for scope in scopes)))
            for name in sorted(names):
                if time.monotonic() >= deadline:
                    gaps.append({"kind": "discovery_budget"})
                    stop = True
                    break
                path = Path(base) / name
                rel = path.relative_to(root).as_posix()
                if not _wanted(rel, scopes) or path.suffix.lower() not in EXTENSIONS:
                    continue
                if len(files) >= max_files or time.monotonic() >= deadline:
                    gaps.append({"kind": "discovery_budget", "detail": "Source list is incomplete; narrow paths or increase the explicit maintenance budget."})
                    stop = True
                    break
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        gaps.append({"path": rel, "kind": "non_regular_source"})
                        continue
                    files.append({"path": rel, "size": path.stat().st_size})
                except OSError as exc:
                    gaps.append({"path": rel, "kind": "unavailable_file", "detail": str(exc)[:300]})
            if stop:
                break
    for scope in scopes:
        if not any(item["path"] == scope or item["path"].startswith(scope + "/") for item in files):
            gaps.append({"path": scope, "kind": "empty_or_unavailable_scope"})
    return sorted(files, key=lambda item: item["path"]), gaps


def _valid_parsed(value):
    return (isinstance(value, dict) and isinstance(value.get("content_sha256"), str) and isinstance(value.get("line_count"), int)
            and all(isinstance(value.get(key), list) and all(isinstance(item, dict) for item in value[key])
                    for key in ("symbols", "references", "imports", "gaps"))
            and all(all(field in item for field in ("id", "qualified_name", "name", "language", "line_start", "line_end")) for item in value["symbols"])
            and all(all(field in item for field in ("source", "name", "kind", "language", "line_start")) for item in value["references"]))


def build_code_map(root, state_root, *, revision=None, paths=None, limits=None, cancelled=None) -> dict:
    """Inspect one explicit local root and persist only rebuildable private cache.

    ``revision`` reads immutable *local Git objects*, never fetches. Without it,
    hashes identify the exact bytes read from the working tree, with HEAD only
    an observation. ``paths`` bounds discovery to selected files/directories.
    Partial/failed work never replaces the previous complete map. File parse
    checkpoints survive interruptions and are reused on the next explicit call.
    The returned graph is for tools; use ``navigate`` for bounded Agent output.
    """
    started = time.monotonic()
    root = Path(root).resolve()
    state_root = Path(state_root).resolve()
    if not root.is_dir():
        return {"schema": SCHEMA, "status": "unavailable", "complete": False, "root": str(root),
                "source_complete": False, "source_id": _hash(str(root).encode())[:24], "scope": _scope(paths), "snapshot": None,
                "nodes": [], "edges": [], "files": [], "gaps": [{"kind": "source_unavailable"}]}
    budget = {**DEFAULT_LIMITS, **(limits or {})}
    if set(budget) != set(DEFAULT_LIMITS) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 for v in budget.values()):
        raise ValueError("limits require positive max_files, max_bytes, max_file_bytes and max_seconds")
    scopes = _scope(paths)
    deadline = started + budget["max_seconds"]
    observed_head = _git(root, "rev-parse", "--verify", "HEAD", check=False).decode().strip() or None
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", str(revision) + "^{commit}").decode().strip() if revision else None
    source_id = _hash(str(root).encode())[:24]
    destination = state_root / "code-map" / source_id
    from mindie_knowledge.distribution.sync import SwitchLock
    from mindie_knowledge.distribution.errors import DistributionError
    try:
        lock = SwitchLock(destination / "build.lock")
        lock.acquire()
    except DistributionError:
        return {"schema": SCHEMA, "status": "busy", "complete": False, "root": str(root), "source_id": source_id,
                "nodes": [], "edges": [], "files": [], "gaps": [{"kind": "another_build_active"}]}
    try:
        entries, gaps = _sources(root, commit, scopes, int(budget["max_files"]), deadline)
        parsed_files, inspected_files = {}, []
        stats = {"files_discovered": len(entries), "parsed": 0, "reused": 0, "bytes_read": 0}
        for entry in entries:
            path = entry["path"]
            language = EXTENSIONS[Path(path).suffix.lower()]
            fingerprint = _fingerprint(language)
            if time.monotonic() >= deadline or (cancelled and cancelled()):
                gaps.append({"path": path, "kind": "cancelled" if cancelled and cancelled() else "time_budget"})
                break
            if entry["size"] > budget["max_file_bytes"]:
                gaps.append({"path": path, "kind": "file_byte_budget", "bytes": entry["size"]})
                continue
            cached = {}
            if entry.get("object_id"):
                key = _hash((path + fingerprint + entry["object_id"]).encode())
                cached = _read(destination / "parsed" / (key + ".json"))
            if _valid_parsed(cached):
                parsed = cached
                stats["reused"] += 1
            else:
                if stats["bytes_read"] + entry["size"] > budget["max_bytes"]:
                    gaps.append({"path": path, "kind": "total_byte_budget"})
                    continue
                try:
                    if entry.get("object_id"):
                        data = _git(root, "cat-file", "blob", entry["object_id"])
                    else:
                        # Enforce the byte cap while reading even if a file grows
                        # after discovery. Never follow a newly substituted link.
                        current = root / path
                        if current.is_symlink() or not current.resolve().is_relative_to(root):
                            raise OSError("source path changed to a link outside the inspected file")
                        with current.open("rb") as stream:
                            data = stream.read(min(int(budget["max_file_bytes"]), int(budget["max_bytes"] - stats["bytes_read"])) + 1)
                    stats["bytes_read"] += len(data)
                    if len(data) > budget["max_file_bytes"] or stats["bytes_read"] > budget["max_bytes"]:
                        gaps.append({"path": path, "kind": "read_byte_budget"})
                        continue
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    gaps.append({"path": path, "kind": "unavailable_file", "detail": str(exc)[:300]})
                    continue
                digest = _hash(data)
                key = _hash((path + fingerprint + (entry.get("object_id") or digest)).encode())
                parsed = _read(destination / "parsed" / (key + ".json"))
                if _valid_parsed(parsed) and parsed["content_sha256"] == digest:
                    stats["reused"] += 1
                else:
                    if language == "python":
                        from .python import parse
                    else:
                        from .cpp import parse
                    try:
                        parsed = parse(data, path)
                    except Exception as exc:
                        parsed = {"symbols": [], "references": [], "imports": [], "gaps": [
                            {"kind": "parser_failure", "detail": f"{type(exc).__name__}: {exc}"[:500]}]}
                    parsed["content_sha256"] = digest
                    parsed["line_count"] = len(data.splitlines())
                    if not any(gap["kind"] == "parser_failure" for gap in parsed["gaps"]):
                        _write(destination / "parsed" / (key + ".json"), parsed)
                    stats["parsed"] += 1
            record = {"path": path, "language": language, "content_sha256": parsed["content_sha256"],
                      "revision": commit, "object_id": entry.get("object_id"), "size": entry["size"], "line_count": parsed["line_count"]}
            parsed_files[path] = parsed
            inspected_files.append(record)
            gaps.extend({"path": path, **gap} for gap in parsed["gaps"])
        snapshot = _hash(json.dumps({"revision": commit, "scope": scopes,
            "files": [(item["path"], item["content_sha256"]) for item in inspected_files]}, sort_keys=True).encode())
        nodes, edges = _assemble(parsed_files, inspected_files, snapshot)
        informational = {"static_scope", "unexpanded_macro", "wildcard_import", "syntax_warning"}
        source_failures = {"discovery_budget", "empty_or_unavailable_scope", "cancelled", "time_budget", "file_byte_budget", "total_byte_budget", "read_byte_budget", "unavailable_file", "unavailable_directory", "non_regular_source"}
        source_complete = len(inspected_files) == len(entries) and not any(gap["kind"] in source_failures for gap in gaps)
        complete = len(inspected_files) == len(entries) and not any(gap["kind"] not in informational for gap in gaps)
        result = {"schema": SCHEMA, "status": "ready" if complete else "partial", "complete": complete,
            "source_complete": source_complete,
            "root": str(root), "source_id": source_id, "revision": commit, "observed_head": observed_head,
            "repositories": _repositories(root),
            "snapshot": snapshot, "identity_kind": "git_revision" if commit else "observed_worktree_content",
            "scope": scopes, "files": inspected_files, "nodes": nodes, "edges": edges, "gaps": gaps,
            "stats": {**stats, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)},
            "semantics": "Static source references, not execution evidence or applicability decisions."}
        if complete:
            target = destination / ("map-" + _hash(json.dumps(scopes).encode())[:12] + ".json")
            previous = _read(target)
            if previous.get("snapshot") != snapshot or previous.get("parser_fingerprint") != [_fingerprint("python"), _fingerprint("cpp")]:
                result["parser_fingerprint"] = [_fingerprint("python"), _fingerprint("cpp")]
                _write(target, result)
        return result
    finally:
        lock.release()


def _assemble(files, records, snapshot):
    by_path = {item["path"]: item for item in records}
    nodes, edges = {}, []
    definitions = []
    by_qualified = defaultdict(list)
    by_file_qualified = defaultdict(list)
    modules = {}
    for path, parsed in files.items():
        record = by_path[path]
        def evidence(line=1, end=None):
            return {"path": path, "line_start": line, "line_end": end or line,
                    "content_sha256": record["content_sha256"], "revision": record["revision"], "snapshot": snapshot}
        nodes[f"file:{path}"] = {"id": f"file:{path}", "name": path, "kind": "file", "evidence": evidence()}
        for item in parsed["symbols"]:
            value = {**item, "path": path, "evidence": evidence(item["line_start"], item["line_end"])}
            nodes[item["id"]] = value
            definitions.append(value)
            by_qualified[(item["language"], item["qualified_name"])].append(value)
            by_file_qualified[(path, item["qualified_name"])].append(value)
        if record["language"] == "python":
            module = path.rsplit(".", 1)[0].replace("/", ".")
            modules[module.removesuffix(".__init__")] = path

    def external(ident, name, kind):
        nodes.setdefault(ident, {"id": ident, "name": name, "qualified_name": name, "kind": kind})
        return ident

    def emit(source, target, kind, ref, path, **extra):
        record = by_path[path]
        edges.append({"source": source, "target": target, "kind": kind, "resolution": "declared",
            "extraction_kind": "syntax_adapter" if kind in {"torch_schema", "torch_registration", "ascend_registration", "dynamic_api_name", "torch_operator_reference"} else "syntax_tree",
            "evidence": {"path": path, "line_start": ref["line_start"], "line_end": ref.get("line_end", ref["line_start"]),
                         "content_sha256": record["content_sha256"], "revision": record["revision"], "snapshot": snapshot},
            **extra})

    def candidates(name, language, path, scope=""):
        if language == "cpp":
            qualified = name if "::" in name else "::".join(filter(None, [scope, name]))
            exact = by_qualified[(language, qualified)]
            found = exact or by_file_qualified[(path, name)]
            return [d for d in found if d["kind"] != "function_declaration"] or found
        parts = scope.split(".") if scope else []
        while parts:
            exact = by_file_qualified[(path, ".".join([*parts, name]))]
            if exact:
                return exact
            parts.pop()
        return by_file_qualified[(path, name)]

    for path, parsed in files.items():
        for ref in parsed["references"]:
            name, kind = ref["name"], ref["kind"]
            if kind in {"torch_schema", "torch_registration"}:
                op = external("torch-op:" + name, name, "torch_operator")
                if kind == "torch_schema":
                    emit(f"file:{path}", op, kind, ref, path, schema=ref["schema"], guards=ref.get("guards", []))
                else:
                    matches = candidates(ref.get("implementation", ""), "cpp", path, ref.get("scope", "")) if ref.get("implementation") else []
                    target = matches[0]["id"] if len(matches) == 1 else external("cpp-reference:" + ref.get("implementation", name), ref.get("implementation", name), "unresolved_symbol")
                    emit(op, target, kind, ref, path, resolution="declared" if len(matches) == 1 else "unresolved",
                         dispatch=ref.get("dispatch", ""), candidates=[m["id"] for m in matches], guards=ref.get("guards", []))
                continue
            if kind == "dynamic_api_name":
                target = external("dynamic-api:" + name, name, "dynamic_api")
                emit(ref["source"], target, kind, ref, path, resolution="dynamic_name_only", guards=ref.get("guards", []))
                continue
            if kind == "ascend_registration":
                matches = candidates(name, "cpp", path, ref.get("scope", ""))
                target = matches[0]["id"] if len(matches) == 1 else external("ascend-op:" + name, name, "ascend_registration")
                emit(ref["source"], target, kind, ref, path, registration=ref["registration"],
                     implementation=ref.get("implementation"), guards=ref.get("guards", []))
                continue
            if ref["language"] == "python" and name.startswith("torch.ops."):
                parts = name.split(".")
                if len(parts) >= 4:
                    target_name = parts[2] + "::" + parts[3]
                    target = external("torch-op:" + target_name, target_name, "torch_operator")
                    emit(ref["source"], target, "torch_operator_reference", ref, path, resolution="static_reference")
                    continue
            matches = candidates(name, ref["language"], path, ref.get("scope", "")) if kind != "dynamic_expression" else []
            if not matches and ref["language"] == "python":
                parts = name.split(".")
                for imp in parsed["imports"]:
                    if imp["alias"] != parts[0] or (imp["scope"] and not ref.get("scope", "").startswith(imp["scope"])):
                        continue
                    module = imp["module"]
                    if imp["level"]:
                        current = path.rsplit("/", 1)[0].split("/") if "/" in path else []
                        current = current[:len(current) - imp["level"] + 1]
                        module = ".".join([*current, *filter(None, [module])])
                    tail = parts[1:]
                    member = imp["member"]
                    if member:
                        tail.insert(0, member)
                    elif not imp.get("bind_full_module", True):
                        module = parts[0]
                    while module not in modules and tail:
                        module += "." + tail.pop(0)
                    qualified = ".".join(tail)
                    matches.extend(by_file_qualified[(modules.get(module), qualified)])
            if len(matches) == 1:
                emit(ref["source"], matches[0]["id"], "static_call_reference", ref, path, resolution="static_reference", guards=ref.get("guards", []))
            else:
                target = external(f"unresolved:{ref['source']}:{ref['line_start']}:{name}", name, "unresolved_symbol")
                emit(ref["source"], target, kind, ref, path, resolution="ambiguous" if matches else "unresolved",
                     candidates=[m["id"] for m in matches], guards=ref.get("guards", []))
        for imp in parsed["imports"]:
            target_path = modules.get(imp["module"]) if by_path[path]["language"] == "python" else None
            if by_path[path]["language"] == "cpp":
                local = (Path(path).parent / imp["module"]).as_posix()
                target_path = local if local in files else imp["module"] if imp["module"] in files else None
            target = "file:" + target_path if target_path else external("import:" + imp["module"], imp["module"], "external_module")
            emit(f"file:{path}", target, "import_reference", imp, path,
                 resolution="static_reference" if target_path else "unresolved")
    return list(nodes.values()), edges


def navigate(code_map: dict, symbol: str, *, depth: int = 2, limit: int = 40) -> dict:
    """Bounded forward and backward static relationships for a symbol or file."""
    if not symbol.strip() or not 0 <= depth <= 8 or not 1 <= limit <= 1000:
        raise ValueError("symbol required; depth must be 0..8 and limit 1..1000")
    nodes = {item["id"]: item for item in code_map.get("nodes", [])}
    exact = [n for n in nodes.values() if symbol in {n["id"], n.get("name"), n.get("qualified_name"), n.get("path")}]
    matches = exact or [n for n in nodes.values() if symbol.casefold() in n.get("qualified_name", n.get("name", "")).casefold()]
    selected = {n["id"] for n in matches[:limit]}
    frontier, output, seen = set(selected), [], set()
    truncated = len(matches) > limit
    ordered = sorted(enumerate(code_map.get("edges", [])), key=lambda pair: (pair[1].get("resolution") in {"unresolved", "ambiguous"}, pair[1]["kind"] == "import_reference"))
    for _ in range(depth):
        following = set()
        for index, edge in ordered:
            if index in seen or not ({edge["source"], edge["target"]} & frontier):
                continue
            endpoints = {edge["source"], edge["target"]}
            if len(output) >= limit or len(selected | endpoints) > limit:
                truncated = True
                continue
            seen.add(index)
            output.append(edge)
            following.update(ident for ident in endpoints - selected if nodes.get(ident, {}).get("kind") not in {"file", "unresolved_symbol", "external_module", "dynamic_api"})
            selected.update(endpoints)
        frontier = following
        if not frontier:
            break
    return {"status": code_map.get("status", "unknown"), "snapshot": code_map.get("snapshot"),
            "root": code_map.get("root"), "revision": code_map.get("revision"),
            "matches": [item["id"] for item in matches[:limit]], "nodes": [nodes[k] for k in nodes if k in selected],
            "edges": output, "truncated": truncated, "gaps": code_map.get("gaps", [])[:limit],
            "semantics": "Static relationships; dynamic dispatch and observed execution remain unknown."}


def compare_maps(before: dict, after: dict) -> dict:
    """Changed/removed file and symbol facts; an incomplete map cannot prove deletion."""
    if before.get("source_id") != after.get("source_id") or before.get("scope") != after.get("scope"):
        raise ValueError("compare maps from the same source and scope")
    old = {item["path"]: item for item in before.get("files", [])}
    new = {item["path"]: item for item in after.get("files", [])}
    source_complete = after.get("source_complete", after.get("complete"))
    removed = sorted(set(old) - set(new)) if source_complete else []
    changed = sorted(path for path in old.keys() & new.keys() if old[path]["content_sha256"] != new[path]["content_sha256"])
    added = sorted(set(new) - set(old))
    affected = set(changed + removed)
    kinds = {"function", "function_declaration", "class"}
    remaining = {(n.get("path"), n.get("qualified_name"), n.get("kind"), n.get("content_sha256"))
                 for n in after.get("nodes", []) if n.get("kind") in kinds}
    symbols = [n for n in before.get("nodes", []) if n.get("kind") in kinds and n.get("path") in affected
               and (n.get("path"), n.get("qualified_name"), n.get("kind"), n.get("content_sha256")) not in remaining]
    return {"source_id": after.get("source_id"), "root": after.get("root"), "before": before.get("snapshot"),
            "after": after.get("snapshot"), "changed": changed, "added": added, "removed": removed,
            "unknown": sorted(set(old) - set(new)) if not source_complete else [],
            "symbols": [{"id": n["id"], "qualified_name": n["qualified_name"], "path": n["path"]} for n in symbols],
            "semantics": "Source change is an inspection hint, not proof that a linked claim is false."}
