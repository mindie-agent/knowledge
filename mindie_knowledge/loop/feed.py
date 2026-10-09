"""Model-free Git synchronization of complete current task file packages.

Candidate bytes are ordinary temporary files, bound to one immutable commit.
All packages validate before the material store switches its current snapshot.
SQLite keeps progress and receipts only. Superseded references expire; neither
sync nor index installation calls a model or republishes consumer material.
"""

from __future__ import annotations

import re
import hashlib
import shutil
import os
import time
from pathlib import Path

from . import documents
from mindie_knowledge.materials.store import MaterialCleanupError
from .locks import StartInProgress, StartLock
from .store import _backoff_seconds, digest
from mindie_knowledge.community.common import CommunityError, run_argv
from mindie_knowledge.gitread import with_windows_longpaths, _check_cancel
from mindie_knowledge.publication_contract import read_git_contract


def _feed_git_env():
    """hooksPath suppression, plus Windows long paths before clone exists."""
    return with_windows_longpaths({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
    })

# Repeated discovery failure defers ordinary calls for one hour, the
# updater's established post-failure cadence; it is not a new quota.
DISCOVERY_BACKOFF_SECONDS = 3600
DISCOVERY_FAILURE_LIMIT = 3
# Transient candidate-validation failures resume with exponential backoff
# (one minute doubling to one hour), persisted across restarts.
CANDIDATE_BACKOFF_BASE = 60
CANDIDATE_BACKOFF_CAP = 3600
_ENTRY_RE = re.compile(r"^tasks/[0-9a-f]{64}/(?:index\.md|blocks/[A-Za-z0-9_-]{1,128}\.md)$")
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
        self.contract_sha256 = config.get("contract_sha256")
        if self.contract_sha256 is not None and (
            not isinstance(self.contract_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.contract_sha256)
        ):
            raise ValueError("feed contract_sha256 must be a complete SHA256 digest")
        self.prefix = (config.get("prefix") or "").strip("/")
        self.url = config.get("url") or f"https://github.com/{repository}.git"
        self.ident = feed_ident(repository, ref, self.prefix)
        self.dir = store.root / "feed" / self.ident
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = self.dir / "repo.git"
        self._cancel = None

    # -------------------------------------------------------------- git I/O

    def _git(self, *args, deadline=None):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("knowledge sync exceeded the caller's explicit timeout")
        maximum = 1024 * 1024
        try:
            completed = run_argv(
            ["git", "-C", str(self.repo), *args],
                timeout=remaining, max_output=maximum, input_bytes=b"",
                env=_feed_git_env(), cancel=self._cancel,
            )
        except CommunityError as exc:
            _check_cancel(self._cancel)
            raise OSError(f"bounded git {args[0]} failed: {exc}") from exc
        if completed.timed_out:
            raise TimeoutError(f"git {args[0]} exceeded the sync deadline")
        if completed.code != 0:
            raise OSError(
                f"git {args[0]} failed: {completed.err_text[:300]}"
            )
        return completed.out

    def _clone(self, deadline=None):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("knowledge sync exceeded the caller's explicit timeout")
        url = self.url
        if "://" not in url and not url.startswith("git@"):
            # A local path with --depth goes through git's local-copy path,
            # which mishandles packed/multi-pack-index sources; file:// uses
            # the same pack protocol as a real remote (git's own advice).
            url = Path(url).resolve().as_uri()
        try:
            completed = run_argv(
                ["git", "clone", "--bare", "--quiet", "--depth=1", "--single-branch", "--no-tags",
                 "--branch", self.ref, url, str(self.repo)],
                timeout=remaining, max_output=65536, input_bytes=b"",
                env=_feed_git_env(), cancel=self._cancel,
            )
        except CommunityError as exc:
            _check_cancel(self._cancel)
            raise OSError(f"bounded git clone failed: {exc}") from exc
        if completed.timed_out:
            raise TimeoutError("git clone exceeded the sync deadline")
        if completed.code != 0:
            raise OSError(
                f"git clone failed: {completed.err_text[:300]}"
            )

    # ----------------------------------------------------------------- sync

    def _retain_tip(self, commit, deadline):
        """Prune history in this private read cache, under the sync lock.

        The installed database does not depend on old Git objects. Keep only
        the candidate tip, including when a candidate later fails validation.
        Interrupted cleanup is retried on the next sync, not in a busy loop.
        """
        key = 'feed-git-tip:' + self.ident
        if self.store.feed_get(key) == {'commit': commit}:
            return
        tip = 'refs/heads/mindie-current'
        self._git('update-ref', tip, commit, deadline=deadline)
        self._git('symbolic-ref', 'HEAD', tip, deadline=deadline)
        refs = self._git('for-each-ref', '--format=%(refname)', deadline=deadline)
        for ref in refs.decode().splitlines():
            if ref != tip:
                self._git('update-ref', '-d', ref, deadline=deadline)
        self._git('reflog', 'expire', '--expire=now', '--all', deadline=deadline)
        self._git('gc', '--prune=now', '--quiet', deadline=deadline)
        self.store.feed_set(key, {'commit': commit})

    def _candidate(self):
        return self.store.feed_get(f"feed-candidate:{self.ident}") or {}

    def _save_candidate(self, value):
        self.store.feed_set(f"feed-candidate:{self.ident}", value)

    def _validate_listing(self, commit, deadline):
        """Eager metadata pass: paths, modes, per-file sizes, layout.

        Returns ``[(path, size, Git object identity), ...]`` (small per-entry metadata, not
        bodies). Raises ValueError for incompatible content (quarantined, no
        retry) and OSError/TimeoutError for transient failures. The listing
        itself is written to a scratch file and parsed in bounded chunks, so
        a growing catalogue has no fixed whole-tree output cap.
        """
        from mindie_knowledge.gitread import iter_file_records, run_stdout_to_file

        listing_path = self.dir / "listing.tmp"
        run_stdout_to_file(
            ["git", "-C", str(self.repo), "ls-tree", "-r", "-z", "--long", commit,
             "--", self.prefix or "."],
            listing_path,
            timeout=None if deadline is None else max(0, deadline - time.monotonic()),
            env=_feed_git_env(), cancel=self._cancel,
        )
        records = None
        try:
            records = iter_file_records(listing_path)
            entries = []
            for raw_record in records:
                _check_cancel(self._cancel)
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
                if _ENTRY_RE.fullmatch(path):
                    if mode != "100644":
                        raise ValueError(
                            f"entry {path} has unsafe Git mode {mode}; only regular "
                            "non-executable 100644 blobs are admitted"
                        )
                    if size_i > documents.MAX_FILE_BYTES:
                        raise ValueError(f"entry {path} exceeds the byte limit")
                    entries.append((path, size_i, _sha))
                elif path.startswith("tasks/"):
                    raise ValueError(f"unsupported task package path {path!r}")
                elif _FEEDBACK_RE.fullmatch(path) or not path.endswith(".md"):
                    # Feedback belongs to the repository side; non-Markdown files
                    # (workflows, scripts) are not knowledge content.
                    continue
                elif path in _METADATA_FILES or path.startswith("docs/"):
                    # Repository metadata Markdown is never knowledge content.
                    continue
                else:
                    # Markdown outside task packages — e.g. the old corpus/ layout —
                    # is an unsupported tree, never a valid empty new feed.
                    raise ValueError(
                        f"unsupported content layout at {path!r}; not a canonical feed"
                    )
        except (ValueError, OSError, TimeoutError) as exc:
            if records is not None:
                records.close()
            try:
                listing_path.unlink(missing_ok=True)
            except OSError as cleanup:
                # No feed switch has happened. Keep its original invalid or
                # unavailable outcome and both reasons if scratch cleanup fails.
                error = ValueError if isinstance(exc, ValueError) else OSError
                raise error(f"{exc}; listing cleanup also failed: {type(cleanup).__name__}: {cleanup}") from exc
            raise
        else:
            records.close()
            listing_path.unlink(missing_ok=True)
        return sorted(entries)

    def _staging_dir(self, commit):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("invalid feed commit identity")
        return self.dir / "staging" / commit

    @staticmethod
    def _matches_blob(raw, size, object_id):
        if len(raw) != size:
            return False
        hashing = hashlib.sha1 if len(object_id) == 40 else hashlib.sha256
        return hashing(b"blob " + str(size).encode("ascii") + b"\0" + raw).hexdigest() == object_id

    def _stage_tree_docs(self, commit, entries, deadline):
        """Checkpoint checked blobs as files, without any database body mirror."""
        from mindie_knowledge.gitread import CatFileBatch

        staging = self._staging_dir(commit)
        reader = None
        staged = 0
        try:
            for path, size, object_id in entries:
                _check_cancel(self._cancel)
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("task package staging exceeded the sync deadline")
                target = staging / path
                if target.is_symlink():
                    raise ValueError("feed staging contains an unsafe symlink")
                if target.is_file() and self._matches_blob(target.read_bytes(), size, object_id):
                    staged += 1
                    continue
                if reader is None:
                    reader = CatFileBatch(self.repo, env=_feed_git_env())
                raw = reader.read(object_id, deadline=deadline, max_bytes=documents.MAX_FILE_BYTES,
                                  cancel=self._cancel)
                if raw is None:
                    raise OSError(f"listed blob is unreadable in the local clone: {path}")
                if not self._matches_blob(raw, size, object_id):
                    raise ValueError(f"blob identity mismatch for {path}")
                text = raw.decode("utf-8", "strict")
                if "\r" in text or "\x00" in text:
                    raise ValueError(f"task package is not canonical UTF-8/LF text: {path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_bytes(raw)
                temporary.replace(target)
                staged += 1
        finally:
            if reader is not None:
                reader.close()
        return staged

    def _packages(self, commit, entries):
        from mindie_knowledge.materials import validate_package_files
        from mindie_knowledge.materials.file_source import FileContents

        grouped = {}
        for path, _size, _object_id in entries:
            _, task_id, relative = path.split("/", 2)
            grouped.setdefault(task_id, []).append(relative)
        staging = self._staging_dir(commit)
        for task_id, names in sorted(grouped.items()):
            files = FileContents.from_paths(staging / 'tasks' / task_id, names)
            checked = validate_package_files(files, self.store.domain)
            package = checked
            if not package["ready"]:
                raise ValueError("published task package has unfinished material indexes")
            if package["task_id"] != task_id:
                raise ValueError("task package identity differs from its directory")
            yield package

    def _clear_staging(self):
        staging = self.dir / "staging"
        if staging.exists():
            shutil.rmtree(staging)

    def _staged_count(self, commit):
        directory = self._staging_dir(commit)
        return sum(1 for _ in directory.rglob("*.md")) if directory.exists() else 0

    def _cleanup_receipt(self, receipt, *, retry_material=False):
        errors = []
        if retry_material and receipt.get("material_cleanup_error"):
            try:
                self.store.retry_feed_cleanup()
                receipt.pop("material_cleanup_error", None)
            except (OSError, MaterialCleanupError) as exc:
                receipt["material_cleanup_error"] = str(exc)[:300]
        if receipt.get("material_cleanup_error"):
            errors.append("material cleanup: " + receipt["material_cleanup_error"])
        try:
            self._clear_staging()
        except OSError as exc:
            errors.append("staging cleanup: " + str(exc)[:300])
        if errors:
            receipt.update(cleanup_status="failed", cleanup_error="; ".join(errors), cleanup_errors=errors)
        else:
            for key in ("cleanup_status", "cleanup_error", "cleanup_errors"):
                receipt.pop(key, None)
        return receipt

    @staticmethod
    def _partial_promotion(commit, repository, exc):
        return dict(status="partial", repository=repository, commit=commit,
                    metadata_committed=True, failed_stage="material-promotion",
                    detail=f"Task metadata committed; material promotion failed: {type(exc).__name__}: {exc}"[:500])

    @staticmethod
    def _candidate_backoff(attempts):
        return _backoff_seconds(
            CANDIDATE_BACKOFF_BASE, CANDIDATE_BACKOFF_CAP, attempts
        )

    def sync(self, *, force=False, cancel=None, timeout=None):
        if timeout is not None and (type(timeout) not in (int, float) or timeout <= 0):
            raise ValueError("explicit sync timeout must be positive")
        _check_cancel(cancel)
        receipt_key = "feed:" + self.ident
        receipt = self.store.feed_get(receipt_key) or {}
        lock = StartLock(self.dir / "sync.lock")
        try:
            lock.acquire()
        except StartInProgress:
            return dict(status="busy", repository=self.repository,
                        retained_commit=receipt.get("commit"))
        try:
            self._cancel = cancel
            deadline = None if timeout is None else time.monotonic() + timeout
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
                self._git("fetch", "--quiet", "--depth=1", "--no-tags", "origin", self.ref, deadline=deadline)
                commit = self._git("rev-parse", "FETCH_HEAD", deadline=deadline)
                commit = commit.decode().strip()
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                    raise OSError("remote ref did not resolve to a commit")
                self._retain_tip(commit, deadline)
            except InterruptedError:
                raise
            except (OSError, TimeoutError) as exc:
                self.store.feed_set(receipt_key, dict(
                    receipt, status="unavailable", repository=self.repository,
                    detail=str(exc)[:300], retained_commit=receipt.get("commit"),
                    checked=time.time(),
                ))
                return self.store.feed_get(receipt_key)
            self.store.feed_set(discovery_key, {"failures": 0, "commit": commit})
            if (receipt.get("commit") == commit and receipt.get("contract_sha256")
                    and receipt.get("contract_requirement") == self.contract_sha256 and not force):
                cleaned = dict(receipt)
                cleaned.pop("detail", None)
                cleaned.pop("retained_commit", None)
                cleaned["status"] = "unchanged"
                cleaned["checked"] = time.time()
                self._cleanup_receipt(cleaned, retry_material=True)
                self.store.feed_set(receipt_key, cleaned)
                return cleaned
            candidate = self._candidate()
            if (candidate.get("commit") != commit
                    or candidate.get("contract_requirement", "") != self.contract_sha256):
                candidate = {"commit": commit, "attempts": 0, "status": "new",
                             "contract_requirement": self.contract_sha256}
            if candidate.get("status") == "invalid":
                result = dict(status="invalid", repository=self.repository,
                              commit=commit, detail=candidate.get("detail", ""),
                              retained_commit=receipt.get("commit"))
                if candidate.get("error_code"):
                    result.update(error_code=candidate["error_code"], failed_stage="publication-contract")
                return result
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
                contract = read_git_contract(
                    self.repo, commit, self.store.domain, prefix=self.prefix,
                    expected_sha256=self.contract_sha256, deadline=deadline, env=_feed_git_env(), cancel=cancel,
                )
                listing = self._validate_listing(commit, deadline)
                staged = self._stage_tree_docs(commit, listing, deadline)
                # The candidate switched atomically only when every blob of
                # the exact commit was verified and staged; the switch itself
                # is a short local transaction with no Git IO inside.
                packages = self._packages(commit, listing)
                installed = self.store.install_feed(packages, feed_ident=self.ident, source_revision=commit)
            except MaterialCleanupError as exc:
                # The new pointers and metadata are already committed. Preserve
                # this outcome separately from retiring superseded local files.
                installed = dict(entries=len({path.split('/')[1] for path, _, _ in listing}),
                                 cleanup_error=str(exc)[:300])
            except ValueError as exc:
                if getattr(exc, "metadata_committed", False):
                    return self._partial_promotion(commit, self.repository, exc)
                candidate.update(status="invalid", detail=str(exc)[:300])
                if getattr(exc, "code", None):
                    candidate["error_code"] = exc.code
                candidate.pop("next_check", None)
                self._save_candidate(candidate)
                result = dict(status="invalid", repository=self.repository,
                              commit=commit, detail=str(exc)[:300],
                              retained_commit=receipt.get("commit"))
                if getattr(exc, "code", None):
                    result.update(error_code=exc.code, failed_stage="publication-contract")
                try:
                    self._clear_staging()
                except OSError as cleanup:
                    result.update(cleanup_status="failed", cleanup_error=str(cleanup)[:300])
                return result
            except InterruptedError:
                raise
            except (OSError, TimeoutError) as exc:
                if getattr(exc, "metadata_committed", False):
                    return self._partial_promotion(commit, self.repository, exc)
                remaining = self._staged_count(commit)
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
            except Exception as exc:
                if getattr(exc, "metadata_committed", False):
                    return self._partial_promotion(commit, self.repository, exc)
                raise
            receipt = dict(
                status="synced", repository=self.repository, ref=self.ref,
                prefix=self.prefix, commit=commit, entries=installed["entries"],
                attempts=candidate["attempts"], checked=time.time(),
                contract_sha256=contract["sha256"], contract_requirement=self.contract_sha256,
            )
            if installed.get("cleanup_error"):
                receipt["material_cleanup_error"] = installed["cleanup_error"]
            self._cleanup_receipt(receipt)
            try:
                self.store.feed_set(receipt_key, receipt)
                self._save_candidate({"commit": commit, "attempts": 0, "status": "ok",
                                      "contract_requirement": self.contract_sha256})
            except Exception as exc:
                # The file snapshot is already installed. Report its known
                # outcome even when saving the secondary sync receipt fails.
                receipt.update(receipt_status="failed", receipt_error=f"{type(exc).__name__}: {exc}"[:300])
            return receipt
        finally:
            self._cancel = None
            lock.release()
