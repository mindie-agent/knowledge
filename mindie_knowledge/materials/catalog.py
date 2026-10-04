"""Incremental current directory and atomic source-pointer generations.

Markdown holds the authoritative body and its validated manifest. Current
source pointers and validated headers share one SQLite transaction; current.json
is a fixed-size generation marker. A pending marker receipt recovers precisely
the catalog-commit / marker-replacement crash window, never unrelated corruption.
"""
from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping, MutableMapping


class CurrentVisibility(Mapping):
    """One immutable metadata snapshot, reusable for its catalog generation."""
    def __init__(self, generation, values):
        self.generation = generation
        self._values = dict(values)

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class SourcePointers(MutableMapping):
    """Lazy current pointers with an operation-local mutation overlay."""
    def __init__(self, catalog, source):
        self.catalog, self.source, self.updates = catalog, source, {}

    def __getitem__(self, key):
        if key in self.updates:
            value = self.updates[key]
        else:
            row = self.catalog.db.execute(
                "SELECT revision FROM pointers WHERE task_id=? AND source=?", (key, self.source)).fetchone()
            value = row[0] if row is not None else None
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.updates[key] = value

    def __delitem__(self, key):
        self[key]
        self.updates[key] = None

    def __iter__(self):
        existing = {row[0] for row in self.catalog.db.execute(
            "SELECT task_id FROM pointers WHERE source=?", (self.source,))}
        for key in existing | set(self.updates):
            if self.updates.get(key, True) is not None:
                yield key

    def __len__(self):
        return sum(1 for _ in self)


def signature(path):
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("current material file must be a regular file")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


