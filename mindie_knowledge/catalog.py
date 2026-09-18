"""Incremental SQLite reference catalog; only maintenance writes snapshots.

Queries use an existing SQLite read transaction. Filesystem scans, extraction
and tokenization belong to refresh_catalog, never to opening a provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from mindie_knowledge.distribution.errors import DistributionError, SwitchInProgress
from mindie_knowledge.distribution.sync import SwitchLock
from mindie_knowledge.local.backend import Hit
from mindie_knowledge.markdown import Document, LAYERS, MAX_METADATA_BYTES, MAX_REFERENCE_BYTES, document_from_text, meta_path, normalized_sha256, read_bounded, retrieval_aliases, uri_for
from mindie_knowledge.retrieval import tokens

SCHEMA = 1
CATALOG_NAME = "reference-catalog.sqlite3"
POINTER_NAME = "reference-catalog.json"


def catalog_path(config: Any) -> Path | None:
    root = getattr(config, "state_root", None)
    if root is None:
        return None
    root = Path(root)
    pointer = root / POINTER_NAME
    try:
        if not pointer.exists():
            return root / CATALOG_NAME
        value = json.loads(read_bounded(pointer, 4096))
        name = value.get("file") if isinstance(value, dict) and value.get("schema") == 1 else None
        if not isinstance(name, str) or not re.fullmatch(r"reference-catalog-[0-9a-f]{32}\.sqlite3", name):
            return None
        path = root / name
        path.resolve().relative_to(root.resolve())
        return path
    except (OSError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _terms(text: str) -> str:
    # SQLite's tokenizer must not split an ACL/C++ identifier or a CJK bigram
    # differently from the public lexical route. Hex tokens are portable and
    # retain the existing tokenizer's exact boundaries without an extension.
    return " ".join(token.encode("utf-8").hex() for token in tokens(text))


def _root_identity(config: Any) -> list[list[str]]:
    return [[layer, str(Path(root).resolve())] for layer in LAYERS for root in config.mount(layer).roots]


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        connection = sqlite3.connect(path, timeout=1)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
    else:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.05)
        connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA cache_size=-16384")
    return connection


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY, uri TEXT UNIQUE NOT NULL, layer TEXT NOT NULL,
            path TEXT NOT NULL, root TEXT NOT NULL, relative_path TEXT NOT NULL,
            title TEXT NOT NULL, raw TEXT NOT NULL, payload TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, fingerprint TEXT NOT NULL, stat_key TEXT NOT NULL,
            contexts TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS documents_path ON documents(path);
        CREATE INDEX IF NOT EXISTS documents_layer ON documents(layer);
        CREATE INDEX IF NOT EXISTS documents_title ON documents(layer,root,title,path);
        CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(title,body,aliases);
        CREATE TABLE IF NOT EXISTS topics(document_id INTEGER NOT NULL, topic TEXT NOT NULL,
            PRIMARY KEY(document_id,topic));
        CREATE INDEX IF NOT EXISTS topics_name ON topics(topic,document_id);
    """)
    version = _metadata(db).get("schema")
    if version is not None and version != SCHEMA:
        raise ValueError(f"catalog schema {version} requires a rebuild")


def _metadata(db: sqlite3.Connection) -> dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM meta")}


def _aliases(document: Document) -> str:
    return "\n".join(retrieval_aliases(document))


