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
from .common import CommunityError, Deadline, UnknownOutcome
from .ledger import Ledger
from .settings import live_gate, sharing_enabled
from .transport import Transport, transport_from_settings

RECEIPT_STATUSES = (
    "submitted",
    "updated",
    "unchanged",
    "unknown",
    "failed",
    "disabled",
    "needs_review",
)


def _receipt(
    batch: Mapping[str, Any],
    *,
    status: str,
    detail: str = "",
    pr_url: str | None = None,
    head_sha: str | None = None,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "status": status,
        "batch_id": batch.get("batch_id"),
        "revision": batch.get("revision"),
        "pr_url": pr_url,
        "head_sha": head_sha,
        "detail": detail[:800],
    }
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
                            pr_url=existing.get("pr_url"), head_sha=existing.get("head_sha"))
        if status == "needs_review":
            return ledger.receipt(existing)
        if status == "failed" and not checked["explicit_retry"]:
            return _receipt(checked, status="failed",
                            detail="this revision already failed; it is not resent automatically. "
                                   + (existing.get("detail") or ""),
                            pr_url=existing.get("pr_url"), head_sha=existing.get("head_sha"))
        if status in ("intent", "unknown"):
            resolved = _resolve_unknown(ledger, existing, settings, transport, deadline)
            if resolved["status"] in ("submitted", "updated"):
                return _receipt(checked, status=resolved["status"],
                                detail="resolved by read-only reconciliation",
                                pr_url=resolved.get("pr_url"), head_sha=resolved.get("head_sha"))
            if not checked["explicit_retry"]:
                return resolved
            # Explicit retry of a still-unknown/failed revision: step receipts of
            # completed earlier stages are preserved; git push of an identical
            # commit is a no-op, and a moved remote branch parks needs_review.

    # Reconcile before considering a new revision of the same lineage.
    for row in ledger.unresolved_for_batch(batch_id):
        if row["revision"] == revision:
            continue
        resolved = _resolve_unknown(ledger, row, settings, transport, deadline)
        if resolved["status"] == "unknown":
            return _receipt(checked, status="unknown",
                            detail="an older revision of this batch is still unresolved; "
                                   "not starting new work until it is reconciled",
                            pr_url=row.get("pr_url"), head_sha=row.get("head_sha"))

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
    # the genuine delta, never a rewrite of the merged branch.
    prior = _own_prior_pr(ledger, batch_id, transport, repository, branch, deadline)
    if prior and prior.get("state") == "open":
        branch = prior["head"]["ref"]
    elif prior and prior.get("merged"):
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
        return _receipt(checked, status="unknown", detail=str(exc))
    except CommunityError as exc:
        ledger.finish_publication(
            batch_id, revision,
            status=exc.status if exc.status in RECEIPT_STATUSES else "failed",
            detail=str(exc),
        )
        return _receipt(checked, status=exc.status, detail=str(exc))
    ledger.finish_publication(
        batch_id, revision, status=result["status"], detail=result.get("detail", ""),
        pr_number=result.get("pr_number"), pr_url=result.get("pr_url"),
        head_sha=result.get("head_sha"),
    )
    return _receipt(checked, status=result["status"], detail=result.get("detail", ""),
                    pr_url=result.get("pr_url"), head_sha=result.get("head_sha"))


