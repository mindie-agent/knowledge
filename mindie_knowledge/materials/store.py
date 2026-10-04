"""Markdown authority with stable material blocks and small atomic pointers.

The only durable body lives in ordinary Markdown files. A manifest binds ordered
block hashes, their fallible search headers, and current navigation. Candidate
manifests let the queue database commit a pointer without overwriting the last
valid body first; ``retain_current`` retires unreferenced candidates afterwards.
ReMe owns derived chunking, the file graph, and BM25 (see ``reme_index.py``).
"""

from __future__ import annotations

import hashlib
import contextlib
import copy
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import yaml

from ..loop import documents
from ..loop.locks import StartLock

TASK_SCHEMA = "mindie-material-task/1"
BLOCK_SCHEMA = "mindie-material-block/1"
PACKAGE_SCHEMA = "mindie-material-package/1"
MAX_BLOCK_BYTES = 16 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = {"draft", "feed"}


class MaterialCleanupError(RuntimeError):
    """The current pointer committed, but retiring unused files failed."""

    committed = True


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(value, name="identity"):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _source(value):
    if value not in _SOURCE:
        raise ValueError("source must be draft or feed")
    return value


def _render(header, body):
    return "---\n" + yaml.safe_dump(header, sort_keys=True, allow_unicode=True, width=10**6) + "---\n\n" + body


def _parse(text):
    if not isinstance(text, str) or not text.startswith("---\n") or "\n---\n\n" not in text:
        raise ValueError("material file requires canonical YAML frontmatter")
    raw, body = text[4:].split("\n---\n\n", 1)
    header = yaml.load(raw, Loader=documents._UniqueKeyLoader)
    if not isinstance(header, dict) or _render(header, body) != text:
        raise ValueError("material file is not canonical Markdown")
    return header, body


def _navigation_body(header):
    lines = ["# " + header["entry"]["title"], "", header["navigation"], "", "## Materials", ""]
    for block in header["blocks"]:
        lines.append(f"- [{block['title']}](blocks/{block['block_id']}.md): {block['summary']}")
    return "\n".join(lines) + "\n"


def _material_digest(blocks, navigation, status):
    return _digest(dict(blocks=blocks, navigation=navigation, status=status))


def _entry_header(entry):
    return {key: value for key, value in entry.items() if key != "content"}


def _make_manifest(entry, blocks, navigation, status):
    entry = _entry_header(entry)
    entry["schema"] = "mindie-entry/3"
    entry["material_digest"] = _material_digest(blocks, navigation, status)
    entry["revision"] = documents.revision_of(entry)
    header = dict(schema=TASK_SCHEMA, task_id=entry["entry_id"], entry=entry,
                  blocks=blocks, navigation=navigation, status=status)
    return header, _render(header, _navigation_body(header))


def _parse_manifest(text, domain=None):
    header, body = _parse(text)
    return _validate_manifest(header, body, domain)


def _validate_manifest(header, body, domain=None):
    if set(header) != {"schema", "task_id", "entry", "blocks", "navigation", "status"}:
        raise ValueError("invalid material task fields")
    if header["schema"] != TASK_SCHEMA:
        raise ValueError("unsupported material task schema")
    _identity(header["task_id"], "task_id")
    entry = header["entry"]
    if not isinstance(entry, dict) or entry.get("entry_id") != header["task_id"]:
        raise ValueError("material task and entry identity differ")
    if entry.get("schema") != "mindie-entry/3" or "content" in entry:
        raise ValueError("task entry must be a mindie-entry/3 header")
    if domain is not None and entry.get("domain") != domain:
        raise ValueError("material package belongs to another domain")
    if not isinstance(header["navigation"], str) or not header["navigation"].strip():
        raise ValueError("navigation must be nonempty text")
    if header["status"] not in {"pending", "incomplete", "complete", "failed", "uncertain"}:
        raise ValueError("invalid material task status")
    blocks = header["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("material task requires ordered blocks")
    seen = set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) != {"block_id", "sha256", "title", "summary", "source_range", "indexed"}:
            raise ValueError("invalid material block descriptor")
        _identity(block["block_id"], "block_id")
        _identity(block["sha256"], "block sha256")
        if block["block_id"] in seen:
            raise ValueError("duplicate material block identity")
        seen.add(block["block_id"])
        if not all(isinstance(block[k], str) for k in ("title", "summary")) or type(block["indexed"]) is not bool:
            raise ValueError("invalid block indexing metadata")
        if block["indexed"] and not all(block[k].strip() for k in ("title", "summary")):
            raise ValueError("indexed blocks require a title and summary")
        if not isinstance(block["source_range"], (str, dict, list)):
            raise ValueError("source_range must be a public source descriptor")
    if entry.get("material_digest") != _material_digest(blocks, header["navigation"], header["status"]):
        raise ValueError("material digest does not bind the manifest")
    if entry.get("revision") != documents.revision_of(entry):
        raise ValueError("entry revision does not bind the manifest")
    if body != _navigation_body(header):
        raise ValueError("navigation body differs from bound task metadata")
    # Validate public fields independently of the body, which is checked below.
    documents.validate(dict(entry, content="Material body validated separately."))
    return header