def _insert(db: sqlite3.Connection, document: Document, root: str, fingerprint: str, stat_key: str) -> None:
    from mindie_knowledge.context import document_context

    raw = document.raw_text or f"# {document.title}\n\n{document.content}\n"
    payload = document.to_dict()
    payload.pop("content", None)
    old = db.execute("SELECT id FROM documents WHERE uri=?", (document.uri,)).fetchone()
    if old:
        _delete(db, old["id"])
    cursor = db.execute("""INSERT INTO documents(uri,layer,path,root,relative_path,title,raw,payload,
                      source_sha256,fingerprint,stat_key,contexts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (document.uri, document.layer, str(document.path), root,
                       document.path.relative_to(Path(root)).as_posix() if root else document.path.name,
                       document.title, raw, _json(payload), normalized_sha256(raw), fingerprint, stat_key,
                       _json(document_context(raw, document.conditions))))
    ident = cursor.lastrowid
    db.execute("INSERT INTO terms(rowid,title,body,aliases) VALUES(?,?,?,?)",
               (ident, _terms(document.title), _terms(document.content), _terms(_aliases(document))))
    db.executemany("INSERT INTO topics(document_id,topic) VALUES(?,?)",
                   ((ident, topic.casefold()) for topic in document.retrieval.get("topics", []) if isinstance(topic, str)))


def _delete(db: sqlite3.Connection, ident: int) -> None:
    db.execute("DELETE FROM terms WHERE rowid=?", (ident,))
    db.execute("DELETE FROM topics WHERE document_id=?", (ident,))
    db.execute("DELETE FROM documents WHERE id=?", (ident,))


def _repair_paths(root: Path, *, deadline: float, max_entries: int) -> Iterable[Path]:
    pending = [root]
    visited = 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                visited += 1
                if visited > max_entries or time.perf_counter() > deadline:
                    raise ValueError("catalog repair exceeded its scan or time budget")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name.endswith(".md"):
                    yield Path(entry.path)


def refresh_catalog(config: Any, *, force: bool = False, extra_documents: Iterable[Document] | None = None) -> dict[str, Any]:
    """Reconcile mounted sources and optional prepared shared documents once.

    Unchanged file/sidecar stat signatures avoid body reads. A forced integrity
    pass hashes sources too, while unchanged content still avoids tokenization.
    Missing or unreadable mounts preserve prior observations and report gaps.
    ``extra_documents`` are supplied by the release owner, not fetched here.
    When supplied they are the complete current imported shared collection;
    replacing it and retiring older derived rows happens in this transaction.
    """
    root = getattr(config, "state_root", None)
    if root is None:
        return {"status": "unavailable", "reason": "catalog state root is not configured"}
    lock = SwitchLock(Path(root) / "reference-catalog.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        return {"status": "busy"}
    try:
        # Read the pointer only after the writer lock: an explicit repair may
        # have switched it while this refresher was waiting.
        path = catalog_path(config)
        if path is None:
            return {"status": "pending", "reason": "catalog pointer is invalid; explicit repair is available"}
        return _refresh_at(config, path, force=force, extra_documents=extra_documents)
    finally:
        lock.release()


def _refresh_at(config: Any, path: Path, *, force: bool = False,
                extra_documents: Iterable[Document] | None = None,
                limits: dict[str, int | float] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    report: dict[str, Any] = {"status": "ready", "scanned": 0, "parsed": 0, "unchanged": 0,
                              "deleted": 0, "read_bytes": 0, "errors": [], "changed_uris": [], "deleted_uris": []}
    def budget() -> None:
        if limits and (report["scanned"] > limits["max_documents"] or report["read_bytes"] > limits["max_bytes"]
                       or time.perf_counter() - started > limits["max_seconds"]):
            raise ValueError("catalog repair exceeded its document, byte or time budget; the previous catalog remains active")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(path, write=True)) as db:
            _schema(db)
            db.execute("BEGIN IMMEDIATE")
            before = _metadata(db)
            report["previous_snapshot"] = before.get("snapshot", before.get("generation"))
            root_identity = _root_identity(config)
            mounted_roots = {root for _layer, root in root_identity}
            seen: set[str] = set()
            readable: set[str] = set()
            for layer in LAYERS:
                mount = config.mount(layer)
                for root in mount.roots:
                    root = Path(root).resolve()
                    try:
                        if not root.is_dir():
                            if layer == "candidate" and not root.exists():
                                continue
                            raise OSError("mounted source directory is unavailable")
                        from mindie_knowledge.health import _markdown_paths
                        paths = (_repair_paths(root, deadline=started + limits["max_seconds"],
                                               max_entries=min(int(limits["max_documents"] * 8), 1000000))
                                 if limits else _markdown_paths(root))
                        readable.add(str(root))
                    except OSError as exc:
                        report["errors"].append(f"{root}: {exc}")
                        continue
                    for source in paths:
                        report["scanned"] += 1
                        budget()
                        uri = uri_for(layer, source.relative_to(root).as_posix())
                        seen.add(uri)
                        try:
                            sidecar = meta_path(source)
                            file_stat = source.lstat()
                            try:
                                side_stat = sidecar.lstat()
                            except FileNotFoundError:
                                side_stat = None
                            if stat.S_ISLNK(file_stat.st_mode):
                                source.resolve().relative_to(root)
                                file_stat = source.stat()
                            if side_stat and stat.S_ISLNK(side_stat.st_mode):
                                sidecar.resolve().relative_to(root)
                                side_stat = sidecar.stat()
                            stat_key = _json([file_stat.st_mtime_ns, file_stat.st_size,
                                              side_stat.st_mtime_ns if side_stat else None,
                                              side_stat.st_size if side_stat else None])
                            old = db.execute("SELECT path,root,stat_key,fingerprint FROM documents WHERE uri=?", (uri,)).fetchone()
                            if old and old["path"] != str(source):
                                if old["root"] in mounted_roots and Path(old["path"]).exists():
                                    raise ValueError("two mounted files have the same reference; use distinct relative paths")
                                # A deliberate remount is a new source binding,
                                # even if size, mtime and text happen to agree.
                                old = None
                            if not force and old and old["stat_key"] == stat_key:
                                report["unchanged"] += 1
                                continue
                            raw = read_bounded(source, MAX_REFERENCE_BYTES)
                            metadata = read_bounded(sidecar, MAX_METADATA_BYTES) if side_stat else b""
                            report["read_bytes"] += len(raw) + len(metadata)
                            budget()
                            fingerprint = hashlib.sha256(raw + b"\0" + metadata).hexdigest()
                            if old and old["fingerprint"] == fingerprint:
                                db.execute("UPDATE documents SET stat_key=? WHERE uri=?", (stat_key, uri))
                                report["unchanged"] += 1
                                continue
                            parsed_metadata = json.loads(metadata.decode("utf-8")) if metadata else {}
                            if not isinstance(parsed_metadata, dict):
                                raise ValueError("source metadata must be an object")
                            document = document_from_text(raw.decode("utf-8"), path=source, layer=layer, root=root, metadata=parsed_metadata)
                            if document.uri != uri or normalized_sha256(document.raw_text) != normalized_sha256(raw.decode("utf-8")):
                                raise ValueError("source changed during catalog extraction or its identity is invalid")
                            if source.stat().st_mtime_ns != file_stat.st_mtime_ns or (sidecar.stat().st_mtime_ns if sidecar.is_file() else None) != (side_stat.st_mtime_ns if side_stat else None):
                                raise ValueError("source metadata changed during catalog extraction")
                            _insert(db, document, str(root), fingerprint, stat_key)
                            report["parsed"] += 1
                            report["changed_uris"].append(document.uri)
                            if document.retrieval.get("ignored"):
                                report["errors"].append(f"{source}: enrichment {document.retrieval['ignored']}")
                        except (OSError, ValueError, UnicodeError) as exc:
                            report["errors"].append(f"{source}: {exc}")
            for row in db.execute("SELECT id,uri,root,path FROM documents").fetchall():
                unmounted = bool(row["root"]) and row["root"] not in mounted_roots
                if unmounted or (row["root"] in readable and row["uri"] not in seen and not Path(row["path"]).exists()):
                    _delete(db, row["id"])
                    report["deleted"] += 1
                    report["deleted_uris"].append(row["uri"])
            if extra_documents is not None:
                extra_seen: set[str] = set()
                for document in extra_documents:
                    report["scanned"] += 1
                    budget()
                    if document.layer != "shared":
                        raise ValueError("prepared extra documents must belong to the shared layer")
                    extra_seen.add(document.uri)
                    raw = document.raw_text or f"# {document.title}\n\n{document.content}\n"
                    report["read_bytes"] += len(raw.encode("utf-8"))
                    budget()
                    if len(raw.encode("utf-8")) > MAX_REFERENCE_BYTES:
                        raise ValueError("prepared reference exceeds the catalog source byte budget")
                    fingerprint = hashlib.sha256((raw + _json(document.to_dict())).encode("utf-8")).hexdigest()
                    old = db.execute("SELECT fingerprint FROM documents WHERE uri=?", (document.uri,)).fetchone()
                    if old and old["fingerprint"] == fingerprint:
                        report["unchanged"] += 1
                    else:
                        _insert(db, document, "", fingerprint, "")
                        report["parsed"] += 1
                        report["changed_uris"].append(document.uri)
                for row in db.execute("SELECT id,uri FROM documents WHERE root='' AND layer='shared'").fetchall():
                    if row["uri"] not in extra_seen:
                        _delete(db, row["id"])
                        report["deleted"] += 1
                        report["deleted_uris"].append(row["uri"])
            budget()
            changed = report["parsed"] or report["deleted"] or before.get("roots") != root_identity
            generation = int(before.get("generation", 0)) + int(bool(changed) or not before)
            # A rebuilt database must never impersonate a receipt for an older
            # generation, even when both contain revision 1.
            epoch = before.get("epoch") or uuid.uuid4().hex
            snapshot = f"{epoch}:{generation}"
            report["documents"] = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            report["status"] = "partial" if report["errors"] else "ready"
            values = {"schema": SCHEMA, "epoch": epoch, "generation": generation, "snapshot": snapshot, "roots": root_identity,
                      "documents": report["documents"], "errors": report["errors"], "checked_at": time.time()}
            db.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ((key, _json(value)) for key, value in values.items()))
            db.commit()
            report["snapshot"] = snapshot
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report["disk_bytes"] = sum(item.stat().st_size for item in path.parent.glob(path.name + "*") if item.is_file())
        return report
    except (OSError, sqlite3.Error, ValueError, DistributionError) as exc:
        return {**report, "status": "pending", "reason": f"{type(exc).__name__}: {exc}"}


def repair_catalog(config: Any, *, extra_documents: Iterable[Document] | None = None,
                   limits: dict[str, int | float] | None = None) -> dict[str, Any]:
    """Build a fresh bounded generation and atomically switch a small pointer.

    Old databases and their WAL files are never renamed or removed. Existing
    readers finish on their observed generation, including on Windows. A failed
    rebuild or pointer replacement leaves the previous generation available.
    This explicit maintenance entry is never called by query or provider open.
    """
    from mindie_knowledge.distribution.manifest import atomic_write_json
    root = getattr(config, "state_root", None)
    if root is None:
        return {"status": "unavailable", "reason": "catalog state root is not configured"}
    bounds: dict[str, int | float] = {"max_documents": 100000, "max_bytes": 1073741824, "max_seconds": 1800}
    bounds.update(limits or {})
    ceilings = {"max_documents": 1000000, "max_bytes": 8589934592, "max_seconds": 7200}
    if set(bounds) != set(ceilings) or any(isinstance(value, bool) or not isinstance(value, (int, float))
                                         or not 1 <= value <= ceilings[key] for key, value in bounds.items()):
        raise ValueError("catalog repair limits are outside the supported bounded range")
    root = Path(root)
    lock = SwitchLock(root / "reference-catalog.lock")
    try:
        lock.acquire()
    except SwitchInProgress:
        return {"status": "busy"}
    try:
        previous = catalog_path(config)
        destination = root / f"reference-catalog-{uuid.uuid4().hex}.sqlite3"
        if extra_documents is None:
            from mindie_knowledge.local.shared import current_shared
            current = current_shared(root)
            if current and current.get("prepared_root"):
                from mindie_knowledge.distribution.references import prepared_shared_documents
                extra_documents = prepared_shared_documents(root, current=current)
        report = _refresh_at(config, destination, force=True, extra_documents=extra_documents, limits=bounds)
        report.update(repair=True, retained_catalog=str(previous) if previous and previous.exists() else None)
        if report["status"] != "ready":
            report["switched"] = False
            return report
        try:
            atomic_write_json(root / POINTER_NAME, {"schema": 1, "file": destination.name, "snapshot": report["snapshot"]})
        except OSError as exc:
            return {**report, "status": "busy", "switched": False, "reason": f"catalog pointer replacement failed: {exc}"}
        return {**report, "switched": True}
    finally:
        lock.release()


def _document(row: sqlite3.Row) -> Document:
    from mindie_knowledge.markdown import parse_markdown
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("catalog document metadata must be an object")
    _title, content = parse_markdown(row["raw"])
    return Document(layer=row["layer"], title=row["title"], content=content,
                    slug=payload.get("slug") or Path(row["path"]).stem, path=Path(row["path"]), uri=row["uri"],
                    status=payload.get("status"), source=payload.get("source"), conditions=payload.get("conditions", {}),
                    evidence=payload.get("evidence"), captured_at=payload.get("captured_at"), raw_text=row["raw"],
                    retrieval=payload.get("retrieval", {}))


@dataclass
class CatalogSearch:
    hits: list[Hit] = field(default_factory=list)
    documents: dict[str, Document] = field(default_factory=dict)
    incomplete: bool = False
    notes: list[str] = field(default_factory=list)
    snapshot: str | int | None = None
    available: bool = False


def topic_selection(config: Any, text: str, selection: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    import re
    settings = dict(getattr(config, "selection", {}) if selection is None else selection)
    if re.search(r"\ball-topics\b", text, re.I):
        settings = {}
        text = re.sub(r"\ball-topics\b", "", text, flags=re.I)
    explicit = re.findall(r"\btopic:([\w./-]+)", text, re.I)
    if explicit:
        settings = {"topics": explicit, "mode": "prefer"}
        text = re.sub(r"\btopic:[\w./-]+", "", text, flags=re.I)
    settings.setdefault("mode", "prefer")
    return text.strip(), settings


def search_catalog(config: Any, text: str, *, layers: Sequence[str] | None = None,
                   limit: int = 32, selection: dict[str, Any] | None = None,
                   shared_current: dict[str, Any] | None = None) -> CatalogSearch:
    """Search a durable snapshot without scanning or modifying source files."""
    result = CatalogSearch()
    path = catalog_path(config)
    if path is None or not path.is_file():
        result.incomplete = True
        result.notes.append("Reference catalog is absent; bounded fallback may be incomplete.")
        return result
    clean, settings = topic_selection(config, text, selection)
    terms = list(dict.fromkeys(tokens(clean)))[:32]
    wanted = [layer for layer in (layers or LAYERS) if layer in LAYERS]
    if shared_current is None:
        from mindie_knowledge.local.shared import current_shared
        shared_current = current_shared(getattr(config, "state_root", None)) or {}
    active_root = str(shared_current.get("root_uri") or "").rstrip("/")
    try:
        with closing(_connect(path)) as db:
            db.execute("BEGIN")
            metadata = _metadata(db)
            if metadata.get("schema") != SCHEMA:
                raise ValueError("reference catalog schema is incompatible")
            result.available = True
            result.snapshot = metadata.get("snapshot", metadata.get("generation"))
            result.incomplete = bool(metadata.get("errors")) or metadata.get("roots") != _root_identity(config)
            if result.incomplete:
                result.notes.append("Catalog has source gaps or changed mounts; maintenance is pending.")
            if not terms or not wanted:
                return result
            expression = " OR ".join('"' + term.encode("utf-8").hex() + '"' for term in terms)
            placeholders = ",".join("?" for _ in wanted)
            topics = settings.get("topics", [])
            topics = [topics] if isinstance(topics, str) else topics
            topics = {topic.casefold() for topic in topics if isinstance(topic, str)}
            params: list[Any] = [expression, *wanted]
            # Local shared bootstrap roots stay searchable. Imported catalog
            # rows may belong only to the single pointer observed for this query.
            active = " AND (d.layer!='shared' OR d.root!=''"
            if active_root:
                active += " OR substr(d.uri,1,?)=?"
                params.extend([len(active_root) + 1, active_root + "/"])
            active += ")"
            only = ""
            if topics and settings.get("mode") == "only":
                only = " AND d.id IN (SELECT document_id FROM topics WHERE topic IN (" + ",".join("?" for _ in topics) + "))"
                params.extend(sorted(topics))
            params.append(max(1, min(limit * 3, 192)))
            rows = db.execute(f"""SELECT d.*,bm25(terms,2.0,1.0,0.5) AS rank FROM terms
                JOIN documents d ON d.id=terms.rowid WHERE terms MATCH ? AND d.layer IN ({placeholders})
                {active}{only} ORDER BY rank,d.uri LIMIT ?""", params).fetchall()
            ranked = []
            for row in rows:
                document = _document(row)
                preferred = bool(topics.intersection(topic.casefold() for topic in document.retrieval.get("topics", [])))
                score = -row["rank"] * (1.25 if preferred else 1.0)
                ranked.append((score, document))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].uri))
            for score, document in ranked[:limit]:
                result.hits.append(Hit(document.uri, score, document.title, layer=document.layer))
                result.documents[document.uri] = document
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        result.available = False
        result.incomplete = True
        result.notes.append(f"Reference catalog is unavailable: {type(exc).__name__}: {exc}")
    return result


def catalog_title_matches(config: Any, title: str, *, root: Path, layer: str = "candidate") -> dict[str, Any]:
    """Read a bounded exact-title observation, without opening document bodies.

    Maintenance creates the title index. An older or unavailable catalog falls
    back to the caller's bounded compatibility lookup, never a SQLite full scan.
    The snapshot cannot establish the identity of an unobserved manual rename.
    """
    path = catalog_path(config)
    unavailable = {"available": False, "matches": [], "incomplete": True}
    if path is None or not path.is_file():
        return unavailable
    try:
        with closing(_connect(path)) as db:
            db.execute("BEGIN")
            metadata = {row["key"]: json.loads(row["value"]) for row in db.execute(
                "SELECT key,value FROM meta WHERE key IN ('schema','snapshot','roots')")}
            if metadata.get("schema") != SCHEMA:
                return unavailable
            # A large source-error list need not be decoded by a capture.
            gaps = db.execute("SELECT value!='[]' FROM meta WHERE key='errors'").fetchone()
            resolved_root = str(root.resolve())
            rows = db.execute("""SELECT path,uri FROM documents INDEXED BY documents_title
                                 WHERE layer=? AND root=? AND title=? ORDER BY path LIMIT 9""",
                              (layer, resolved_root, title)).fetchall()
            return {"available": True, "matches": [dict(row) for row in rows[:8]],
                    "incomplete": bool(len(rows) > 8 or (gaps and gaps[0])
                                       or [layer, resolved_root] not in metadata.get("roots", [])),
                    "snapshot": metadata.get("snapshot")}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return unavailable


def get_catalog_documents(config: Any, refs: Sequence[str], *, layers: Sequence[str] | None = None) -> dict[str, Document]:
    """Locate only requested candidates; source validation stays with query."""
    path = catalog_path(config)
    if not refs or path is None or not path.is_file():
        return {}
    wanted = set(layers or LAYERS)
    try:
        with closing(_connect(path)) as db:
            rows = db.execute("SELECT * FROM documents WHERE uri IN (" + ",".join("?" for _ in refs) + ")", list(refs))
            return {row["uri"]: _document(row) for row in rows if row["layer"] in wanted}
    except (sqlite3.Error, OSError, ValueError):
        return {}


def get_catalog_document(config: Any, ref: str, *, layers: Sequence[str] | None = None) -> Document | None:
    path = catalog_path(config)
    if path is None or not path.is_file():
        return None
    try:
        with closing(_connect(path)) as db:
            # URI/path lookups are indexed; short names remain a bounded
            # catalog lookup, never a filesystem walk.
            row = db.execute("SELECT * FROM documents WHERE uri=? OR path=? OR relative_path=? LIMIT 1", (ref, ref, ref)).fetchone()
            if row is None:
                row = db.execute("SELECT * FROM documents WHERE json_extract(payload,'$.slug')=? LIMIT 1", (ref,)).fetchone()
            return _document(row) if row and row["layer"] in (layers or LAYERS) else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def export_catalog(config: Any, *, layers: Sequence[str] | None = None) -> Iterable[dict[str, Any]]:
    """Stream portable source-bound rows. This is not a redaction boundary.

    Public release callers must build from package-prepared public sources and
    validate every exported field. Absolute local paths and private sidecars
    are intentionally omitted, but source text itself may still be private.
    """
    path = catalog_path(config)
    if path is None or not path.is_file():
        return
    with closing(_connect(path)) as db:
        for row in db.execute("SELECT * FROM documents ORDER BY uri"):
            if row["layer"] not in (layers or LAYERS):
                continue
            payload = json.loads(row["payload"])
            yield {"schema": SCHEMA, "relative_path": row["relative_path"], "title": row["title"],
                   "source_sha256": row["source_sha256"], "raw_text": row["raw"],
                   "retrieval": payload.get("retrieval", {}), "contexts": json.loads(row["contexts"])}
