"""``submit_batch`` / ``reconcile_batch``: the outbound publication machine.

Ordering guarantees, all backed by the durable ledger in ``state_dir``:

1. The sharing gate is re-read before Git mutation and again before every
   single outbound request; ``disabled`` stops without touching anything.
2. The publication intent row and step receipts land BEFORE the effect they
   name. A crash between steps leaves an ``intent``/``unknown`` row, which is
   resolved by bounded read-only reconciliation — never by blind re-writes.
3. Idempotence keys on the candidate digest (content revision), not on event
   identity: a resubmitted batch with the same revision returns the recorded
   receipt; a failed or unresolved revision is never automatically resent.
4. Our own open PR is updated in place (new commit on top, no force push);
   a merged prior PR plus a genuine new delta creates a follow-up PR that
   references the merged one. Conflicts and base divergence park the batch as
   ``needs_review`` without overwriting anyone's edits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import gitops
from .batch import (
    check_path,
    contribution_branch,
    merge_votes,
    render_commit_message,
    render_feedback,
    render_pr_body,
    render_pr_title,
    scan_outbound,
    validate_batch,
    validate_feedback,
)
from .common import (
    CommunityError,
    Deadline,
    TransientError,
    UnknownOutcome,
    finite_epoch,
    sha256_text,
)
from .ledger import Ledger
from .settings import live_gate, sharing_enabled
from .transport import Transport, transport_from_settings
from mindie_knowledge.loop import documents

RECEIPT_STATUSES = (
    "submitted",
    "updated",
    "unchanged",
    "unknown",
    "failed",
    "rejected",
    "disabled",
    "needs_review",
    "unavailable",
)


def _valid_sha(value) -> bool:
    """A present Git object name: 40-char SHA-1 or 64-char SHA-256 hex."""
    if not isinstance(value, str) or len(value) not in (40, 64):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value)


def _receipt(
    batch: Mapping[str, Any],
    *,
    status: str,
    detail: str = "",
    pr_url: str | None = None,
    head_sha: str | None = None,
    extras: Mapping[str, Any] | None = None,
    retry_at: float | None = None,
) -> dict[str, Any]:
    out = {
        "status": status,
        "batch_id": batch.get("batch_id"),
        "revision": batch.get("revision"),
        "pr_url": pr_url,
        "head_sha": head_sha,
        "detail": detail[:800],
    }
    when = finite_epoch(retry_at)
    if when is not None:
        out["retry_at"] = when
    if extras:
        out.update(extras)
    return out


def _settings_gate(settings: Mapping[str, Any]) -> None:
    """Re-read the live sharing config; called before every external effect."""
    live_gate(settings)


def submit_batch(
    batch: dict,
    settings: dict,
    state_dir: Path,
    *,
    cancel: Any = None,
    transport: Transport | None = None,
) -> dict:
    """Publish one contribution batch through Git + GitHub, idempotently."""
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir)
    try:
        return _submit(batch, settings, state_dir, ledger, cancel=cancel, transport=transport)
    finally:
        ledger.close()


def _submit(batch, settings, state_dir, ledger, *, cancel, transport) -> dict[str, Any]:
    if not sharing_enabled(settings):
        return _receipt(batch if isinstance(batch, Mapping) else {}, status="disabled",
                        detail="community sharing is disabled or unconfigured")
    checked = validate_batch(batch)
    deadline = Deadline(
        settings.get("transaction_seconds", 120),
        settings.get("operation_limit", 60),
        cancel=cancel,
    )
    transport = transport or transport_from_settings(settings, state_dir)
    repository = settings["repository"]
    write_repo = settings.get("fork") or repository
    remote_url = _remote_url(settings, write_repo)
    branch = contribution_branch(checked["domain"], checked["batch_id"])
    batch_id, revision = checked["batch_id"], checked["revision"]

    # A new event id alone is not new content: the digest owns idempotence.
    existing = ledger.get_publication(batch_id, revision) or ledger.find_by_revision(revision)
    if existing is not None:
        status = existing["status"]
        if status in ("submitted", "updated", "unchanged"):
            return _receipt(checked, status="unchanged" if status != "unchanged" else status,
                            detail="identical revision already published",
                            pr_url=existing.get("pr_url"), head_sha=existing.get("head_sha"),
                            extras={"files": Ledger.parse_actual_files(existing)})
        if status == "needs_review":
            return ledger.receipt(existing)
        if status == "rejected" and not checked["explicit_retry"]:
            # The PR was closed unmerged: the same content is never
            # resurrected by an automatic resubmission. Only an explicit
            # operator retry of the stored payload may re-attempt it.
            return _receipt(checked, status="rejected",
                            detail="this revision's contribution was rejected (PR closed "
                                   "unmerged); it is not resurrected automatically. "
                                   + (existing.get("detail") or ""),
                            pr_url=existing.get("pr_url"), head_sha=existing.get("head_sha"))
        if status == "failed" and not checked["explicit_retry"]:
            return _receipt(checked, status="failed",
                            detail="this revision already failed; it is not resent automatically. "
                                   + (existing.get("detail") or ""),
                            pr_url=existing.get("pr_url"), head_sha=existing.get("head_sha"))
        if status in ("intent", "unknown"):
            resolved = _resolve_unknown(ledger, existing, settings, state_dir, transport, deadline)
            if resolved["status"] in ("submitted", "updated"):
                return _receipt(checked, status=resolved["status"],
                                detail="resolved by read-only reconciliation",
                                pr_url=resolved.get("pr_url"), head_sha=resolved.get("head_sha"),
                                extras={"files": resolved.get("files")})
            if not checked["explicit_retry"]:
                return resolved
            # Explicit retry of a still-unknown/failed revision: step receipts of
            # completed earlier stages are preserved; git push of an identical
            # commit is a no-op, and a moved remote branch parks needs_review.
        # "unavailable" deliberately falls through: the earlier attempt failed
        # in the environment before any uncertain write, so the same stored
        # operation is retried (bounded by the caller's persisted backoff).

    # Reconcile before considering a new revision of the same lineage.
    for row in ledger.unresolved_for_batch(batch_id):
        if row["revision"] == revision:
            continue
        resolved = _resolve_unknown(ledger, row, settings, state_dir, transport, deadline)
        if resolved["status"] == "unknown":
            return _receipt(checked, status="unknown",
                            detail="an older revision of this batch is still unresolved; "
                                   "not starting new work until it is reconciled",
                            pr_url=row.get("pr_url"), head_sha=row.get("head_sha"),
                            retry_at=resolved.get("retry_at"))

    pr_title = render_pr_title(checked)
    pr_body = render_pr_body(checked)
    commit_message = render_commit_message(checked)
    try:
        scan_outbound(checked, pr_title, pr_body, commit_message)
    except CommunityError as exc:
        # Recorded as a failed attempt: the identical revision is not resent.
        ledger.record_intent(batch_id=batch_id, revision=revision,
                             domain=checked["domain"], repository=repository, branch=branch)
        ledger.finish_publication(batch_id, revision, status="failed", detail=str(exc))
        return _receipt(checked, status="failed", detail=str(exc))

    # Read-only ownership lookup before the intent: an open PR pins its existing
    # head branch (in-place update); a merged prior PR gets a fresh branch for
    # the genuine delta, never a rewrite of the merged branch. A rejected
    # (closed unmerged) PR's branch is likewise retired with it: new material
    # continues on a fresh branch — never resurrecting rejected content and
    # never a non-fast-forward fight with the retired branch.
    try:
        prior = _own_prior_pr(ledger, batch_id, transport, repository, branch, deadline)
    except TransientError as exc:
        # A failed read is not "no existing PR" and must not start a write.
        return _receipt(checked, status="unavailable", detail=str(exc), retry_at=exc.retry_at)
    except UnknownOutcome as exc:
        return _receipt(checked, status="unknown", detail=str(exc), retry_at=exc.retry_at)
    except CommunityError as exc:
        # Auth or any other unfinished lookup is not "there is no PR".
        return _receipt(
            checked,
            status=exc.status if exc.status in RECEIPT_STATUSES else "failed",
            detail=str(exc),
            retry_at=exc.retry_at,
        )
    if prior and prior.get("state") == "open":
        branch = prior["head"]["ref"]
    elif prior:
        branch = f"{branch}-{revision[:8]}"

    ledger.record_intent(
        batch_id=batch_id, revision=revision, domain=checked["domain"],
        repository=repository, branch=branch,
    )
    try:
        result = _publish(
            checked, settings, state_dir, ledger, deadline, transport,
            repository=repository, write_repo=write_repo, remote_url=remote_url,
            branch=branch, pr_title=pr_title, pr_body=pr_body, commit_message=commit_message,
            prior=prior,
        )
    except UnknownOutcome as exc:
        ledger.finish_publication(batch_id, revision, status="unknown", detail=str(exc))
        return _receipt(checked, status="unknown", detail=str(exc), retry_at=exc.retry_at)
    except CommunityError as exc:
        ledger.finish_publication(
            batch_id, revision,
            status=exc.status if exc.status in RECEIPT_STATUSES else "failed",
            detail=str(exc),
        )
        return _receipt(checked, status=exc.status, detail=str(exc), retry_at=exc.retry_at)
    ledger.finish_publication(
        batch_id, revision, status=result["status"], detail=result.get("detail", ""),
        pr_number=result.get("pr_number"), pr_url=result.get("pr_url"),
        head_sha=result.get("head_sha"),
    )
    return _receipt(checked, status=result["status"], detail=result.get("detail", ""),
                    pr_url=result.get("pr_url"), head_sha=result.get("head_sha"),
                    extras={"files": result.get("files")})


def _publish(checked, settings, state_dir, ledger, deadline, transport, *,
             repository, write_repo, remote_url, branch, pr_title, pr_body, commit_message,
             prior):
    batch_id, revision = checked["batch_id"], checked["revision"]
    base_branch = settings.get("branch", "main")

    ledger.record_step(batch_id, revision, "gate:before-git")
    _settings_gate(settings)
    ledger.record_step(batch_id, revision, "git:clone-fetch")
    genv = gitops.git_env(settings)
    work_dir = gitops.ensure_clone(
        remote_url, state_dir / "git" / write_repo.replace("/", "_"), deadline, env=genv
    )

    # Upstream main is the content authority. The fork is only the push target.
    read_url = _remote_url(settings, repository)
    base_tip = gitops.remote_tip(read_url, f"refs/heads/{base_branch}", deadline, env=genv)
    if base_tip is None:
        raise CommunityError(f"base branch {base_branch} not found on {repository}")
    if write_repo != repository:
        base_tip = gitops.fetch_ref(
            work_dir, read_url, f"refs/heads/{base_branch}", deadline, env=genv
        )
    if checked["base_commit"] and checked["base_commit"] != base_tip:
        ledger.record_step(batch_id, revision, "git:base-diverged",
                           f"batch base {checked['base_commit'][:12]} != remote {base_tip[:12]}")

    updating = bool(prior and prior.get("state") == "open")
    merged_prior = prior if prior and prior.get("merged") else None

    ledger.record_step(batch_id, revision, "gate:before-worktree")
    _settings_gate(settings)
    resumed = False
    own_resume = False
    if updating:
        # No checkout, commit, push, or PR write until the API head and the
        # upstream PR ref are the same commit. A retained fork branch is not
        # a substitute, for a fork or for a same-repository PR.
        proven = _proven_open_head(
            work_dir, prior, branch, settings, read_url, deadline, genv
        )
        gitops.checkout_new(work_dir, branch, proven, deadline, env=genv)
    elif checked["explicit_retry"] and gitops.remote_tip(remote_url, f"refs/heads/{branch}", deadline, env=genv):
        # Explicit retry with the branch already pushed: resume from the remote
        # tip and its recorded steps instead of re-pushing earlier commits.
        gitops.checkout_existing(work_dir, branch, deadline, env=genv)
        resumed = True
        ledger.record_step(batch_id, revision, "git:resumed-branch", branch)
    else:
        # A prior attempt of THIS lineage may have pushed the branch before
        # failing (e.g. auth refused the PR creation). Building on top of our
        # own proven pushed tip keeps the write fast-forward; a tip that is
        # not ours is a real divergence and parks as needs_review on push.
        tip = gitops.remote_tip(remote_url, f"refs/heads/{branch}", deadline, env=genv)
        if tip is not None and any(
            _expected_head(ledger, row) == tip
            for row in ledger.all_for_batch(batch_id)
        ):
            gitops.checkout_existing(work_dir, branch, deadline, env=genv)
            own_resume = True
            ledger.record_step(batch_id, revision, "git:resume-own-branch", tip)
        else:
            gitops.checkout_new(work_dir, branch, base_tip, deadline, env=genv)

    files, conflict = _prepare_files(checked, work_dir, own_branch_resume=own_resume)
    if conflict:
        raise CommunityError(conflict, status="needs_review")

    if files:
        ledger.record_step(batch_id, revision, "git:apply-files", f"{len(files)} file(s)")
        gitops.apply_files(work_dir, files)
    ledger.record_step(batch_id, revision, "git:commit")
    committed = gitops.stage_and_commit(work_dir, [f["path"] for f in files], commit_message, deadline, env=genv)
    # Persist the actual committed identities BEFORE any uncertain external
    # step (push, API), so a lost response can still recover exactly what the
    # branch holds. These are worktree bytes, never the candidate payload.
    actual_files = _actual_entry_files(checked, work_dir)
    ledger.record_actual_files(batch_id, revision, actual_files)
    if committed is None:
        head_sha = gitops.current_head(work_dir, deadline, env=genv)
        if _valid_sha(head_sha):
            ledger.record_step(batch_id, revision, "git:intended-head", head_sha)
        if updating:
            ledger.record_step(batch_id, revision, "gate:before-metadata")
            _settings_gate(settings)
            transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
            return {"status": "unchanged", "detail": "no content change; PR metadata refreshed",
                    "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha,
                    "files": actual_files}
        if resumed:
            # The exact commits are already on the remote branch; only the PR
            # step is outstanding. Skip the push entirely and create the PR.
            pass
        else:
            return {"status": "unchanged",
                    "detail": "content already present on the target branch",
                    "pr_number": (prior or {}).get("number"), "pr_url": (prior or {}).get("html_url"),
                    "head_sha": head_sha, "files": actual_files}
    else:
        head_sha = committed
        ledger.record_step(batch_id, revision, "gate:before-push")
        _settings_gate(settings)
        # Persist the exact commit we intend to land independently of any later
        # observed PR head (reconcile must never treat a rewritten head_sha as
        # this revision's expected commit).
        ledger.record_step(batch_id, revision, "git:intended-head", head_sha)
        ledger.record_step(batch_id, revision, "git:push")
        gitops.push_branch(work_dir, branch, deadline, env=genv, cancel=deadline.cancel)
        ledger.record_step(batch_id, revision, "git:pushed", head_sha)

    ledger.record_step(batch_id, revision, "gate:before-api")
    _settings_gate(settings)
    if updating:
        ledger.record_step(batch_id, revision, "github:update-pr", f"#{prior['number']}")
        transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
        return {"status": "updated", "detail": f"updated open PR #{prior['number']}",
                "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha,
                "files": actual_files}

    if merged_prior:
        pr_body = pr_body + f"\nFollow-up to merged PR #{merged_prior['number']}.\n"
    ledger.record_step(batch_id, revision, "github:create-pr")
    head_ref = branch if write_repo == repository else f"{write_repo.split('/')[0]}:{branch}"
    pr = transport.create_pull_request(
        write_repo if write_repo == repository else repository,
        title=pr_title, body=pr_body, head=head_ref, base=base_branch, deadline=deadline,
    )
    ledger.record_step(batch_id, revision, "github:pr-created", f"#{pr.get('number')}")
    return {"status": "submitted", "detail": f"opened PR #{pr.get('number')}",
            "pr_number": pr.get("number"), "pr_url": pr.get("html_url"), "head_sha": head_sha,
            "files": actual_files}


def _actual_entry_files(checked, work_dir):
    """Actual per-file committed identities, always read from worktree bytes.

    After apply/commit the worktree IS what the branch holds, for written,
    identical-skipped and merge-no-change paths alike — never the candidate
    payload. Observation identity stays the marker set; these hashes and
    revisions are only its committed-content evidence.
    """
    actual = []
    for item in checked["files"]:
        path = item["path"]
        if path.startswith("feedback/"):
            continue
        text = gitops.read_tree_file(work_dir, path)
        if text is None:
            continue
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            revision = documents.parse_entry(normalized)["revision"]
        except ValueError:
            revision = None
        actual.append({
            "path": path,
            "sha256": sha256_text(normalized),
            "revision": revision,
        })
    return actual


def _merge_observations(current_body, our_content, sent_markers, path):
    """Apply only the not-yet-sent observation delta onto the current body.

    ``current_body`` is the actual current remote content (possibly
    bot/maintainer edited) and stays authoritative — header, base text and
    every remotely-present block included; only observation blocks whose
    marker identity is neither confirmed-sent (``sent_markers``, the durable
    sending history) nor already present remotely are appended. A block the
    remote removed stays removed: its marker is in ``sent_markers``, so it is
    never re-attached. Returns the merged content, or None when either side
    does not parse as the same entry — the caller parks the batch instead of
    overwriting. No Git-history search: confirmed observation identities plus
    the current remote body are the whole mechanism.
    """
    try:
        current_doc = documents.parse_entry(current_body)
        our_doc = documents.parse_entry(our_content)
    except ValueError:
        return None
    expected_id = path.rsplit("/", 1)[-1].removesuffix(".md")
    if current_doc["entry_id"] != expected_id or our_doc["entry_id"] != expected_id:
        return None
    our_split = documents.split_observations(our_doc["content"])
    if our_split is None:
        return None  # opaque observation tail: never guess a delta
    _base, our_blocks = our_split
    confirmed = set(sent_markers)
    remote_markers = set(documents.observation_markers(current_doc["content"]))
    merged = current_doc["content"].rstrip()
    for marker, addition in our_blocks:
        if marker in confirmed or marker in remote_markers:
            continue  # already submitted (possibly removed upstream) or present
        merged = documents.append_observation_text(merged, addition, marker)
    merged_doc = dict(current_doc, content=merged)
    merged_doc["revision"] = documents.revision_of(merged_doc)
    # Canonical render: for an unchanged body this reproduces the exact remote
    # bytes, so the caller's hash comparison cleanly detects "nothing to do".
    return documents.render_entry(merged_doc)


def _prepare_files(checked, work_dir, *, own_branch_resume=False):
    """Decide per-file content, merging votes and refusing silent overwrites.

    Returns (files, conflict_detail). A file whose remote content moved away
    from the declared base (bot edits, maintainer fixes) is reconciled by
    applying only the not-yet-sent observation delta onto the CURRENT remote
    body — the remote content (header included) stays authoritative and an
    old full submitted draft is never restored over it; feedback files merge
    votes by vote_id deterministically. Without confirmed observation
    identities for the entry the batch parks as a conflict instead of
    guessing a delta. ``own_branch_resume`` means the checkout is proven (by
    durable ledger push receipts) to hold only this lineage's own
    never-confirmed push and no PR was ever created for it, so a differing
    existing file is our own earlier content and the fuller candidate simply
    replaces it.
    """
    out = []
    for item in checked["files"]:
        path = item["path"]
        current_sha = gitops.tree_sha256(work_dir, path)
        if current_sha is None:
            if item["base_sha256"] is not None and not path.startswith("feedback/"):
                return None, f"{path} was deleted from the remote; not restoring withdrawn content"
            out.append(item)
            continue
        if current_sha == item["sha256"]:
            continue  # identical content already present
        if item["base_sha256"] is None and own_branch_resume:
            # The existing bytes are this lineage's own earlier, never
            # PR-confirmed push (no PR exists to have been edited on): the
            # newer candidate supersedes it as the first creation.
            out.append(item)
            continue
        if (
            item["base_sha256"] is not None
            and current_sha == item["base_sha256"]
            and item.get("sent_markers") is None
        ):
            # Declared-base update with no confirmed observation history:
            # the direct batch contract writes the full candidate content.
            # Core-produced updates always carry marker history and merge
            # below instead, so a bot-edited remote body is never overwritten
            # by an old full draft.
            out.append(item)
            continue
        if path.startswith("feedback/"):
            current = gitops.read_tree_file(work_dir, path)
            existing = validate_feedback(current, path)["votes"] if current else []
            new = validate_feedback(item["content"], path)["votes"]
            merged = render_feedback(merge_votes(existing, new))
            if sha256_text(merged) != current_sha:
                out.append({**item, "content": merged, "sha256": sha256_text(merged)})
            continue
        # Entry file whose remote moved from the declared base (or whose base
        # proof predates marker receipts): apply only the unsent observation
        # delta onto the current remote content.
        if item["base_sha256"] is not None and item.get("sent_markers") is not None:
            current = gitops.read_tree_file(work_dir, path)
            if current is None:
                return None, f"{path} became unreadable on the remote; not overwriting"
            current = current.replace("\r\n", "\n").replace("\r", "\n")
            merged = _merge_observations(
                current, item["content"], item["sent_markers"], path
            )
            if merged is not None:
                # Write even when the merge reproduces the current bytes:
                # the staged diff is then empty and the commit step reports
                # "no change" instead of misreading an empty file list as a
                # commit. The receipt identity is read from the worktree.
                out.append({**item, "content": merged,
                            "sha256": sha256_text(merged)})
                continue
        return None, (
            f"{path} changed away from the declared base on the remote "
            "(possible bot or maintainer edit) and no verifiable observation "
            "delta could be applied; not overwriting"
        )
    return out, None


def _contribution_head(pr, settings) -> bool:
    """True when this PR head repository is the configured contribution.

    The fork account is that repository when publication pushes to a fork;
    otherwise it is the upstream repository itself. A missing repository
    name is not proof of ownership.
    """
    head = pr.get("head") if isinstance(pr, Mapping) else None
    if not isinstance(head, Mapping):
        return False
    repo_info = head.get("repo")
    repo = repo_info.get("full_name") if isinstance(repo_info, Mapping) else None
    if not isinstance(repo, str) or "/" not in repo:
        return False
    fork = settings.get("fork") if isinstance(settings, Mapping) else None
    expected = fork or (settings.get("repository") if isinstance(settings, Mapping) else None)
    if repo != expected:
        return False
    ref = head.get("ref")
    return isinstance(ref, str) and bool(ref)


def _proven_open_head(work_dir, prior, branch, settings, read_url, deadline, env) -> str:
    """SHA of an open PR only when ownership, API SHA, and upstream ref agree.

    A missing number, linkage, or head SHA, and a fetched ref that is not the
    API head, are unread checks: nothing was pushed. They stay
    ``unavailable`` so the same batch can be submitted again. A head that was
    read and belongs to someone else is a refusal, not a network failure.
    The fetch's own exception is kept. No branch is used as a substitute.
    """
    number = prior.get("number") if isinstance(prior, Mapping) else None
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise TransientError("open PR number is unreadable; not updating")
    head = prior.get("head") if isinstance(prior, Mapping) else None
    if not isinstance(head, Mapping):
        raise TransientError("open PR head is unreadable; not updating")
    ref = head.get("ref")
    repo_info = head.get("repo")
    repo = repo_info.get("full_name") if isinstance(repo_info, Mapping) else None
    if not isinstance(ref, str) or not ref or not isinstance(repo, str) or "/" not in repo:
        raise TransientError("open PR linkage is unreadable; not updating")
    if ref != branch or not _owned_head(prior, branch, settings) or not _contribution_head(prior, settings):
        raise CommunityError(
            "open PR head is not the configured contribution; not updating",
            status="failed",
        )
    api_sha = head.get("sha")
    if not _valid_sha(api_sha):
        raise TransientError("open PR has no valid head SHA; not updating")
    fetched = gitops.fetch_ref(
        work_dir, read_url, f"refs/pull/{number}/head", deadline, env=env
    )
    if fetched.lower() != api_sha.lower():
        raise TransientError(
            "fetched upstream PR head does not match the API head; not updating"
        )
    return fetched


def _pr_number_absent(exc: BaseException) -> bool:
    """True when this number was looked up and is not a pull request.

    Auth failures and unreadable responses are unfinished evidence, not proof
    that no contribution PR exists.
    """
    text = str(exc).lower()
    return "not found" in text or "no such pr" in text or "http 404" in text


def _own_prior_pr(ledger, batch_id, transport, repository, branch, deadline):
    """Locate our own contribution PR: ledger first, bounded lookup second.

    Only PRs whose head branch is our deterministic contribution branch are
    considered ours; unrelated PRs are never touched.
    """
    numbers = {
        row["pr_number"]
        for row in ledger.all_for_batch(batch_id)
        if row.get("pr_number")
    }
    found = []
    for number in sorted(numbers):
        try:
            found.append(transport.get_pull_request(repository, number, deadline))
        except TransientError:
            raise
        except UnknownOutcome:
            raise
        except CommunityError as exc:
            # A missing number is absence of that PR. Any other read failure
            # is unfinished evidence and must not become "no existing PR".
            if _pr_number_absent(exc):
                continue
            raise
    if not found:
        found = transport.find_pull_requests(
            repository, head_branch=branch, deadline=deadline
        )
    own = [pr for pr in found if pr.get("head", {}).get("ref") == branch]
    open_pr = next((pr for pr in own if pr.get("state") == "open"), None)
    if open_pr:
        return open_pr
    merged = [pr for pr in own if pr.get("merged")]
    return merged[-1] if merged else (own[-1] if own else None)


def _owned_head(pr, branch, settings) -> bool:
    """True when this PR head is our configured contribution, not a same-named branch."""
    head = pr.get("head") or {}
    if head.get("ref") != branch:
        return False
    repo = (head.get("repo") or {}).get("full_name")
    fork = (settings or {}).get("fork")
    if fork and repo and repo != fork:
        return False
    return True


def _attach_retry(receipt: dict, retry_at) -> dict:
    when = finite_epoch(retry_at)
    if when is None:
        return receipt
    return dict(receipt, retry_at=when)


def _expected_head(ledger, row) -> str | None:
    """The exact commit this revision intended to land.

    Taken only from the durable intended-push steps — never from the
    ``head_sha`` column, which reconcile may have overwritten with a later
    observed remote head that is not this revision.
    """
    steps = ledger.steps_for(row["batch_id"], row["revision"])
    for name in ("git:pushed", "git:intended-head"):
        matches = [s for s in steps if s["step"] == name and _valid_sha(s.get("detail"))]
        if matches:
            return matches[-1]["detail"]
    return None


def _verdict_for_pr(pr, expected_head) -> tuple[str, str]:
    """Confirmation verdict for one located PR.

    Confirmation requires a present, valid remote head SHA that equals the
    saved expected commit. A matching branch/PR number alone is not proof
    this revision arrived. A closed unmerged PR is a proven content-level
    rejection: ``rejected``, so the contributor side quarantines exactly
    that material instead of resurrecting it with a new PR. Unavailable or
    incomplete evidence stays ``unknown``.
    """
    remote_head = (pr.get("head") or {}).get("sha")
    if pr.get("state") != "open" and not pr.get("merged"):
        return "rejected", "reconciled: PR closed unmerged"
    if not _valid_sha(expected_head):
        return "unknown", "no saved expected head; this revision is not confirmed"
    if not _valid_sha(remote_head):
        return "unknown", (
            "remote PR has no valid head SHA; this revision is not confirmed"
        )
    if remote_head != expected_head:
        return "unknown", (
            "remote head does not match the saved expected head; "
            "this revision is not confirmed"
        )
    if pr.get("merged"):
        return "submitted", "reconciled: PR was merged at the expected head"
    return "submitted", "reconciled: PR is open at the expected head"


_INSPECT_DETAIL = (
    "remote outcome remains unconfirmed; each scheduler opportunity runs one "
    "bounded read-only check with a persisted backoff. Unknown never becomes "
    "confirmed failure from exhaustion; explicit inspection stays available "
    "as contribution-inspect / contribution-reconcile"
)


def _resolve_unknown(ledger, row, settings, state_dir, transport, deadline) -> dict[str, Any]:
    """Bounded READ-ONLY reconciliation of our own branch/PR. Never creates.

    Exactly one bounded lookup per call; the caller (the outbox worker or an
    explicit operator) spaces calls with a persisted backoff — there is no
    attempt cap that would permanently stop reconciliation: unavailable
    evidence stays ``unknown``, never converts into proven failure. Only a
    proven remote rejection (closed unmerged PR) becomes ``rejected``.
    Confirmation requires either the exact expected head or, when the PR head
    has since advanced (bot/maintainer commits, merge), Git proof that the
    expected commit is an ancestor of the current head — our exact write
    reached our exact PR. Branch existence alone never confirms.
    """
    target = row["repository"]
    expected = _expected_head(ledger, row)
    ledger.record_step(row["batch_id"], row["revision"], "reconcile:attempt")
    status = "unknown"
    detail = _INSPECT_DETAIL
    pr_url = row.get("pr_url")
    retry_at = None
    try:
        prs = []
        if row.get("pr_number"):
            try:
                prs.append(transport.get_pull_request(target, row["pr_number"], deadline))
            except TransientError:
                raise
            except UnknownOutcome:
                raise
            except CommunityError:
                pass
        if not prs:
            prs = transport.find_pull_requests(target, head_branch=row["branch"], deadline=deadline)
        own = [pr for pr in prs if _owned_head(pr, row["branch"], settings)]
        if own:
            pr = own[0]
            pr_url = pr.get("html_url") or pr_url
            status, detail = _verdict_for_pr(pr, expected)
            remote_head = (pr.get("head") or {}).get("sha")
            if (
                status == "unknown"
                and _valid_sha(expected)
                and _valid_sha(remote_head)
                and remote_head != expected
            ):
                verdict = _ancestor_verdict(
                    row, settings, state_dir, expected, pr, deadline
                )
                if verdict is True:
                    status = "submitted"
                    detail = (
                        "confirmed: the expected commit is an ancestor of the "
                        "current PR head (the write landed; the head advanced)"
                    )
                elif verdict is False:
                    detail = (
                        "expected commit is not part of the current PR head; "
                        "not confirmed"
                    )
    except TransientError as exc:
        # A failed read does not clear an earlier uncertain write, and it
        # does not authorize another POST. retry_at only delays the next read.
        detail = f"reconciliation lookup failed: {exc}"
        retry_at = exc.retry_at
    except UnknownOutcome as exc:
        detail = f"reconciliation lookup failed: {exc}"
        retry_at = exc.retry_at
    except CommunityError as exc:
        detail = f"reconciliation lookup failed: {exc}"
    ledger.finish_publication(
        row["batch_id"], row["revision"], status=status, detail=detail,
        pr_url=pr_url, head_sha=expected,
    )
    return _attach_retry(
        ledger.receipt(ledger.get_publication(row["batch_id"], row["revision"])),
        retry_at,
    )


def _inspect_row(ledger, row, settings, state_dir, transport, deadline) -> dict[str, Any]:
    """EXPLICIT bounded read-only inspection of one row in ANY unresolved
    state (intent/unknown/failed/rejected, including cap-exhausted rows).

    Verifies the exact saved expected PR head before confirming; a merged PR
    whose head moved past the expected commit is confirmed only when the
    expected commit is a proven ancestor of the merged head (the branch may
    be deleted after merge, so ancestry uses the durable refs/pull/N/head
    plus an exact-commit fetch). Lookup failures, mismatches and
    no-evidence stay ``unknown`` — a historical falsely-failed cap row is
    not kept failed. Only a proven closed unmerged PR is ``rejected``.
    """
    ledger.record_step(row["batch_id"], row["revision"], "inspect:attempt")
    status = "unknown"
    detail = "explicit inspection found no confirming remote evidence"
    expected = _expected_head(ledger, row)
    pr_url = row.get("pr_url")
    target = row["repository"]
    retry_at = None
    try:
        prs = []
        if row.get("pr_number"):
            try:
                prs.append(transport.get_pull_request(target, row["pr_number"], deadline))
            except TransientError:
                raise
            except UnknownOutcome:
                raise
            except CommunityError:
                pass
        if not prs and row.get("branch"):
            prs = transport.find_pull_requests(target, head_branch=row["branch"], deadline=deadline)
        own = [pr for pr in prs if _owned_head(pr, row["branch"], settings)]
        if not own:
            detail = (
                "no matching remote PR evidence; not confirmed "
                "(unknown, not a proven failure)"
            )
        else:
            pr = own[0]
            pr_url = pr.get("html_url") or pr_url
            remote_head = (pr.get("head") or {}).get("sha")
            status, detail = _verdict_for_pr(pr, expected)
            if (
                status == "unknown"
                and _valid_sha(expected)
                and _valid_sha(remote_head)
                and remote_head != expected
            ):
                # Head moved after the write (in-place update, bot/maintainer
                # commits, merge) and the branch may be deleted after merge:
                # prove the expected commit is an ancestor of the current head
                # (durable refs/pull/N/head plus an exact-commit fetch).
                verdict = _ancestor_verdict(
                    row, settings, state_dir, expected, pr, deadline
                )
                if verdict is True:
                    status = "submitted"
                    detail = ("confirmed by ancestry proof: expected head "
                              "is an ancestor of the current PR head")
                elif verdict is False:
                    status, detail = "unknown", (
                        "expected head is not part of the current PR head; not confirmed"
                    )
    except TransientError as exc:
        status, detail = "unknown", f"explicit inspection lookup failed: {exc}"
        retry_at = exc.retry_at
    except UnknownOutcome as exc:
        status, detail = "unknown", f"explicit inspection lookup failed: {exc}"
        retry_at = exc.retry_at
    except CommunityError as exc:
        status, detail = "unknown", f"explicit inspection lookup failed: {exc}"
    ledger.finish_publication(
        row["batch_id"], row["revision"], status=status, detail=detail,
        pr_url=pr_url, head_sha=expected,
    )
    return _attach_retry(
        ledger.receipt(ledger.get_publication(row["batch_id"], row["revision"])),
        retry_at,
    )


def _ancestor_verdict(row, settings, state_dir, expected, pr, deadline) -> bool | None:
    """True only when upstream's fetched PR head is the API head and contains
    the expected commit. A fork branch or an exact-commit fetch is not proof."""
    target = row["repository"]
    head = pr.get("head") or {}
    if not _owned_head(pr, row.get("branch"), settings):
        return None
    api_sha = head.get("sha")
    if not pr.get("number") or not _valid_sha(api_sha):
        return None
    genv = gitops.git_env(settings)
    work_dir = gitops.ensure_clone(
        _remote_url(settings, target),
        Path(state_dir) / "git" / str(target).replace("/", "_"),
        deadline, env=genv,
    )
    # The PR ref is the only head under test. Fetching the expected SHA, or
    # an old branch that still points at it, does not prove the API head.
    try:
        fetched = gitops.fetch_pr_head(
            work_dir, pr["number"], row.get("branch") or "", deadline, env=genv,
            remote_url=_remote_url(settings, target),
        )
    except TransientError:
        raise
    except CommunityError:
        return None
    if fetched != api_sha:
        return None
    return gitops.is_ancestor(work_dir, expected, fetched, deadline, env=genv)


def inspect_batch(batch_id: str, settings: dict, state_dir: Path, *, transport: Transport | None = None) -> dict:
    """Explicit post-exhaustion inspection: bounded, read-only on the remote,
    verifies the exact expected PR head, updates the durable ledger.

    Works on rows in any unresolved state — including rows a previous
    automatic budget falsely marked failed — because an exhausted lookup
    budget is not proof of failure. No-evidence stays ``unknown``. Retry
    remains reserved for proven remote failures."""
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir)
    try:
        if not sharing_enabled(settings):
            return {"status": "disabled", "batch_id": batch_id, "revision": None,
                    "pr_url": None, "head_sha": None,
                    "detail": "community sharing is disabled or unconfigured"}
        transport = transport or transport_from_settings(settings, state_dir)
        deadline = Deadline(settings.get("transaction_seconds", 120),
                            settings.get("operation_limit", 60))
        rows = [
            row for row in ledger.all_for_batch(batch_id)
            if row["status"] in ("intent", "unknown", "failed", "rejected")
        ]
        if not rows:
            latest = ledger.latest_for_batch(batch_id)
            if latest is None:
                return {"status": "unknown", "batch_id": batch_id, "revision": None,
                        "pr_url": None, "head_sha": None,
                        "detail": "no publication record for this batch"}
            return ledger.receipt(latest)
        result = None
        for row in rows:
            result = _inspect_row(ledger, row, settings, state_dir, transport, deadline)
        return result
    finally:
        ledger.close()


def reconcile_batch(batch_id: str, settings: dict, state_dir: Path, *, transport: Transport | None = None) -> dict:
    """Read-only reconciliation of every unresolved intent of one batch id."""
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir)
    try:
        if not sharing_enabled(settings):
            return {"status": "disabled", "batch_id": batch_id, "revision": None,
                    "pr_url": None, "head_sha": None,
                    "detail": "community sharing is disabled or unconfigured"}
        transport = transport or transport_from_settings(settings, state_dir)
        deadline = Deadline(settings.get("transaction_seconds", 120),
                            settings.get("operation_limit", 60))
        unresolved = ledger.unresolved_for_batch(batch_id)
        if not unresolved:
            latest = ledger.latest_for_batch(batch_id)
            if latest is None:
                return {"status": "unknown", "batch_id": batch_id, "revision": None,
                        "pr_url": None, "head_sha": None, "detail": "no publication record for this batch"}
            return ledger.receipt(latest)
        result = None
        for row in unresolved:
            result = _resolve_unknown(ledger, row, settings, state_dir, transport, deadline)
            if result["status"] == "unknown":
                break
        return result
    finally:
        ledger.close()


def _remote_url(settings: Mapping[str, Any], write_repo: str) -> str:
    dev = settings.get("dev_remotes") or {}
    if write_repo in dev:
        return dev[write_repo]
    return f"https://github.com/{write_repo}.git"