def _parse_block(text, descriptor):
    if _sha(text) != descriptor["sha256"]:
        raise ValueError("material block hash mismatch")
    header, body = _parse(text)
    expected = dict(schema=BLOCK_SCHEMA, block_id=descriptor["block_id"], source_range=descriptor["source_range"])
    if header != expected:
        raise ValueError("material block header differs from task descriptor")
    if not body or len(body.encode("utf-8")) > MAX_BLOCK_BYTES:
        raise ValueError("material block body is empty or exceeds its byte envelope")
    return body


def validate_package_files(files, domain=None):
    """Validate the complete canonical package; never trust paths from a peer."""
    if not isinstance(files, dict) or "index.md" not in files:
        raise ValueError("material package requires index.md")
    header = _parse_manifest(files["index.md"], domain)
    expected = {"index.md"} | {f"blocks/{b['block_id']}.md" for b in header["blocks"]}
    if set(files) != expected:
        raise ValueError("package files differ from the exact manifest reference set")
    for block in header["blocks"]:
        _parse_block(files[f"blocks/{block['block_id']}.md"], block)
    return dict(schema=PACKAGE_SCHEMA, task_id=header["task_id"],
                revision=header["entry"]["revision"], package_hash=header["entry"]["revision"],
                files=dict(files), entry=dict(header["entry"]),
                ready=all(block["indexed"] for block in header["blocks"]))


package_from_files = validate_package_files


def _split_text(text):
    """Bound a new manual document without dropping a byte or cutting UTF-8."""
    raw = text.encode("utf-8")
    while raw:
        end = min(len(raw), MAX_BLOCK_BYTES)
        if end < len(raw):
            line_end = raw.rfind(b"\n", 0, end)
            if line_end > end // 2:
                end = line_end + 1
            while end and (raw[end] & 0xC0) == 0x80:
                end -= 1
        yield raw[:end].decode("utf-8")
        raw = raw[end:]


