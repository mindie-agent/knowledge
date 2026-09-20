"""Model-free knowledge sync from the canonical Git publication.

The configured content repository's branch is followed as one immutable Git
commit whose tree holds canonical ``mindie-entry/1`` documents under
``cases/`` and ``topics/`` (``feedback/*.json`` belongs to the repository
side and is ignored here). Every candidate commit is fully validated before
the atomic switch: layout, sizes, UTF-8/LF bytes, schema, revisions, domain
and entry identities. A structurally incompatible candidate stops
immediately; a transient failure consumes one of three persisted attempts
per candidate; a bad candidate always keeps the old cache. An empty or
retired-only tree is valid and empties ordinary search, while every
historical revision body stays readable by pinned reference.

Unsupported old layouts (e.g. ``corpus/``) fail loudly instead of looking
like a valid empty new feed. Sync runs standalone (``sync --config``) with
community contribution off; it never starts the maintenance service or a
model, and it is bounded to 30 seconds per attempt.
"""

from __future__ import annotations

import re
import os
import time
from pathlib import Path

from . import documents
from .locks import StartInProgress, StartLock
from .store import digest
from mindie_knowledge.community.common import CommunityError, run_argv

MAX_FILES = 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
ATTEMPT_LIMIT = 3
ATTEMPT_SECONDS = 30
_ENTRY_RE = re.compile(r"^(cases|topics)/[^/]+\.md$")
_FEEDBACK_RE = re.compile(r"^feedback/[^/]+\.json$")
_METADATA_FILES = {"README.md", "README", "AGENTS.md", "LICENSE", "LICENSE.md",
                   "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md"}


def feed_ident(repository, ref, prefix=""):
    return digest([repository, ref, prefix])


