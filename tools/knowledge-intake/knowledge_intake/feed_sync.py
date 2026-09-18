"""Pull a prepared Markdown feed explicitly, without any VAWS runtime import.

The configured Git repository is the trusted publisher. Hash verification binds
its selected commit to exact prepared bytes; it is not a new redaction engine
or evidence that the publisher's reference claims are true.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .common import Budget, IntakeError, Limits, command, digest, inside, read_json, write_atomic, write_json
from .sources import github_api
from .sync import _lock, _safe

SCHEMA = "vaws-curation-export/1"
PROFILE = "r2"
MAX_FILES = 1024
MAX_BODY = 4 * 1024 * 1024
MAX_META = 256 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_ROW_KEYS = {"path", "size", "sha256", "source_sha256", "input_sha256", "metadata_size", "metadata_sha256"}


def _encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or value.startswith("/"):
        raise IntakeError("feed paths must be portable relative names")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part[-1:] in {".", " "} or any(ord(char) < 32 or char in '<>\"|?*' for char in part)
           or part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in parts):
        raise IntakeError("feed paths are invalid or unsafe on Windows")
    return value


def _json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IntakeError("duplicate JSON field in feed")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


class GitFeed:
    """Read only committed blobs, using bounded local Git or native gh auth."""
    def __init__(self, repository: str, ref: str, budget: Budget):
        self.budget = budget
        self.local = Path(repository).resolve() if Path(repository).is_dir() else None
        self.repo = None
        self.trees = {}
        self.entries = {}
        self.reads = self.read_bytes = 0
        if not isinstance(ref, str) or not ref or ref.startswith("-") or any(char in ref for char in "\r\n\x00"):
            raise IntakeError("invalid feed ref")
        if self.local:
            _, raw = command(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=self.local,
                             timeout=budget.remaining(), max_bytes=4096)
            self.commit = raw.decode("ascii").strip()
        else:
            match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", repository)
            if not match:
                raise IntakeError("feed repository must be a GitHub repository or an existing local Git directory")
            self.repo = match[1]
            record = github_api(f"repos/{self.repo}/commits/{quote(ref, safe='')}", budget)
            self.commit = record["sha"]
            self.tree = record["commit"]["tree"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.commit):
            raise IntakeError("feed did not resolve to a Git commit")

    def _entry(self, name: str) -> dict:
        if name in self.entries:
            return self.entries[name]
        tree = self.tree
        parts = _relative(name).split("/")
        for index, part in enumerate(parts):
            if tree not in self.trees:
                self.trees[tree] = github_api(f"repos/{self.repo}/git/trees/{tree}", self.budget)
            result = self.trees[tree]
            if result.get("truncated"):
                raise IntakeError("feed tree listing was truncated")
            found = [row for row in result["tree"] if row.get("path") == part]
            if len(found) != 1:
                raise IntakeError(f"feed source is unavailable: {name}")
            row = found[0]
            if index + 1 < len(parts) and row.get("type") != "tree":
                raise IntakeError("feed path does not name an ordinary tree")
            tree = row["sha"]
        return row

    def read(self, name: str, limit: int) -> bytes:
        name = _relative(name)
        if self.local:
            # Confirm the Git mode; cat-file follows blob ids but never links.
            if name not in self.entries:
                _, raw = command(["git", "ls-tree", "-z", self.commit, "--", name], cwd=self.local,
                                 timeout=self.budget.remaining(), max_bytes=8192)
                rows = [row for row in raw.split(b"\0") if row]
                if len(rows) != 1 or rows[0].split(b"\t", 1)[0].split()[:2] != [b"100644", b"blob"]:
                    raise IntakeError(f"feed source is not a regular non-executable file: {name}")
                self.entries[name] = {"sha": rows[0].split(b"\t", 1)[0].split()[2].decode("ascii")}
            blob = self.entries[name]["sha"]
            _, data = command(["git", "cat-file", "blob", blob], cwd=self.local,
                              timeout=self.budget.remaining(), max_bytes=limit)
        else:
            row = self._entry(name)
            if row.get("mode") != "100644" or row.get("type") != "blob" or row.get("size", limit + 1) > limit:
                raise IntakeError("feed source is not a bounded non-executable file")
            blob = github_api(f"repos/{self.repo}/git/blobs/{row['sha']}", self.budget)
            if blob.get("encoding") != "base64" or blob.get("size", limit + 1) > limit:
                raise IntakeError("unsupported feed blob")
            data = base64.b64decode("".join(blob["content"].split()), validate=True)
        if len(data) > limit:
            raise IntakeError("feed source exceeds its byte budget")
        self.budget.check()
        self.reads += 1
        self.read_bytes += len(data)
        return data

    def files(self, prefix: str) -> set[str]:
        prefix = _relative(prefix)
        if self.local:
            _, raw = command(["git", "ls-tree", "-r", "-z", self.commit, "--", prefix + "/"], cwd=self.local,
                             timeout=self.budget.remaining(), max_bytes=MAX_MANIFEST)
            rows = []
            for value in raw.split(b"\0"):
                if value:
                    header, name = value.split(b"\t", 1)
                    mode, kind, oid = header.decode("ascii").split()
                    rows.append({"path": name.decode("utf-8")[len(prefix) + 1:], "mode": mode, "type": kind, "sha": oid})
        else:
            directory = self._entry(prefix)
            if directory.get("type") != "tree":
                raise IntakeError("feed generation is not a directory")
            data = github_api(f"repos/{self.repo}/git/trees/{directory['sha']}?recursive=1", self.budget)
            if data.get("truncated"):
                raise IntakeError("feed generation listing was truncated")
            rows = [row for row in data["tree"] if row.get("type") != "tree"]
        if len(rows) > MAX_FILES * 2 + 1 or any(row.get("type") != "blob" or row.get("mode") != "100644" for row in rows):
            raise IntakeError("feed generation contains excess, executable, linked or non-file content")
        self.entries.update({prefix + "/" + _relative(row["path"]): row for row in rows})
        return {_relative(row["path"]) for row in rows}


def verified_snapshot(source: GitFeed, prefix: str = "", *, reuse=None) -> dict:
    """Verify the export v1 protocol; do not import the producer's runtime."""
    base = _relative(prefix).rstrip("/") + "/" if prefix else ""
    pointer = _json(source.read(base + "current.json", 4096))
    if not isinstance(pointer, dict) or set(pointer) != {"schema", "generation", "manifest_sha256", "snapshot"} or pointer["schema"] != SCHEMA:
        raise IntakeError("feed export pointer is invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", str(pointer["generation"])) or any(not _SHA.fullmatch(str(pointer[key])) for key in ("manifest_sha256", "snapshot")):
        raise IntakeError("feed pointer hashes or generation are invalid")
    generation = base + "generations/" + pointer["generation"]
    raw_manifest = source.read(generation + "/prepared.json", MAX_MANIFEST)
    if digest(raw_manifest) != pointer["manifest_sha256"]:
        raise IntakeError("feed manifest hash differs from its pinned pointer")
    manifest = _json(raw_manifest)
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "redaction_profile", "includes", "snapshot", "previous_snapshot", "files", "changes"} or manifest["schema"] != SCHEMA or manifest["redaction_profile"] != PROFILE:
        raise IntakeError("feed export schema or public preparation profile is unsupported")
    rows, includes = manifest["files"], manifest["includes"]
    if not isinstance(rows, list) or len(rows) > MAX_FILES or not isinstance(includes, list) or not 1 <= len(includes) <= 16:
        raise IntakeError("feed manifest has invalid source bounds")
    for include in includes:
        _relative(include)
    if len(set(includes)) != len(includes):
        raise IntakeError("feed selected subdirectories contain duplicates")
    previous = manifest["previous_snapshot"]
    if previous is not None and (not isinstance(previous, str) or not _SHA.fullmatch(previous)):
        raise IntakeError("feed previous snapshot is invalid")
    changes = manifest["changes"]
    if not isinstance(changes, dict) or set(changes) != {"added", "removed", "updated", "renamed"}:
        raise IntakeError("feed change observations are malformed")
    for key in ("added", "removed", "updated"):
        if not isinstance(changes[key], list) or len(changes[key]) > MAX_FILES:
            raise IntakeError("feed change observations exceed their bounds")
        for name in changes[key]:
            _relative(name)
    if not isinstance(changes["renamed"], list) or len(changes["renamed"]) > MAX_FILES:
        raise IntakeError("feed rename observations are malformed")
    for rename in changes["renamed"]:
        if not isinstance(rename, dict) or set(rename) != {"from", "to", "basis"} or rename["basis"] != "identical_input_sha256":
            raise IntakeError("feed rename observation is invalid")
        _relative(rename["from"])
        _relative(rename["to"])
    signature = digest(_encoded({"files": rows, "includes": includes, "redaction_profile": PROFILE}))
    if signature != manifest["snapshot"] or signature != pointer["snapshot"]:
        raise IntakeError("feed snapshot hashes are inconsistent")
    observed = source.files(generation)
    files, expected, names, total = {}, {"prepared.json"}, set(), len(raw_manifest)
    reused_files = reused_bytes = 0
    def read(name, size, signature):
        nonlocal reused_files, reused_bytes
        raw = reuse(name, size, signature) if reuse else None
        if raw is not None:
            # A new commit can tamper with a blob while retaining yesterday's
            # manifest. Check its fixed Git tree identity before local reuse.
            oid = source.entries[generation + "/" + name]["sha"]
            algorithm = "sha1" if len(oid) == 40 else "sha256" if len(oid) == 64 else None
            if algorithm is None or hashlib.new(algorithm, b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest() != oid:
                raise IntakeError("feed committed blob disagrees with its declared unchanged source")
            reused_files += 1
            reused_bytes += len(raw)
            return raw
        return source.read(generation + "/" + name, size)
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise IntakeError("feed file entry has unsupported fields")
        name = _relative(row["path"])
        if not any(name.startswith(include + "/") for include in includes):
            raise IntakeError("feed source is outside its selected subdirectories")
        side = str(PurePosixPath(name).with_suffix(".meta.json"))
        if not name.casefold().endswith(".md") or name.casefold() in names or side.casefold() in names:
            raise IntakeError("feed source names collide across platforms")
        names.update((name.casefold(), side.casefold()))
        if any(not _SHA.fullmatch(str(row[key])) for key in ("sha256", "source_sha256", "input_sha256", "metadata_sha256")):
            raise IntakeError("feed source hashes are invalid")
        for key, ceiling in (("size", MAX_BODY), ("metadata_size", MAX_META)):
            if type(row[key]) is not int or not 0 <= row[key] <= ceiling:
                raise IntakeError("feed source exceeds its per-file bound")
        raw = read(name, row["size"], row["sha256"])
        metadata_raw = read(side, row["metadata_size"], row["metadata_sha256"])
        if len(raw) != row["size"] or digest(raw) != row["sha256"] or len(metadata_raw) != row["metadata_size"] or digest(metadata_raw) != row["metadata_sha256"]:
            raise IntakeError("feed body or metadata hash differs from the manifest")
        text, metadata = raw.decode("utf-8"), _json(metadata_raw)
        normalized = digest(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
        if normalized != row["source_sha256"] or not isinstance(metadata, dict) or set(metadata) != {"conditions", "retrieval"}:
            raise IntakeError("feed metadata is not prepared source-bound retrieval metadata")
        retrieval = metadata["retrieval"]
        if not isinstance(metadata["conditions"], dict) or any(not isinstance(value, str) for value in metadata["conditions"].values()) or not isinstance(retrieval, dict) or set(retrieval) != {"source_sha256", "aliases", "topics"} or retrieval["source_sha256"] != normalized:
            raise IntakeError("feed retrieval binding is invalid")
        if not isinstance(retrieval["aliases"], list) or not isinstance(retrieval["topics"], list) or any(not isinstance(value, str) for value in retrieval["topics"]):
            raise IntakeError("feed aliases or topics are malformed")
        for alias in retrieval["aliases"]:
            if isinstance(alias, str):
                continue
            if not isinstance(alias, dict) or not {"text", "relation"} <= alias.keys() <= {"text", "relation", "scope"} or not all(isinstance(alias[key], str) for key in ("text", "relation")):
                raise IntakeError("feed scoped alias is malformed")
            scope = alias.get("scope", [])
            if not isinstance(scope, str) and (not isinstance(scope, list) or any(not isinstance(item, str) for item in scope)):
                raise IntakeError("feed alias scope is malformed")
        total += len(raw) + len(metadata_raw)
        if total > MAX_TOTAL:
            raise IntakeError("feed snapshot exceeds its 32 MiB total bound")
        files.update({name: raw, side: metadata_raw})
        expected.update((name, side))
    if observed != expected:
        raise IntakeError("feed generation has missing or unmanaged files")
    return {"files": files, "generation": pointer["generation"], "manifest_sha256": pointer["manifest_sha256"], "snapshot": signature,
            "revision": source.commit, "changes": manifest["changes"], "documents": len(rows),
            "prepared_bytes": total, "reused_files": reused_files, "reused_bytes": reused_bytes,
            "downloaded_files": source.reads, "downloaded_bytes": source.read_bytes}


def _hash_file(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BODY:
        raise IntakeError("local feed file is not a bounded ordinary file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise IntakeError("local feed file exceeds its bound")
    return digest(raw)


def _validate_journal(journal, ident: str) -> None:
    if not isinstance(journal, dict) or journal.get("schema") != 1 or journal.get("identity") != ident or not isinstance(journal.get("files"), dict):
        raise IntakeError("feed journal is invalid or belongs to a different source/output")
    if len(journal["files"]) > MAX_FILES * 2:
        raise IntakeError("feed journal exceeds its file bound")
    for name, signature in journal["files"].items():
        _relative(name)
        if not isinstance(signature, str) or not _SHA.fullmatch(signature):
            raise IntakeError("feed journal contains an invalid file hash")
    if "pending" not in journal:
        return
    pending = journal["pending"]
    if not isinstance(pending, dict) or len(pending) > MAX_FILES * 4:
        raise IntakeError("feed recovery receipt is invalid")
    for name, row in pending.items():
        _relative(name)
        if not isinstance(row, dict) or set(row) != {"old", "new", "backup"}:
            raise IntakeError("feed recovery entry is invalid")
        if any(value is not None and (not isinstance(value, str) or not _SHA.fullmatch(value)) for value in (row["old"], row["new"])):
            raise IntakeError("feed recovery entry has invalid hashes")
        backup = _relative(row["backup"]).split("/", 2)
        if len(backup) != 3 or backup[0] != "recovery" or not re.fullmatch(r"[0-9a-f]{32}", backup[1]) or backup[2] != name:
            raise IntakeError("feed recovery path is invalid")


def _output_lock(output: Path) -> Path:
    # The same output may be accidentally configured with two different state
    # roots. All feed writers share this per-user OS lock, outside the notes.
    return Path(tempfile.gettempdir()) / "knowledge-intake-feed-locks" / (digest(os.path.normcase(str(output.resolve()))) + ".lock")


def _recover(state: Path, output: Path, journal: dict) -> None:
    """Rollback an interrupted batch only where exact old/new hashes still match."""
    pending = journal.get("pending")
    if not pending:
        return
    conflicts = []
    for name, info in pending.items():
        path = _safe(output, _relative(name))
        actual = _hash_file(path)
        if actual == info["old"]:
            continue
        if actual != info["new"]:
            conflicts.append(name)
            continue
        if info["old"] is None:
            path.unlink(missing_ok=True)
        else:
            backup = _safe(state, info["backup"])
            if _hash_file(backup) != info["old"]:
                raise IntakeError("feed recovery backup was modified; current files remain intact")
            write_atomic(path, backup.read_bytes())
    if conflicts:
        raise IntakeError("feed recovery preserved independently changed files: " + ", ".join(conflicts[:8]))
    journal.pop("pending", None)
    write_json(state / "journal.json", journal)
    for directory in {info["backup"].split("/")[1] for info in pending.values()}:
        _discard_backup(state, directory)


def _discard_backup(state: Path, transaction: str) -> None:
    # Only this completed transaction's private recovery files are disposable.
    if not re.fullmatch(r"[0-9a-f]{32}", transaction):
        return
    try:
        directory = _safe(state, "recovery/" + transaction)
        if directory.exists():
            shutil.rmtree(directory)
    except (OSError, ValueError):
        pass  # A locked/changed backup is harmless; committed notes remain active.


def sync_feed(config: dict) -> dict:
    """Apply one complete verified feed to its own directory, with recoverable rollback."""
    repository = str(config["repository"])
    if Path(repository).is_dir():
        repository = str(Path(repository).resolve())
    ref = config.get("ref", "codex/va-reference-feed")
    prefix = config.get("export_path", "")
    state, output = Path(config["state_root"]).resolve(), Path(config["output_root"]).resolve()
    if state == output or inside(state, output) or inside(output, state):
        raise IntakeError("feed state and output roots must not overlap")
    ident = digest({"repository": repository, "ref": ref, "export_path": prefix, "output_root": str(output)})
    result = {"status": "error", "updated": [], "deleted": [], "missing": [], "conflicts": [], "output_root": str(output)}
    seconds = config.get("max_seconds", 120)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 300:
        raise IntakeError("feed max_seconds must be between 0 and 300")
    budget = Budget(Limits(file_bytes=6 * 1024 * 1024, total_bytes=64 * 1024 * 1024, scan_entries=8192, seconds=seconds))
    with _lock(state / "sync.lock"), _lock(_output_lock(output)):
        journal, transaction = {}, None
        try:
            journal = read_json(state / "journal.json", {"schema": 1, "identity": ident, "files": {}})
            _validate_journal(journal, ident)
            _recover(state, output, journal)
            source = GitFeed(repository, ref, budget)
            if (journal.get("revision") == source.commit and _SHA.fullmatch(str(journal.get("manifest_sha256")))
                    and _SHA.fullmatch(str(journal.get("snapshot"))) and len(journal["files"]) <= MAX_FILES * 2):
                # Reuse the verified immutable Git commit, but reobserve local
                # files so replay never endorses a maintainer's changed bytes.
                result.update({key: journal[key] for key in ("revision", "snapshot", "manifest_sha256")})
                for name, expected in journal["files"].items():
                    budget.check()
                    if _hash_file(_safe(output, _relative(name))) != expected:
                        result["conflicts"].append(name)
                result["status"] = "conflict" if result["conflicts"] else "unchanged"
                result["reused_commit"] = True
                result.update(downloaded_files=0, downloaded_bytes=0, reused_files=len(journal["files"]),
                              documents=sum(name.casefold().endswith(".md") for name in journal["files"]))
                return result
            def reuse(name, size, signature):
                if journal["files"].get(name) != signature:
                    return None
                path = _safe(output, name)
                if not path.is_file() or path.stat().st_size != size:
                    return None
                with path.open("rb") as stream:
                    raw = stream.read(size + 1)
                return raw if len(raw) == size and digest(raw) == signature else None
            incoming = verified_snapshot(source, prefix, reuse=reuse)
            result.update({key: incoming[key] for key in ("revision", "snapshot", "manifest_sha256")})
            result.update({key: incoming[key] for key in ("documents", "prepared_bytes", "reused_files", "reused_bytes", "downloaded_files", "downloaded_bytes")})
            files, old = incoming["files"], journal["files"]
            new = {name: digest(raw) for name, raw in files.items()}
            # Preflight the complete set before changing a single visible file.
            for name in sorted(old.keys() | new.keys()):
                budget.check()
                actual = _hash_file(_safe(output, _relative(name)))
                if actual != old.get(name):
                    result["conflicts"].append(name)
            if result["conflicts"]:
                result["status"] = "conflict"
                return result
            changed = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
            result["missing"] = sorted(name for name in old.keys() - new.keys() if name.casefold().endswith(".md"))
            if not changed:
                if journal.get("revision") != incoming["revision"]:
                    journal.update({key: incoming[key] for key in ("revision", "snapshot", "manifest_sha256")})
                    write_json(state / "journal.json", journal)
                result["status"] = "unchanged"
                return result
            transaction = digest({"old": old, "new": new, "time": time.time_ns()})[:32]
            pending = {}
            for name in changed:
                budget.check()
                path = _safe(output, name)
                backup = f"recovery/{transaction}/{name}"
                if old.get(name) is not None:
                    raw = path.read_bytes()
                    if digest(raw) != old[name]:
                        raise IntakeError("local feed changed during transaction staging")
                    write_atomic(_safe(state, backup), raw)
                pending[name] = {"old": old.get(name), "new": new.get(name), "backup": backup}
            # This durable receipt precedes every output mutation.
            journal["pending"] = pending
            write_json(state / "journal.json", journal)
            try:
                for name in changed:
                    budget.check()
                    path = _safe(output, name)
                    if _hash_file(path) != pending[name]["old"]:
                        raise IntakeError("local feed changed before transaction commit")
                    if name in files:
                        write_atomic(path, files[name])
                    else:
                        path.unlink()
                if any(_hash_file(_safe(output, name)) != expected for name, expected in new.items()):
                    raise IntakeError("local feed changed during transaction commit")
                finished = {"schema": 1, "identity": ident, "files": new,
                            **{key: incoming[key] for key in ("revision", "snapshot", "manifest_sha256")}}
                write_json(state / "journal.json", finished)
                journal = finished
                _discard_backup(state, transaction)
            except Exception:
                _recover(state, output, journal)
                raise
            result["updated"] = [name for name in changed if name in new and name.casefold().endswith(".md")]
            result["deleted"] = result["missing"]
            result["status"] = "updated"
            return result
        except (OSError, ValueError, KeyError, TypeError, IntakeError) as exc:
            if transaction and isinstance(journal, dict) and not journal.get("pending"):
                _discard_backup(state, transaction)
            result.update(status="error", reason=str(exc), retained_revision=journal.get("revision") if isinstance(journal, dict) else None)
            return result
        finally:
            result["elapsed_ms"] = round((time.monotonic() - budget.started) * 1000, 3)
            write_json(state / "last-run.json", result)


def load_config(path: Path) -> dict:
    path = path.resolve()
    value = read_json(path)
    if not isinstance(value, dict):
        raise IntakeError("feed configuration must be a JSON object")
    if not isinstance(value.get("repository"), str) or not value["repository"].strip():
        raise IntakeError("feed configuration needs a repository string")
    for key in ("state_root", "output_root"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise IntakeError(f"feed configuration needs {key}")
        value[key] = str((path.parent / value[key]).resolve())
    repo = value.get("repository")
    if isinstance(repo, str) and (path.parent / repo).is_dir():
        value["repository"] = str((path.parent / repo).resolve())
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit prepared Markdown feed sync and optional Windows scheduling")
    commands = parser.add_subparsers(dest="action", required=True)
    sync = commands.add_parser("sync", help="pull and validate one committed export")
    sync.add_argument("config", type=Path)
    schedule = commands.add_parser("schedule", help="manage this feed's independent Windows task")
    schedule.add_argument("operation", choices=("install", "uninstall", "status"))
    schedule.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "sync":
            result = sync_feed(load_config(args.config))
        else:
            from .schedule import manage_schedule
            result = manage_schedule(args.operation, args.config)
    except (OSError, ValueError, KeyError, IntakeError) as exc:
        result = {"status": "error", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"updated", "unchanged", "installed", "uninstalled", "absent", "present"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