class CurrentCatalog:
    VERSION = "mindie-current-catalog/2"
    POINTER_SCHEMA = "mindie-material-current/2"

    def __init__(self, materials):
        self.materials = materials
        path = materials.root / ".current-catalog.sqlite3"
        if path.is_symlink():
            raise ValueError("current material catalog cannot be a symlink")
        existed = path.exists()
        marker = materials.root / "current.json"
        if not existed and (marker.exists() or marker.is_symlink()):
            raise ValueError("existing current material catalog is missing")
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            if existed:
                tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"meta", "tasks", "pointers"}.issubset(tables):
                    raise ValueError("existing current material catalog is incomplete")
                values = {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT key,value FROM meta")}
                if not {"version", "generation", "source_revision", "pending_marker"}.issubset(values):
                    raise ValueError("existing current material catalog has missing metadata")
                if values["version"] != self.VERSION:
                    raise ValueError("unsupported current material catalog")
                generation, pending = values["generation"], values["pending_marker"]
                if (type(generation) is not int or generation < 0
                        or not isinstance(values["source_revision"], str)
                        or (pending is not None and (type(pending) is not int or pending != generation))
                        or (pending is None and "pointer_signature" not in values)):
                    raise ValueError("existing current material catalog has invalid metadata")
            self.db.execute("PRAGMA journal_mode=WAL")
            if not existed:
                self.db.executescript("""
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE tasks(task_id TEXT PRIMARY KEY,revision TEXT,source TEXT,
                        header TEXT,manifest_signature TEXT,generation INTEGER NOT NULL);
                    CREATE INDEX tasks_changed ON tasks(generation);
                    CREATE TABLE pointers(task_id TEXT NOT NULL,source TEXT NOT NULL,
                        revision TEXT NOT NULL,PRIMARY KEY(task_id,source));
                    CREATE INDEX pointers_by_source ON pointers(source,task_id);
                """)
                with self.db:
                    self._set("version", self.VERSION)
                    self._set("generation", 0)
                    self._set("source_revision", "")
                    self._set("pending_marker", 0)
        except BaseException:
            self.db.close()
            raise
        self._seen = -1
        self.tasks = {}

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def _set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    @property
    def generation(self):
        value = self._meta("generation")
        if type(value) is not int or value < 0:
            raise ValueError("invalid current material catalog generation")
        return value

    def _write_marker(self, generation):
        from .store import _canonical
        path = self.materials.root / "current.json"
        self.materials._atomic(path, _canonical(dict(schema=self.POINTER_SCHEMA, generation=generation)) + "\n")
        with self.db:
            self._set("pointer_signature", signature(path))
            self._set("pending_marker", None)

    def ensure_current(self):
        path = self.materials.root / "current.json"
        current = signature(path)
        generation, pending = self.generation, self._meta("pending_marker")
        if pending is None and current == self._meta("pointer_signature") and current is not None:
            return generation
        lock = self.materials._lock()
        if not lock.acquired:
            # Another writer may be between its catalog commit and marker.
            # Recheck under the same owner lock before repairing that window.
            lock.acquire(wait=None)
            try:
                return self.ensure_current()
            finally:
                lock.release()
        marker = json.loads(path.read_text(encoding="utf-8")) if current is not None else None
        if marker is not None and (not isinstance(marker, dict) or set(marker) != {"schema", "generation"}
                or marker["schema"] != self.POINTER_SCHEMA or type(marker["generation"]) is not int):
            raise ValueError("invalid current material pointers")
        if marker is None or marker["generation"] != generation:
            if pending != generation:
                raise ValueError("current material marker is missing or differs from the committed catalog")
            # Only this exact persisted promotion authorizes marker repair.
            self._write_marker(generation)
        else:
            with self.db:
                self._set("pointer_signature", current)
                self._set("pending_marker", None)
        return generation

    def pointers(self):
        self.ensure_current()
        return dict(schema=self.POINTER_SCHEMA, draft=SourcePointers(self, "draft"),
                    feed=SourcePointers(self, "feed"), source_revision=self._meta("source_revision"))

    def publish(self, state, touched=None):
        """Commit changed source pointers and visible headers under the write lock."""
        from .store import _canonical, MaterialCleanupError
        self.ensure_current()
        if touched is None:
            touched = (set(state["draft"]) | set(state["feed"]) |
                       {row[0] for row in self.db.execute("SELECT task_id FROM tasks WHERE revision IS NOT NULL")})
        changes, pointer_changes = [], []
        for ident in touched:
            selected = {source: state[source].get(ident) for source in ("draft", "feed")}
            previous = dict(self.db.execute("SELECT source,revision FROM pointers WHERE task_id=?", (ident,)))
            for source, revision in selected.items():
                if previous.get(source) != revision:
                    pointer_changes.append((ident, source, revision))
            source = "feed" if selected["feed"] else "draft"
            revision = selected[source]
            old = self.db.execute("SELECT revision,source FROM tasks WHERE task_id=?", (ident,)).fetchone()
            if (old is None and revision is None) or (old is not None and
                    (old["revision"], old["source"]) == (revision, source if revision else None)):
                continue
            header = self.materials._header(ident, revision, source) if revision else None
            path = self.materials._manifest_path(ident, revision) if revision else None
            changes.append((ident, revision, source if revision else None,
                            _canonical(header) if header else None,
                            _canonical(signature(path)) if path else None))
        if not pointer_changes and not changes and state["source_revision"] == self._meta("source_revision"):
            return
        with self.db:
            generation = self.generation + 1
            for ident, source, revision in pointer_changes:
                if revision is None:
                    self.db.execute("DELETE FROM pointers WHERE task_id=? AND source=?", (ident, source))
                else:
                    self.db.execute("INSERT OR REPLACE INTO pointers VALUES(?,?,?)", (ident, source, revision))
            for row in changes:
                self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?)", (*row, generation))
            self._set("generation", generation)
            self._set("source_revision", state["source_revision"])
            self._set("pending_marker", generation)
        try:
            self._write_marker(generation)
        except Exception as exc:
            raise MaterialCleanupError("current catalog committed; generation marker replacement failed") from exc

    def changes(self, after):
        return list(self.db.execute("SELECT * FROM tasks WHERE generation>? ORDER BY generation,task_id", (after,)))

    def view(self):
        from .store import _validate_manifest, _navigation_body
        generation = self.ensure_current()
        if generation < self._seen:
            raise ValueError("current material catalog generation moved backwards")
        if generation != self._seen:
            for row in self.changes(self._seen):
                if row["revision"] is None:
                    self.tasks.pop(row["task_id"], None)
                    continue
                header = json.loads(row["header"])
                _validate_manifest(header, _navigation_body(header), self.materials.domain)
                if header["task_id"] != row["task_id"] or header["entry"]["revision"] != row["revision"]:
                    raise ValueError("current material catalog identity differs from its manifest")
                self.tasks[row["task_id"]] = dict(header, source=row["source"],
                    _manifest_signature=json.loads(row["manifest_signature"]))
            self._seen = generation
        return generation, self.tasks

    def close(self):
        self.db.close()