class Feed:
    def __init__(self, store, config):
        self.store = store
        self.config = dict(config)
        if config.get("domain") != store.domain:
            raise ValueError("feed must explicitly select this domain")
        repository, ref = config.get("repository"), config.get("ref", "main")
        if (
            not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(ref, str)
            or not ref
        ):
            raise ValueError("feed requires a GitHub owner/repository and ref")
        self.repository, self.ref = repository, ref
        self.prefix = (config.get("prefix") or "").strip("/")
        self.url = config.get("url") or f"https://github.com/{repository}.git"
        self.ident = feed_ident(repository, ref, self.prefix)
        self.dir = store.root / "feed" / self.ident
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = self.dir / "repo.git"

    # -------------------------------------------------------------- git I/O

    def _git(self, *args, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("knowledge sync attempt exceeded 30 seconds")
        maximum = documents.MAX_FILE_BYTES + 8192 if args[0] == "cat-file" else 1024 * 1024
        try:
            completed = run_argv(
            ["git", "-C", str(self.repo), *args],
                timeout=min(remaining, 25), max_output=maximum, input_bytes=b"",
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull},
            )
        except CommunityError as exc:
            raise OSError(f"bounded git {args[0]} failed: {exc}") from exc
        if completed.timed_out:
            raise TimeoutError(f"git {args[0]} exceeded the sync deadline")
        if completed.code != 0:
            raise OSError(
                f"git {args[0]} failed: {completed.err_text[:300]}"
            )
        return completed.out

    def _clone(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("knowledge sync attempt exceeded 30 seconds")
        try:
            completed = run_argv(
                ["git", "clone", "--bare", "--quiet", "--depth=1", "--single-branch",
                 "--branch", self.ref, self.url, str(self.repo)],
                timeout=min(remaining, 25), max_output=65536, input_bytes=b"",
                env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull},
            )
        except CommunityError as exc:
            raise OSError(f"bounded git clone failed: {exc}") from exc
        if completed.timed_out:
            raise TimeoutError("git clone exceeded the sync deadline")
        if completed.code != 0:
            raise OSError(
                f"git clone failed: {completed.err_text[:300]}"
            )

    # ----------------------------------------------------------------- sync

    def _candidate(self):
        return self.store.feed_get(f"feed-candidate:{self.ident}") or {}

    def _save_candidate(self, value):
        self.store.feed_set(f"feed-candidate:{self.ident}", value)

    def _validate_tree(self, commit, deadline):
        """Read and validate the complete candidate tree; raises ValueError
        for incompatible content (no retry) and OSError/TimeoutError for
        transient failures (consumes one attempt)."""
        listing = self._git("ls-tree", "-r", "--long", commit, "--", self.prefix or ".",
                            deadline=deadline)
        entries, total = [], 0
        seen = set()
        for line in listing.decode("utf-8", "strict").splitlines():
            try:
                head, path = line.split("\t", 1)
                mode, _type, _sha, size = head.split()
            except ValueError:
                raise ValueError("unparseable git tree listing")
            if self.prefix:
                if not path.startswith(self.prefix + "/"):
                    continue
                path = path[len(self.prefix) + 1:]
            if not size.isdigit():
                raise ValueError("git tree entry without a size")
            size_i = int(size)
            total += size_i
            if _ENTRY_RE.match(path):
                if mode != "100644":
                    raise ValueError(
                        f"entry {path} has unsafe Git mode {mode}; only regular "
                        "non-executable 100644 blobs are admitted"
                    )
                if size_i > documents.MAX_FILE_BYTES:
                    raise ValueError(f"entry {path} exceeds the byte limit")
                entries.append((path, size_i))
            elif _FEEDBACK_RE.match(path) or not path.endswith(".md"):
                # Feedback belongs to the repository side; non-Markdown files
                # (workflows, scripts) are not knowledge content.
                continue
            elif path in _METADATA_FILES or path.startswith("docs/"):
                # Repository metadata Markdown is never knowledge content.
                continue
            else:
                # Markdown outside cases/topics — e.g. the old corpus/ layout —
                # is an unsupported tree, never a valid empty new feed.
                raise ValueError(
                    f"unsupported content layout at {path!r}; not a canonical feed"
                )
        if len(entries) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise ValueError("feed exceeds the bounded size limits")
        docs = []
        for path, size in sorted(entries):
            raw = self._git("cat-file", "blob", f"{commit}:{self.prefix + '/' if self.prefix else ''}{path}",
                            deadline=deadline)
            if len(raw) != size:
                raise ValueError(f"blob size mismatch for {path}")
            doc = documents.parse_entry(raw)
            if doc["domain"] != self.store.domain:
                raise ValueError(f"entry {path} belongs to another domain")
            if doc["kind"] == "knowledge" and (not doc["conditions"] or not doc["sources"]):
                raise ValueError("knowledge requires sources and applicability")
            if doc["entry_id"] in seen:
                raise ValueError("duplicate entry identity in feed")
            seen.add(doc["entry_id"])
            docs.append(doc)
        return docs

    def sync(self, *, force=False):
        receipt_key = "feed:" + self.ident
        receipt = self.store.feed_get(receipt_key) or {}
        lock = StartLock(self.dir / "sync.lock")
        try:
            lock.acquire()
        except StartInProgress:
            return dict(status="busy", repository=self.repository,
                        retained_commit=receipt.get("commit"))
        try:
            deadline = time.monotonic() + ATTEMPT_SECONDS
            discovery_key = f"feed-discovery:{self.ident}"
            discovery = self.store.feed_get(discovery_key) or {}
            if force:
                discovery = {}  # explicit operator action, never the updater path
            if discovery.get("failures", 0) >= ATTEMPT_LIMIT:
                return dict(status="exhausted", repository=self.repository,
                            retained_commit=receipt.get("commit"),
                            detail="remote discovery failed three times; explicit sync --resume required")
            discovery["failures"] = discovery.get("failures", 0) + 1
            self.store.feed_set(discovery_key, discovery)  # consume a crash before discovery
            try:
                if not self.repo.is_dir():
                    self._clone(deadline)
                self._git("fetch", "--quiet", "origin", self.ref, deadline=deadline)
                commit = self._git("rev-parse", "FETCH_HEAD", deadline=deadline)
                commit = commit.decode().strip()
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise OSError("remote ref did not resolve to a commit")
            except (OSError, TimeoutError) as exc:
                self.store.feed_set(receipt_key, dict(
                    receipt, status="unavailable", repository=self.repository,
                    detail=str(exc)[:300], retained_commit=receipt.get("commit"),
                    checked=time.time(),
                ))
                return self.store.feed_get(receipt_key)
            self.store.feed_set(discovery_key, {"failures": 0, "commit": commit})
            if receipt.get("commit") == commit and not force:
                self.store.feed_set(receipt_key, dict(
                    receipt, status="unchanged", checked=time.time()))
                return self.store.feed_get(receipt_key)
            candidate = self._candidate()
            if candidate.get("commit") != commit:
                candidate = {"commit": commit, "attempts": 0, "status": "new"}
            if candidate.get("status") == "invalid":
                return dict(status="invalid", repository=self.repository,
                            commit=commit, detail=candidate.get("detail", ""),
                            retained_commit=receipt.get("commit"))
            if candidate.get("attempts", 0) >= ATTEMPT_LIMIT:
                return dict(status="exhausted", repository=self.repository,
                            commit=commit, retained_commit=receipt.get("commit"))
            candidate["attempts"] = candidate.get("attempts", 0) + 1
            self._save_candidate(candidate)  # persisted before any work
            try:
                docs = self._validate_tree(commit, deadline)
            except ValueError as exc:
                candidate.update(status="invalid", detail=str(exc)[:300])
                self._save_candidate(candidate)
                return dict(status="invalid", repository=self.repository,
                            commit=commit, detail=str(exc)[:300],
                            retained_commit=receipt.get("commit"))
            except (OSError, TimeoutError) as exc:
                candidate.update(status="unavailable", detail=str(exc)[:300])
                self._save_candidate(candidate)
                self.store.feed_set(receipt_key, dict(
                    receipt, status="unavailable", repository=self.repository,
                    detail=str(exc)[:300], retained_commit=receipt.get("commit"),
                    checked=time.time(),
                ))
                return self.store.feed_get(receipt_key)
            installed = self.store.install_feed(docs, feed_ident=self.ident)
            receipt = dict(
                status="synced", repository=self.repository, ref=self.ref,
                prefix=self.prefix, commit=commit, entries=installed["entries"],
                retired=sum(1 for d in docs if d["status"] == "retired"),
                attempts=candidate["attempts"], checked=time.time(),
            )
            self.store.feed_set(receipt_key, receipt)
            self._save_candidate({"commit": commit, "attempts": 0, "status": "ok"})
            return receipt
        finally:
            lock.release()