class MaterialStore:
    def __init__(self, root, domain=None):
        self.root = Path(root).resolve()
        self.domain = domain
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._write_lock = StartLock(self.root / ".write.lock")
        self._index = None
        self._index_verified = None
        self._index_failure = None
        self._header_cache = None
        self._catalog = None

    @contextlib.contextmanager
    def validated_headers(self):
        """Reuse immutable headers only inside one owning write transaction.

        Public reads always inspect the authoritative file. A subsequent
        transaction cannot silently inherit a header that changed on disk.
        """
        with self._mutex:
            outer = self._header_cache is None
            if outer:
                self._header_cache = {}
            try:
                yield
            finally:
                if outer:
                    self._header_cache = None

    def _atomic(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
        try:
            with tmp.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup:
                exc.add_note(f"material temporary-file cleanup also failed: {type(cleanup).__name__}")
            raise

    def _lock(self):
        return self._write_lock

    @contextlib.contextmanager
    def _locked_current(self):
        with self._mutex:
            owned = self._write_lock.acquired
            if not owned:
                self._write_lock.acquire(wait=None)
            try:
                yield
            finally:
                if not owned:
                    self._write_lock.release()

    def _pointers(self):
        return self._current_catalog().pointers()

    def _current_catalog(self):
        if self._catalog is None:
            from .catalog import CurrentCatalog
            self._catalog = CurrentCatalog(self)
        return self._catalog

    def _save_pointers(self, state, touched=None):
        catalog = self._current_catalog()
        catalog.publish(state, touched)

    def _task_root(self, task_id):
        path = self.root / "tasks" / _identity(task_id, "task_id")
        if any(candidate.is_symlink() for candidate in
               (path.parent, path, path / "manifests", path / "blocks")):
            raise ValueError("managed material directory cannot be a symlink")
        return path

    def _manifest_path(self, task_id, revision):
        return self._task_root(task_id) / "manifests" / (_identity(revision, "revision") + ".md")

    def _header(self, task_id, revision=None, source=None):
        _identity(task_id, "task_id")
        if revision is None:
            state = self._pointers()
            choices = [_source(source)] if source else ["feed", "draft"]
            revision = next((state[s][task_id] for s in choices if task_id in state[s]), None)
            if revision is None:
                raise KeyError(task_id)
        path = self._manifest_path(task_id, revision)
        if self._header_cache is not None and (task_id, revision) in self._header_cache:
            return copy.deepcopy(self._header_cache[task_id, revision])
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            raise KeyError(f"expired or missing material {task_id}@{revision}") from None
        header = _parse_manifest(text, self.domain)
        if header["task_id"] != task_id or header["entry"]["revision"] != revision:
            raise ValueError("material manifest path does not match its identity")
        if self._header_cache is not None:
            self._header_cache[task_id, revision] = header
            return copy.deepcopy(header)
        return header

    def _write_block(self, task_id, block):
        ident = _identity(block["block_id"], "block_id")
        text = block["text"]
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BLOCK_BYTES:
            raise ValueError("new material block must contain 1..16384 UTF-8 bytes")
        descriptor = dict(block_id=ident, title=block.get("title", ""),
                          summary=block.get("summary", ""), source_range=block.get("source_range", {}),
                          indexed=bool(block.get("title") and block.get("summary")))
        payload = _render(dict(schema=BLOCK_SCHEMA, block_id=ident, source_range=descriptor["source_range"]), text)
        descriptor["sha256"] = _sha(payload)
        path = self._task_root(task_id) / "blocks" / (ident + ".md")
        if path.exists():
            if path.read_bytes().decode("utf-8") != payload:
                raise ValueError("stable material block identity was reused with different content or metadata")
        else:
            self._atomic(path, payload)
        return descriptor

    def _write_manifest(self, entry, descriptors, navigation, status):
        header, text = _make_manifest(entry, descriptors, navigation, status)
        _validate_manifest(header, _navigation_body(header), self.domain)
        self._atomic(self._manifest_path(entry["entry_id"], header["entry"]["revision"]), text)
        if self._header_cache is not None:
            self._header_cache[entry["entry_id"], header["entry"]["revision"]] = header
        return dict(header["entry"], content="")

    def append_batch(self, task_id, blocks, navigation, *, domain=None, title="", summary="",
                     conditions=None, source="draft", status="incomplete", kind="experience",
                     revision=None, promote=False):
        """Append only new stable blocks; prior bodies are never read or rewritten."""
        _source(source)
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                selected = revision if revision is not None else self._pointers()[source].get(task_id)
                prior = self._header(task_id, selected, source) if selected is not None else None
                entry = dict(prior["entry"]) if prior else dict(
                    schema="mindie-entry/3", entry_id=_identity(task_id), domain=domain or self.domain,
                    kind=kind, title=title, summary=summary or navigation, conditions=conditions or {})
                if title:
                    entry["title"] = title
                entry["summary"] = summary or navigation
                if conditions is not None:
                    entry["conditions"] = dict(conditions)
                descriptors = list(prior["blocks"]) if prior else []
                known = {b["block_id"]: b for b in descriptors}
                for block in blocks:
                    descriptor = self._write_block(task_id, block)
                    if descriptor["block_id"] in known:
                        if any(known[descriptor["block_id"]][key] != descriptor[key]
                               for key in ("sha256", "source_range")):
                            raise ValueError("replayed material block source differs")
                    else:
                        descriptors.append(descriptor)
                        known[descriptor["block_id"]] = descriptor
                result = self._write_manifest(entry, descriptors, navigation, status)
                if promote:
                    state = self._pointers()
                    state[source][task_id] = result["revision"]
                    self._save_pointers(state, {task_id})
                return self.read_task(task_id, result["revision"], source)
            finally:
                lock.release()

    def put_document(self, doc, source="draft"):
        """Stage one new manual document and return its new bound entry header.

        Callers commit this returned metadata, then call ``retain_current``.
        This operation never changes the current pointer on its own.
        """
        _source(source)
        task_id = _identity(doc["entry_id"])
        blocks = [dict(block_id=_digest([task_id, i, _sha(text)]), text=text,
                       title=doc["title"], summary=doc["summary"], source_range={"part": i})
                  for i, text in enumerate(_split_text(doc["content"]))]
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                descriptors = [self._write_block(task_id, block) for block in blocks]
                result = self._write_manifest(doc, descriptors, doc["summary"], "incomplete")
                result["content"] = doc["content"]
                return result
            finally:
                lock.release()

    def get_document(self, entry_id, revision=None, source=None):
        header = self._header(entry_id, revision, source)
        content = "".join(self._read_block(entry_id, b) for b in header["blocks"])
        doc = dict(header["entry"], content=content)
        documents.validate(doc)
        return doc

    def _read_block(self, task_id, descriptor):
        path = self._task_root(task_id) / "blocks" / (descriptor["block_id"] + ".md")
        return _parse_block(path.read_bytes().decode("utf-8"), descriptor)

    def read_block(self, task_id, block_id, *, revision=None, source=None):
        header = self._header(task_id, revision, source)
        descriptor = next((b for b in header["blocks"] if b["block_id"] == block_id), None)
        if descriptor is None:
            raise KeyError(block_id)
        return dict(descriptor, text=self._read_block(task_id, descriptor),
                    navigation=header["navigation"], task_revision=header["entry"]["revision"])

    def read_current(self, task_id, *, source, revision, block_id=None, sha256=None):
        """Read current metadata and, when requested, exactly one member block.

        The metadata owner supplies its visible source/revision. The material
        lock prevents promotion/pruning during membership validation and the
        one body read. A retained file outside that current manifest grants no
        read access. Missing required files are faults, not expired references.
        """
        from .references import MaterialReadError, ReadReferenceError, task_ref

        navigation_ref = task_ref(self.domain, task_id)
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                pointers = self.root / "current.json"
                if pointers.is_symlink():
                    raise ValueError("current material pointers cannot be a symlink")
                state = self._pointers()
                if state[_source(source)].get(task_id) != revision:
                    raise ValueError("visible metadata and current material pointers differ")
                manifest = self._manifest_path(task_id, revision)
                if manifest.is_symlink():
                    raise ValueError("current manifest cannot be a symlink")
                header = self._header(task_id, revision, source)
                if block_id is None:
                    return dict(header=header)
                position = next((i for i, item in enumerate(header["blocks"])
                                 if item["block_id"] == block_id), None)
                if position is None or header["blocks"][position]["sha256"] != sha256:
                    raise ReadReferenceError(
                        "removed_or_superseded", "The requested block is no longer a member of the current task package.",
                        read_ref=navigation_ref,
                    )
                descriptor = header["blocks"][position]
                path = self._task_root(task_id) / "blocks" / (block_id + ".md")
                if path.is_symlink():
                    raise ValueError("current material block cannot be a symlink")
                text = self._read_block(task_id, descriptor)
                return dict(header=header, block=dict(descriptor), content=text, position=position)
            except ReadReferenceError:
                raise
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                raise MaterialReadError(
                    "Required current material could not be read or verified (" + type(exc).__name__ + ").",
                    read_ref=navigation_ref,
                ) from exc
            finally:
                lock.release()

    def read_task(self, task_id, revision=None, source="draft", *, include_body=False):
        if include_body:
            return self.get_document(task_id, revision, source)
        header = self._header(task_id, revision, source)
        return dict(header, entry=dict(header["entry"], content=""))

    def update_blocks(self, task_id, indexes, navigation, *, title=None, summary=None,
                      source="draft", revision=None, promote=False, status=None):
        """Update fallible headers and navigation without reading any block body."""
        _source(source)
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                prior = self._header(task_id, revision, source)
                changes = {item["block_id"]: item for item in indexes}
                if len(changes) != len(indexes) or set(changes) - {b["block_id"] for b in prior["blocks"]}:
                    raise ValueError("indexes contain duplicate or unknown block identities")
                blocks = []
                for block in prior["blocks"]:
                    block = dict(block)
                    if block["block_id"] in changes:
                        item = changes[block["block_id"]]
                        if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("title", "summary")):
                            raise ValueError("block index requires nonempty title and summary")
                        block.update(title=item["title"], summary=item["summary"], indexed=True)
                    blocks.append(block)
                entry = dict(prior["entry"])
                if title is not None:
                    entry["title"] = title
                entry["summary"] = summary or navigation
                result = self._write_manifest(entry, blocks, navigation, status or prior["status"])
                if promote:
                    state = self._pointers()
                    state[source][task_id] = result["revision"]
                    self._save_pointers(state, {task_id})
                return self.read_task(task_id, result["revision"], source)
            finally:
                lock.release()

    update_indexes = update_blocks

    def rebase_unsent_blocks(self, task_id, *, draft_revision, published_revision, sent_files):
        """Stage confirmed public blocks plus provably unsent local additions.

        This is an identity operation, never a text merge. The local package
        must still contain every last-sent block unchanged, or its additions
        cannot be separated safely from a rewritten old body.
        """
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                draft = self._header(task_id, draft_revision, "draft")
                public = self._header(task_id, published_revision, "feed")
                prefix = f"tasks/{task_id}/blocks/"
                sent = {item["path"][len(prefix):-3]: item["sha256"] for item in sent_files
                        if item["path"].startswith(prefix) and item["path"].endswith(".md")}
                local = {item["block_id"]: item for item in draft["blocks"]}
                if not sent or any(ident not in local or local[ident]["sha256"] != sha
                                   for ident, sha in sent.items()):
                    raise ValueError("local candidate is not an append-only extension of its confirmed sent blocks; "
                                     "cannot separate unsent material from a rewritten old body")
                remote = {item["block_id"]: item for item in public["blocks"]}
                additions = []
                for item in draft["blocks"]:
                    ident = item["block_id"]
                    if ident in remote:
                        if remote[ident]["sha256"] != item["sha256"]:
                            raise ValueError("current remote changed immutable block bytes")
                    elif ident not in sent:
                        additions.append(item)
                if not additions:
                    return dict(header=public, additions=0)
                # Remote navigation replaced claims about removed material.
                # New blocks retain their own already-produced indexes.
                result = self._write_manifest(public["entry"], [*public["blocks"], *additions],
                                              public["navigation"], draft["status"])
                return dict(header=self._header(task_id, result["revision"], "draft"),
                            additions=len(additions))
            finally:
                lock.release()

    def current_revisions(self, entry_id):
        state = self._pointers()
        return {s: state[s][entry_id] for s in ("draft", "feed") if entry_id in state[s]}

    def retain_current(self, entry_id, revisions=None, **kwargs):
        revisions = revisions if revisions is not None else kwargs
        return self.retain_snapshot({entry_id: revisions})

    def retain_snapshot(self, updates, *, complete=False):
        """Atomically promote all metadata-committed tasks, then retire garbage."""
        lock = self._lock()
        with self._mutex, self.validated_headers():
            lock.acquire(wait=None)
            try:
                if complete:
                    updates = dict(updates)
                    state = self._pointers()
                    managed = set(state["draft"]) | set(state["feed"])
                    tasks_root = self.root / "tasks"
                    if tasks_root.is_symlink():
                        raise ValueError("managed material directory cannot be a symlink")
                    if tasks_root.exists():
                        managed.update(path.name for path in tasks_root.iterdir() if _HEX.fullmatch(path.name))
                    for entry_id in managed - set(updates):
                        updates[entry_id] = {}
                for entry_id, revisions in updates.items():
                    _identity(entry_id)
                    for source, revision in revisions.items():
                        _source(source)
                        if revision:
                            self._header(entry_id, revision, source)
                state = self._pointers()
                for entry_id, revisions in updates.items():
                    for source in _SOURCE:
                        if revisions.get(source):
                            state[source][entry_id] = revisions[source]
                        else:
                            state[source].pop(entry_id, None)
                self._save_pointers(state, set(updates))
                try:
                    for entry_id, revisions in updates.items():
                        self._prune_task(entry_id, set(revisions.values()) - {None, ""})
                except Exception as exc:
                    raise MaterialCleanupError("material pointers committed; cleanup of unused files failed") from exc
            finally:
                lock.release()

    def recover_snapshot(self, snapshot):
        """Recover candidates against the complete committed metadata snapshot.

        The caller holds its metadata transaction lock. Only generated task
        directories are examined; native history and previous stores are inert.
        """
        return self.retain_snapshot(snapshot, complete=True)

    def _prune_task(self, task_id, revisions):
        keep_blocks = set()
        for revision in revisions:
            keep_blocks.update(b["block_id"] for b in self._header(task_id, revision)["blocks"])
        root = self._task_root(task_id)
        for path in (root / "manifests").glob("*.md"):
            if path.stem not in revisions:
                path.unlink()
        for path in (root / "blocks").glob("*.md"):
            if path.stem not in keep_blocks:
                path.unlink()
        for folder in (root / "manifests", root / "blocks"):
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        if root.exists() and not any(root.iterdir()):
            root.rmdir()

    def delete_document(self, entry_id, source=None):
        revisions = self.current_revisions(entry_id)
        if source is None:
            revisions.clear()
        else:
            revisions.pop(_source(source), None)
        self.retain_current(entry_id, revisions)

    delete_task = delete_document

    def export_task(self, task_id, source="draft", revision=None):
        header = self._header(task_id, revision, source)
        files = {"index.md": self._manifest_path(task_id, header["entry"]["revision"]).read_bytes().decode("utf-8")}
        for block in header["blocks"]:
            path = f"blocks/{block['block_id']}.md"
            files[path] = (self._task_root(task_id) / path).read_bytes().decode("utf-8")
        return validate_package_files(files, self.domain)

    def export_files(self, entry_id, **kwargs):
        package = self.export_task(entry_id, **kwargs)
        return [dict(path=path, content=text, sha256=_sha(text)) for path, text in package["files"].items()]

    def install_package(self, package, source="feed", source_revision=""):
        return self.install_packages([package], source=source, source_revision=source_revision, replace=False)

    def install_packages(self, packages, source="feed", source_revision="", *, replace=True, promote=True):
        """Validate all packages, write candidates, then atomically switch source."""
        _source(source)
        validated = []
        seen = set()
        for package in packages:
            value = validate_package_files(package["files"], self.domain)
            if any(package.get(k, value[k]) != value[k] for k in ("task_id", "revision", "package_hash")):
                raise ValueError("package envelope identity differs from its files")
            if value["task_id"] in seen:
                raise ValueError("duplicate task in package snapshot")
            seen.add(value["task_id"])
            validated.append(value)
        lock = self._lock()
        with self._mutex:
            lock.acquire(wait=None)
            try:
                state = self._pointers()
                old_ids = set(state[source])
                updated = {} if replace else dict(state[source])
                for package in validated:
                    task_id = package["task_id"]
                    for path, text in package["files"].items():
                        target = self._manifest_path(task_id, package["revision"]) if path == "index.md" else self._task_root(task_id) / path
                        if target.exists() and target.read_bytes().decode("utf-8") != text:
                            raise ValueError("immutable material identity conflicts with installed package")
                        if not target.exists():
                            self._atomic(target, text)
                    updated[task_id] = package["revision"]
                if promote:
                    state[source] = updated
                    if source == "feed":
                        state["source_revision"] = source_revision
                    self._save_pointers(state)
                    try:
                        for task_id in old_ids | set(updated):
                            revisions = {state[s][task_id] for s in _SOURCE if task_id in state[s]}
                            self._prune_task(task_id, revisions)
                    except Exception as exc:
                        raise MaterialCleanupError("package snapshot committed; cleanup of unused files failed") from exc
                return len(validated)
            finally:
                lock.release()

    def visible_tasks(self):
        with self._locked_current():
            return self._current_catalog().view()[1]

    def current_generation(self):
        with self._locked_current():
            return self._current_catalog().ensure_current()

    def _verify_matches(self, results, tasks):
        """Inspect selected authority, never stat every unrelated block."""
        from .catalog import signature
        from .references import parse_read_ref
        selected, checked_tasks = set(), set()
        def visit(items):
            for item in items:
                if item.get("ref"):
                    selected.add(item["ref"])
                for citation in item.get("cites", ()):
                    if citation.get("current_source_ref"):
                        selected.add(citation["current_source_ref"])
                visit(item.get("related", ()))
        visit(results)
        for ref in selected:
            parsed = parse_read_ref(ref, domain=self.domain)
            task_id = parsed["task_id"]
            task = tasks[task_id]
            if task_id not in checked_tasks:
                path = self._manifest_path(task_id, task["entry"]["revision"])
                current = signature(path)
                if current is None:
                    raise FileNotFoundError("required current material manifest is missing")
                if current != task["_manifest_signature"]:
                    header = self._header(task_id, task["entry"]["revision"])
                    if any(header[key] != task[key] for key in header):
                        raise ValueError("current material catalog differs from the authoritative manifest")
                    task["_manifest_signature"] = current
                checked_tasks.add(task_id)
            if parsed["kind"] == "block":
                block = next((block for block in task["blocks"] if block["block_id"] == parsed["block_id"]), None)
                if block is None or block["sha256"] != parsed["sha256"]:
                    raise ValueError("search result is not a current material block")
                self._index.verify_path(f"tasks/{task_id}/blocks/{block['block_id']}.md", block)

    def search(self, query=None, limit=5, conditions=None, allowed_ids=None, continuation=None):
        from .provenance import QueryContinuationError, query_request
        query, conditions = query_request(query, limit, conditions, continuation)
        from .reme_index import ReMeIndex
        with self._locked_current():
            catalog = self._current_catalog()
            generation, tasks = catalog.view()
            if self._index is None:
                self._index = ReMeIndex(self.root, self.domain)
            try:
                results = self._index.search(tasks, query, limit, conditions, allowed_ids,
                                             continuation=continuation, catalog=catalog)
                self._verify_matches(results, tasks)
            except QueryContinuationError:
                # A stale/invalid caller cursor is not a broken derived index.
                raise
            except Exception as exc:
                self._index_failure = type(exc).__name__
                self._index_verified = None
                raise
            self._index_verified = generation
            self._index_failure = None
            return results

    def index_status(self):
        """Report observed readiness; a file pointer is not an indexed body."""
        with self._locked_current():
            fingerprint, tasks = self._current_catalog().view()
            complete = not self._index_failure and (not tasks or self._index_verified == fingerprint)
            phase = ("failed" if self._index_failure else "ready" if complete else
                     "checkpointed-unverified" if (self.root / ".reme-index" / "snapshot.json").exists() else "unbuilt")
            return dict(backend="reme", complete=complete, phase=phase, visible=len(tasks),
                        indexed=len(tasks) if complete else None, error=self._index_failure)

    def refresh_index(self):
        """Build changed ReMe files once without making a model or search call."""
        from .reme_index import ReMeIndex
        with self._locked_current():
            catalog = self._current_catalog()
            fingerprint, tasks = catalog.view()
            if self._index_verified == fingerprint:
                return self.index_status()
            if self._index is None:
                self._index = ReMeIndex(self.root, self.domain)
            try:
                self._index.refresh(tasks, catalog=catalog)
            except Exception as exc:
                self._index_failure = type(exc).__name__
                self._index_verified = None
                raise
            self._index_verified = fingerprint
            self._index_failure = None
            return self.index_status()

    def close(self):
        try:
            if self._index is not None:
                self._index.close()
        finally:
            self._index = None
            self._index_verified = None
            if self._catalog is not None:
                self._catalog.close()
                self._catalog = None

    def rebuild_index(self):
        """Explicitly discard only ReMe's derived cache after a visible failure."""
        import shutil
        self.close()
        lock = StartLock(self.root / ".index.lock")
        lock.acquire(wait=None)
        try:
            path = self.root / ".reme-index"
            if path.exists():
                shutil.rmtree(path)
        finally:
            lock.release()
