"""Model-free knowledge sync from the canonical Git publication.

The configured content repository's branch is followed as one immutable Git
commit whose tree holds canonical ``mindie-entry/2`` documents under
``cases/`` and ``topics/`` (``feedback/*.json`` belongs to the repository
side and is ignored here). Every candidate commit is fully verified before
the atomic switch: layout, per-file platform envelope, UTF-8/LF bytes,
schema, revisions, domain and entry identities. There is no whole-feed
entry-count or total-byte business cap: blobs are read one at a time through
a bounded persistent ``git cat-file --batch`` process, and each verified blob
is checkpointed against the exact candidate commit in short local
transactions, so an attempt that hits the per-attempt deadline resumes after
the last staged blob — never restarting at item zero, never holding a SQLite
write transaction across Git reads — and the visible feed switches atomically
only once the candidate is complete. A structurally
incompatible candidate is quarantined permanently against its immutable
commit; a transient failure (OSError/timeout) persists its attempt count and
a backoff ``next_check`` and is retried automatically once due — never a
permanent exhaustion after three attempts. A bad candidate always keeps the
old cache. An empty tree is valid and empties ordinary search — withdrawal
is deletion from the tree — while every historical revision body stays
readable by pinned reference with an explicit withdrawn flag.

Remote discovery never latches permanently: each sync makes one bounded
discovery pass, and after three consecutive transport failures it defers
ordinary calls for one hour (the updater's established post-failure
cadence). Once that time is due, an ordinary call discovers again; a
successful discovery clears the transient failure state. An explicit
``sync --resume`` skips both deferrals and rechecks now, but never
revalidates an invalid candidate.

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
from .store import _backoff_seconds, digest
from mindie_knowledge.community.common import CommunityError, run_argv
from mindie_knowledge.gitread import with_windows_longpaths


def _feed_git_env():
    """hooksPath suppression, plus Windows long paths before clone exists."""
    return with_windows_longpaths({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
    })

ATTEMPT_SECONDS = 30
# Repeated discovery failure defers ordinary calls for one hour, the
# updater's established post-failure cadence; it is not a new quota.
DISCOVERY_BACKOFF_SECONDS = 3600
DISCOVERY_FAILURE_LIMIT = 3
# Transient candidate-validation failures resume with exponential backoff
# (one minute doubling to one hour), persisted across restarts.
CANDIDATE_BACKOFF_BASE = 60
CANDIDATE_BACKOFF_CAP = 3600
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
        maximum = 1024 * 1024
        try:
            completed = run_argv(
            ["git", "-C", str(self.repo), *args],
                timeout=min(remaining, 25), max_output=maximum, input_bytes=b"",
                env=_feed_git_env(),
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
        url = self.url
        if "://" not in url and not url.startswith("git@"):
            # A local path with --depth goes through git's local-copy path,
            # which mishandles packed/multi-pack-index sources; file:// uses
            # the same pack protocol as a real remote (git's own advice).
            url = Path(url).resolve().as_uri()
        try:
            completed = run_argv(
                ["git", "clone", "--bare", "--quiet", "--depth=1", "--single-branch",
                 "--branch", self.ref, url, str(self.repo)],
                timeout=min(remaining, 25), max_output=65536, input_bytes=b"",
                env=_feed_git_env(),
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

    def _validate_listing(self, commit, deadline):
        """Eager metadata pass: paths, modes, per-file sizes, layout.

        Returns ``[(path, size), ...]`` (small per-entry metadata, not
        bodies). Raises ValueError for incompatible content (quarantined, no
        retry) and OSError/TimeoutError for transient failures. The listing
        itself is written to a scratch file and parsed in bounded chunks, so
        a growing catalogue has no fixed whole-tree output cap.
        """
        from mindie_knowledge.gitread import iter_file_records, run_stdout_to_file

        listing_path = self.dir / "listing.tmp"
        run_stdout_to_file(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "--long", commit,
             "--", self.prefix or "."],
            listing_path,
            timeout=max(1.0, deadline - time.monotonic()),
            env=_feed_git_env(),
        )
        try:
            records = iter_file_records(listing_path, separator=b"\n")
            entries = []
            for raw_record in records:
                line = raw_record.decode("utf-8", "strict")
                if not line:
                    continue
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
        finally:
            try:
                listing_path.unlink()
            except OSError:
                pass
        return sorted(entries)

    def _stage_tree_docs(self, commit, entries, deadline):
        """Fetch and validate candidate blobs, checkpointing per blob.

        Verified progress is persisted per blob against the exact candidate
        commit, so an attempt that hits the 30-second deadline (or any
        transient failure) resumes after the last staged blob instead of
        restarting at item zero. Reads go through one bounded persistent
        ``git cat-file --batch`` process — never a per-blob spawn and never a
        whole-library materialization. Raises ValueError for incompatible
        content (quarantined, no retry) and OSError/TimeoutError for
        transient failures (backoff, progress kept).
        """
        from mindie_knowledge.gitread import CatFileBatch

        staged_paths, seen_ids = self.store.feed_staging_state(self.ident, commit)
        reader = None
        staged = 0
        try:
            for path, size in entries:
                if path in staged_paths:
                    continue
                if reader is None:
                    reader = CatFileBatch(self.repo, env=_feed_git_env())
                raw = reader.read(
                    f"{commit}:{self.prefix + '/' if self.prefix else ''}{path}",
                    deadline=deadline, max_bytes=documents.MAX_FILE_BYTES,
                )
                if raw is None:
                    raise OSError(f"listed blob is unreadable in the local clone: {path}")
                if len(raw) != size:
                    raise ValueError(f"blob size mismatch for {path}")
                doc = documents.parse_entry(raw)
                if doc["domain"] != self.store.domain:
                    raise ValueError(f"entry {path} belongs to another domain")
                if doc["entry_id"] in seen_ids:
                    raise ValueError("duplicate entry identity in feed")
                seen_ids.add(doc["entry_id"])
                self.store.feed_stage_doc(self.ident, commit, path, doc["entry_id"], doc)
                staged += 1
        finally:
            if reader is not None:
                reader.close()
        return staged

    @staticmethod
    def _candidate_backoff(attempts):
        return _backoff_seconds(
            CANDIDATE_BACKOFF_BASE, CANDIDATE_BACKOFF_CAP, attempts
        )

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
            failures = discovery.get("failures", 0)
            next_check = discovery.get("next_check", 0)
            # Deferral, never a permanent latch: state exhausted before this
            # change carries no next_check and is due immediately.
            if (
                not force  # explicit operator action, never the updater path
                and failures >= DISCOVERY_FAILURE_LIMIT
                and next_check > time.time()
            ):
                return dict(
                    status="deferred", repository=self.repository,
                    retained_commit=receipt.get("commit"), next_check=next_check,
                    detail="remote discovery backs off after repeated failures; "
                           "explicit sync --resume checks now",
                )
            failures += 1
            record = {"failures": failures}
            if failures >= DISCOVERY_FAILURE_LIMIT:
                record["next_check"] = time.time() + DISCOVERY_BACKOFF_SECONDS
            self.store.feed_set(discovery_key, record)  # consume a crash before discovery
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
                cleaned = dict(receipt)
                cleaned.pop("detail", None)
                cleaned.pop("retained_commit", None)
                cleaned["status"] = "unchanged"
                cleaned["checked"] = time.time()
                self.store.feed_set(receipt_key, cleaned)
                return self.store.feed_get(receipt_key)
            candidate = self._candidate()
            if candidate.get("commit") != commit:
                candidate = {"commit": commit, "attempts": 0, "status": "new"}
            if candidate.get("status") == "invalid":
                return dict(status="invalid", repository=self.repository,
                            commit=commit, detail=candidate.get("detail", ""),
                            retained_commit=receipt.get("commit"))
            # A transient validation failure never exhausts permanently: it
            # persists a backoff next_check and ordinary sync retries the same
            # candidate once due. An explicit resume skips the wait.
            next_validation = candidate.get("next_check") or 0
            if not force and next_validation > time.time():
                return dict(
                    status="deferred", repository=self.repository,
                    commit=commit, retained_commit=receipt.get("commit"),
                    next_check=next_validation,
                    detail="candidate validation backs off after a transient "
                           "failure; explicit sync --resume checks now",
                )
            candidate["attempts"] = candidate.get("attempts", 0) + 1
            self._save_candidate(candidate)  # persisted before any work
            try:
                listing = self._validate_listing(commit, deadline)
                staged = self._stage_tree_docs(commit, listing, deadline)
                # The candidate switched atomically only when every blob of
                # the exact commit was verified and staged; the switch itself
                # is a short local transaction with no Git IO inside.
                installed = self.store.install_feed(
                    self.store.feed_staged_docs(self.ident, commit),
                    feed_ident=self.ident,
                )
                self.store.feed_staging_clear(self.ident)
            except ValueError as exc:
                candidate.update(status="invalid", detail=str(exc)[:300])
                candidate.pop("next_check", None)
                self._save_candidate(candidate)
                self.store.feed_staging_clear(self.ident)
                return dict(status="invalid", repository=self.repository,
                            commit=commit, detail=str(exc)[:300],
                            retained_commit=receipt.get("commit"))
            except (OSError, TimeoutError) as exc:
                remaining = self.store.feed_staging_count(self.ident, commit)
                candidate.update(
                    status="unavailable", detail=str(exc)[:300],
                    staged=remaining,
                    next_check=(time.time()
                                + self._candidate_backoff(candidate["attempts"])),
                )
                self._save_candidate(candidate)
                self.store.feed_set(receipt_key, dict(
                    receipt, status="unavailable", repository=self.repository,
                    detail=str(exc)[:300], retained_commit=receipt.get("commit"),
                    staged=remaining, checked=time.time(),
                ))
                return self.store.feed_get(receipt_key)
            receipt = dict(
                status="synced", repository=self.repository, ref=self.ref,
                prefix=self.prefix, commit=commit, entries=installed["entries"],
                attempts=candidate["attempts"], checked=time.time(),
            )
            self.store.feed_set(receipt_key, receipt)
            self._save_candidate({"commit": commit, "attempts": 0, "status": "ok"})
            return receipt
        finally:
            lock.release()
