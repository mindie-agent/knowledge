"""Explicit trusted Git feed -> current domain documents, never raw sessions.

The independent intake reader verifies the committed export and all its hashes.
Old revisions remain explainable for existing uses, but disappear from search.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from vaws_knowledge.markdown import _atomic_write_text, render_markdown

from .store import canonical, content_id, digest, session_key, text


class Feed:
    def __init__(self, store, config):
        # Keep intake a separately installable, model-free component.
        from knowledge_intake.common import Budget, Limits
        from knowledge_intake.feed_sync import GitFeed, verified_snapshot

        self.Budget, self.Limits = Budget, Limits
        self.GitFeed, self.verify = GitFeed, verified_snapshot
        self.store = store
        self.config = dict(config)
        if config.get("domain") != store.domain:
            raise ValueError("feed must explicitly select this domain")
        repository, ref = config.get("repository"), config.get("ref")
        if (
            not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(ref, str)
            or not ref
        ):
            raise ValueError("feed requires a GitHub owner/repository and ref")
        self.interval = config.get("interval_seconds", 300)
        if type(self.interval) is not int or not 60 <= self.interval <= 86400:
            raise ValueError("feed interval must be 60..86400 seconds")
        self.ident = digest([repository, ref, config.get("prefix", "")])
        self.cache = store.root / "feeds" / self.ident / "blobs"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.next_sync = 0
        with store.lock:
            row = store.db.execute(
                "SELECT value FROM state WHERE key=?", ("feed:" + self.ident,)
            ).fetchone()
        self.receipt = json.loads(row[0]) if row else None
        self.last = self.receipt or dict(
            status="pending", repository=repository, ref=ref
        )

    def _reuse(self, name, size, signature):
        path = self.cache / signature
        if path.is_file() and path.stat().st_size == size:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() == signature:
                return raw
        return None

    def sync(self, *, force=False):
        if not force and time.monotonic() < self.next_sync:
            return self.last
        self.next_sync = time.monotonic() + self.interval
        try:
            budget = self.Budget(self.Limits(seconds=120))
            source = self.GitFeed(self.config["repository"], self.config["ref"], budget)
            if self.receipt and self.receipt["revision"] == source.commit:
                self.last = dict(self.receipt, status="unchanged", downloaded_files=0)
                return self.last
            snapshot = self.verify(
                source, self.config.get("prefix", ""), reuse=self._reuse
            )
            self.last = self.install(snapshot)
            return self.last
        except Exception as exc:
            self.last = dict(
                status="unavailable",
                repository=self.config["repository"],
                error=type(exc).__name__,
                detail=str(exc)[:500],
                retained_revision=(self.receipt or {}).get("revision"),
            )
            return self.last

    def install(self, snapshot):
        """Prepare all selected documents before switching searchable membership."""
        files = snapshot["files"]
        with self.store.lock:
            previous = {
                r["path"]: dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM feed_entries WHERE feed=? AND active=1",
                    (self.ident,),
                )
            }
        docs, skipped = [], []
        for name, raw in sorted(files.items()):
            if not name.endswith(".md"):
                continue
            # Maintenance diaries are operational records, not domain knowledge.
            kind = (
                "knowledge"
                if name.startswith("topics/")
                else "experience"
                if name.startswith("cases/")
                else None
            )
            if kind is None:
                skipped.append(name)
                continue
            metadata_raw = files[str(Path(name).with_suffix(".meta.json"))]
            metadata = json.loads(metadata_raw)
            conditions = metadata["conditions"]
            content = text(raw.decode("utf-8"), "feed content")
            title = text(
                next(
                    (
                        line.lstrip("# ")
                        for line in content.splitlines()
                        if line.startswith("# ")
                    ),
                    Path(name).stem,
                ),
                "feed title",
                240,
            )
            if kind == "knowledge" and not conditions:
                raise ValueError("topic knowledge requires explicit applicability")
            fingerprint = digest(
                [
                    hashlib.sha256(raw).hexdigest(),
                    hashlib.sha256(metadata_raw).hexdigest(),
                ]
            )
            old = previous.get(name)
            if old and old["fingerprint"] == fingerprint:
                doc = self.store.get(old["entry_id"])
            else:
                path = "/".join(
                    filter(
                        None,
                        [
                            self.config.get("prefix", ""),
                            "generations",
                            snapshot["generation"],
                            name,
                        ],
                    )
                )
                origin = dict(
                    url=f"https://github.com/{self.config['repository']}/blob/{snapshot['revision']}/{quote(path, safe='/')}",
                    revision=snapshot["revision"],
                    sha256=hashlib.sha256(raw).hexdigest(),
                    feed=self.ident,
                    path=name,
                )
                doc = dict(
                    kind=kind,
                    title=title,
                    content=content,
                    source=origin,
                    conditions=conditions,
                    producers=[session_key("git-feed:" + self.ident)],
                )
                doc["id"] = content_id(kind, title, content, origin, conditions)
            docs.append((name, fingerprint, doc))
        # Write inspectable copies first. Failed preparation never changes search.
        for _, _, doc in docs:
            target = self.store.root / "content" / doc["kind"] / doc["id"]
            _atomic_write_text(
                target.with_suffix(".md"), render_markdown(doc["title"], doc["content"])
            )
            _atomic_write_text(target.with_suffix(".meta.json"), canonical(doc) + "\n")
        for raw in files.values():
            signature = hashlib.sha256(raw).hexdigest()
            path = self.cache / signature
            if not path.exists():
                # Only verified bounded bytes are cached; rehash before reuse.
                path.write_bytes(raw)
        receipt = dict(
            status="synced",
            repository=self.config["repository"],
            ref=self.config["ref"],
            revision=snapshot["revision"],
            generation=snapshot["generation"],
            manifest_sha256=snapshot["manifest_sha256"],
            snapshot=snapshot["snapshot"],
            entries=len(docs),
            skipped=skipped,
            downloaded_files=snapshot["downloaded_files"],
            reused_files=snapshot["reused_files"],
        )
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE feed_entries SET active=0 WHERE feed=?", (self.ident,)
            )
            for name, fingerprint, doc in docs:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO entries VALUES(?,?)",
                    (doc["id"], canonical(doc)),
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO feed_entries VALUES(?,?,?,?,1)",
                    (self.ident, name, doc["id"], fingerprint),
                )
            self.store.db.execute(
                "INSERT OR REPLACE INTO state VALUES(?,?)",
                ("feed:" + self.ident, canonical(receipt)),
            )
        self.receipt = receipt
        return receipt
