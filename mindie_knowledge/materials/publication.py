"""Freeze contribution files without putting their bodies in the state database.

[Role] Freeze contribution files without putting their bodies into the state database.
[Invariants] A frozen candidate is reloaded by exact digest; a missing candidate is an error, never an empty contribution.
[Timeouts & Locks] Freeze/reload operations are bounded filesystem work with no network wait.
[Side-Effect Policy] Writes only the caller-designated staging directory; remote publication is out of scope.

The outbox row and manifest hold identities only. A required frozen candidate
lives as ordinary files until its remote outcome is known. Re-loading checks
those exact bytes; a missing candidate is an error, never an empty contribution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from mindie_knowledge.community.batch import check_path
from mindie_knowledge.community.common import MAX_FILE_BYTES, canonical
from mindie_knowledge.filesystem import filesystem_path
from .file_source import FileText, _io_path

STAGING_SCHEMA = "mindie-staged-contribution/1"


class CleanupReceiptError(RuntimeError):
    """The business result is committed; only cleanup receipt persistence failed."""

    def __init__(self, batch_id, detail):
        super().__init__(detail)
        self.batch_id = batch_id


def staging_path(root: Path, batch_id: str, revision: str) -> Path:
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch_id):
        raise ValueError("invalid staged batch identity")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("invalid staged batch revision")
    return filesystem_path(root) / "outbox" / "staging" / batch_id / revision


def metadata_only(batch: dict) -> dict:
    """Return a JSON-safe outbox descriptor, with no file contents."""
    value = {key: item for key, item in batch.items() if key != "files"}
    value["files"] = [
        {key: file[key] for key in file if key != "content"}
        for file in batch["files"]
    ]
    return value


def freeze_batch(root: Path, batch: dict) -> dict:
    """Atomically freeze a complete candidate, or verify the same candidate."""
    descriptor = metadata_only(batch)
    target = staging_path(root, batch["batch_id"], batch["revision"])
    if target.exists():
        existing = load_batch_payload(root, descriptor)
        if existing != batch:
            raise ValueError("frozen contribution differs from the requested candidate")
        return descriptor
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".freeze-", dir=target.parent))
    try:
        files_root = _io_path(temporary / "files")
        created_dirs: set[str] = set()
        for item in batch["files"]:
            path = check_path(item["path"])
            if item.get("delete") is True:
                continue
            if isinstance(item, FileText):
                raw = item.read_verified_bytes()
                raw.decode("utf-8")
                if len(raw) > MAX_FILE_BYTES or item._digest != item["sha256"]:
                    raise ValueError(f"contribution file identity mismatch: {path}")
            else:
                content = item.get("content")
                if not isinstance(content, str):
                    raise ValueError("contribution file has no content")
                raw = content.encode("utf-8")
                if len(raw) > MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise ValueError(f"contribution file identity mismatch: {path}")
            rel_path = path.replace("/", os.sep) if os.sep != "/" else path
            dest = os.path.join(files_root, rel_path)
            parent = os.path.dirname(dest)
            if parent not in created_dirs:
                os.makedirs(parent, exist_ok=True)
                created_dirs.add(parent)
            with open(dest, "wb") as fh:
                fh.write(raw)
        (temporary / "manifest.json").write_text(
            canonical({"schema": STAGING_SCHEMA, "batch": descriptor}) + "\n",
            encoding="utf-8", newline="\n",
        )
        temporary.rename(target)
    except BaseException as exc:
        try:
            if temporary.exists():
                shutil.rmtree(temporary)
        except TimeoutError:
            raise
        except OSError as cleanup:
            exc.add_note(f"incomplete staging cleanup failed: {type(cleanup).__name__}")
        raise
    return descriptor


def load_batch_payload(root, row: dict) -> dict:
    """Rebuild the in-memory publication request from checked frozen files.

    ``root`` may be the state Path or Store. ``row`` may be an outbox row
    containing its JSON ``batch`` descriptor, or the descriptor itself.
    """
    root = Path(root if isinstance(root, (str, os.PathLike)) else root.root)
    descriptor = row.get("batch", row)
    if isinstance(descriptor, str):
        descriptor = json.loads(descriptor)
    if not isinstance(descriptor, dict):
        raise ValueError("invalid outbox contribution descriptor")
    target = staging_path(root, descriptor["batch_id"], descriptor["revision"])
    if target.is_symlink():
        raise ValueError("staged contribution directory cannot be a symlink")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest != {"schema": STAGING_SCHEMA, "batch": descriptor}:
        raise ValueError("staged contribution manifest differs from its outbox receipt")
    target_io = _io_path(target)
    real_target = os.path.realpath(target_io)
    rst = os.stat(real_target) if os.path.exists(real_target) else None
    rid = (rst.st_dev, rst.st_ino) if rst is not None else None
    files_dir = os.path.join(target_io, "files")
    result = dict(descriptor, files=[])
    for item in descriptor["files"]:
        path = check_path(item["path"])
        if item.get("delete") is True:
            result["files"].append(dict(item, content=None))
            continue
        rel_path = path.replace("/", os.sep) if os.sep != "/" else path
        file_str = os.path.join(files_dir, rel_path)
        source = FileText(
            file_str, root=target_io, sha256=item['sha256'], metadata=item,
            _real_root=real_target, _root_id=rid,
        )
        try:
            source.read_verified_bytes().decode("utf-8")  # Validate immediately and again on every later use.
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise ValueError(f"missing or unsafe frozen contribution file: {path}") from None
        except ValueError as exc:
            if "identity mismatch" in str(exc) or "Unicode" in type(exc).__name__:
                raise
            raise ValueError(f"missing or unsafe frozen contribution file: {path}") from None
        result['files'].append(source)
    return result


def cleanup_staged_batch(root: Path, batch_id: str, revision: str) -> bool:
    """Remove only this resolved candidate; propagate cleanup failures."""
    path = staging_path(root, batch_id, revision)
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError("staged contribution directory cannot be a symlink")
    shutil.rmtree(path)
    return True
