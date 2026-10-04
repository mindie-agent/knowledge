"""Atomic per-block checkpoints for embedded ReMe components.

ReMe still chunks, maintains the graph and scores BM25. Only its whole-corpus
JSONL/pickle persistence is replaced: one changed block writes one current row,
and an interrupted transaction cannot expose mixed graph/chunk generations.
"""
from __future__ import annotations

import json
import sqlite3
import zlib

from .catalog import signature
from .provenance import POLICY
from .store import _canonical


class ReMeCheckpoint:
    FORMAT = "mindie-reme-checkpoint/2"

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.stamp = root / "snapshot.json"
        path = root / "blocks.sqlite3"
        marker = dict(schema=self.FORMAT, policy=POLICY)
        if self.stamp.exists() != path.exists():
            raise ValueError("ReMe derived checkpoint is incomplete; rebuild the index")
        existed = path.exists()
        if existed and json.loads(self.stamp.read_text(encoding="utf-8")) != marker:
            raise ValueError("ReMe derived checkpoint policy differs; rebuild the index")
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        if not existed:
            with self.db:
                self.db.executescript("""
                    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE files(path TEXT PRIMARY KEY,record BLOB NOT NULL);
                """)
                self.db.execute("INSERT INTO meta VALUES('generation','-1')")
            self.stamp.write_text(_canonical(marker) + "\n", encoding="utf-8", newline="")
        self.stamp_signature = signature(self.stamp)

    @property
    def generation(self):
        current = signature(self.stamp)
        if current != self.stamp_signature:
            if current is None or json.loads(self.stamp.read_text(encoding="utf-8")) != dict(schema=self.FORMAT, policy=POLICY):
                raise ValueError("ReMe derived checkpoint policy differs; rebuild the index")
            self.stamp_signature = current
        row = self.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        if row is None:
            raise ValueError("ReMe derived checkpoint has no committed generation")
        value = int(row[0])
        if value < -1:
            raise ValueError("invalid ReMe checkpoint generation")
        return value

    def records(self):
        for path, raw in self.db.execute("SELECT path,record FROM files ORDER BY path"):
            try:
                value = json.loads(zlib.decompress(raw).decode("utf-8"))
            except zlib.error as exc:
                raise ValueError("ReMe derived block checkpoint is corrupt; rebuild the index") from exc
            if not isinstance(value, dict) or value.get("node", {}).get("path") != path:
                raise ValueError("ReMe derived file identity differs from its checkpoint")
            yield path, value

    def commit(self, changed, deleted, generation):
        with self.db:
            self.db.executemany("DELETE FROM files WHERE path=?", ((path,) for path in deleted))
            self.db.executemany("INSERT OR REPLACE INTO files VALUES(?,?)",
                                ((path, zlib.compress(_canonical(value).encode("utf-8"))) for path, value in changed.items()))
            self.db.execute("UPDATE meta SET value=? WHERE key='generation'", (str(generation),))

    def close(self):
        self.db.close()