def _publish(checked, settings, state_dir, ledger, deadline, transport, *,
             repository, write_repo, remote_url, branch, pr_title, pr_body, commit_message,
             prior):
    batch_id, revision = checked["batch_id"], checked["revision"]
    base_branch = settings.get("branch", "main")

    ledger.record_step(batch_id, revision, "gate:before-git")
    _settings_gate(settings)
    ledger.record_step(batch_id, revision, "git:clone-fetch")
    work_dir = gitops.ensure_clone(
        remote_url, state_dir / "git" / write_repo.replace("/", "_"), deadline
    )

    base_tip = gitops.remote_tip(remote_url, f"refs/heads/{base_branch}", deadline)
    if base_tip is None:
        raise CommunityError(f"base branch {base_branch} not found on the write remote")
    if checked["base_commit"] and checked["base_commit"] != base_tip:
        ledger.record_step(batch_id, revision, "git:base-diverged",
                           f"batch base {checked['base_commit'][:12]} != remote {base_tip[:12]}")

    updating = bool(prior and prior.get("state") == "open")
    merged_prior = prior if prior and prior.get("merged") else None

    ledger.record_step(batch_id, revision, "gate:before-worktree")
    _settings_gate(settings)
    resumed = False
    if updating:
        if not gitops.checkout_existing(work_dir, branch, deadline):
            raise CommunityError("our open PR branch is missing on the remote", status="needs_review")
    elif checked["explicit_retry"] and gitops.remote_tip(remote_url, f"refs/heads/{branch}", deadline):
        # Explicit retry with the branch already pushed: resume from the remote
        # tip and its recorded steps instead of re-pushing earlier commits.
        gitops.checkout_existing(work_dir, branch, deadline)
        resumed = True
        ledger.record_step(batch_id, revision, "git:resumed-branch", branch)
    else:
        gitops.checkout_new(work_dir, branch, f"origin/{base_branch}", deadline)

    files, conflict = _prepare_files(checked, work_dir)
    if conflict:
        raise CommunityError(conflict, status="needs_review")

    if files:
        ledger.record_step(batch_id, revision, "git:apply-files", f"{len(files)} file(s)")
        gitops.apply_files(work_dir, files)
    ledger.record_step(batch_id, revision, "git:commit")
    committed = gitops.stage_and_commit(work_dir, [f["path"] for f in files], commit_message, deadline)
    if committed is None:
        head_sha = gitops.current_head(work_dir, deadline)
        if updating:
            ledger.record_step(batch_id, revision, "gate:before-metadata")
            _settings_gate(settings)
            transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
            return {"status": "unchanged", "detail": "no content change; PR metadata refreshed",
                    "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha}
        if resumed:
            # The exact commits are already on the remote branch; only the PR
            # step is outstanding. Skip the push entirely and create the PR.
            pass
        else:
            return {"status": "unchanged",
                    "detail": "content already present on the target branch",
                    "pr_number": (prior or {}).get("number"), "pr_url": (prior or {}).get("html_url"),
                    "head_sha": head_sha}
    else:
        head_sha = committed
        ledger.record_step(batch_id, revision, "gate:before-push")
        _settings_gate(settings)
        ledger.record_step(batch_id, revision, "git:push")
        gitops.push_branch(work_dir, branch, deadline)
        ledger.record_step(batch_id, revision, "git:pushed", head_sha)

    ledger.record_step(batch_id, revision, "gate:before-api")
    _settings_gate(settings)
    if updating:
        ledger.record_step(batch_id, revision, "github:update-pr", f"#{prior['number']}")
        transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
        return {"status": "updated", "detail": f"updated open PR #{prior['number']}",
                "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha}

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
            "pr_number": pr.get("number"), "pr_url": pr.get("html_url"), "head_sha": head_sha}


def _prepare_files(checked, work_dir):
    """Decide per-file content, merging votes and refusing silent overwrites.

    Returns (files, conflict_detail). A file whose remote content moved away
    from the declared base (bot edits, maintainer fixes) is a conflict for
    entry files; feedback files merge votes by vote_id deterministically.
    """
    out = []
    for item in checked["files"]:
        path = item["path"]
        current_sha = gitops.tree_sha256(work_dir, path)
        if current_sha is None:
            out.append(item)
            continue
        if current_sha == item["sha256"]:
            continue  # identical content already present
        if item["base_sha256"] is not None and current_sha == item["base_sha256"]:
            out.append(item)  # expected-base change: our own prior version
            continue
        if path.startswith("feedback/"):
            current = gitops.read_tree_file(work_dir, path)
            existing = validate_feedback(current, path)["votes"] if current else []
            new = validate_feedback(item["content"], path)["votes"]
            merged = render_feedback(merge_votes(existing, new))
            from .common import sha256_text

            if sha256_text(merged) != current_sha:
                out.append({**item, "content": merged, "sha256": sha256_text(merged)})
            continue
        return None, (
            f"{path} changed away from the declared base on the remote "
            "(possible bot or maintainer edit); not overwriting"
        )
    return out, None


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
        except CommunityError:
            continue
    if not found:
        try:
            found = transport.find_pull_requests(repository, head_branch=branch, deadline=deadline)
        except UnknownOutcome:
            raise
        except CommunityError:
            found = []
    own = [pr for pr in found if pr.get("head", {}).get("ref") == branch]
    open_pr = next((pr for pr in own if pr.get("state") == "open"), None)
    if open_pr:
        return open_pr
    merged = [pr for pr in own if pr.get("merged")]
    return merged[-1] if merged else (own[-1] if own else None)


def _resolve_unknown(ledger, row, settings, transport, deadline) -> dict[str, Any]:
    """Bounded READ-ONLY reconciliation of our own branch/PR. Never creates."""
    write_repo = row["repository"]
    status = "unknown"
    detail = "remote outcome remains unconfirmed; stopped without retrying"
    pr_url, head_sha = row.get("pr_url"), row.get("head_sha")
    try:
        prs = []
        if row.get("pr_number"):
            try:
                prs.append(transport.get_pull_request(write_repo, row["pr_number"], deadline))
            except CommunityError:
                pass
        if not prs:
            prs = transport.find_pull_requests(write_repo, head_branch=row["branch"], deadline=deadline)
        own = [pr for pr in prs if pr.get("head", {}).get("ref") == row["branch"]]
        if own:
            pr = own[0]
            pr_url = pr.get("html_url")
            head_sha = pr.get("head", {}).get("sha") or head_sha
            if pr.get("merged"):
                status, detail = "submitted", "reconciled: PR was merged"
            elif pr.get("state") == "open":
                status, detail = "submitted", "reconciled: PR is open"
            else:
                status, detail = "failed", "reconciled: PR closed unmerged"
    except (CommunityError, UnknownOutcome) as exc:
        detail = f"reconciliation lookup failed: {exc}"
    ledger.finish_publication(
        row["batch_id"], row["revision"], status=status, detail=detail,
        pr_url=pr_url, head_sha=head_sha,
    )
    return ledger.receipt(ledger.get_publication(row["batch_id"], row["revision"]))


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
            result = _resolve_unknown(ledger, row, settings, transport, deadline)
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
