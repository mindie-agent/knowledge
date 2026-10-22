"""[Role] Idempotent outbound Git + GitHub PR publication state machine (`submit_batch`).

[Invariants]
- Re-verifies `live_gate(settings)` before every local Git mutation and before
  every outbound network request (`fork`, `push`, `create_pr`, `update_pr`).
- Durable `Ledger` records `intent` and step receipts BEFORE each external write;
  interrupted or timed-out writes stay `unknown` and are resolved strictly by
  the read-only reconciliation in `community.reconcile`, never by blind re-pushes.
- A succeeded remote effect (`submitted` / `updated`) is preserved even if the
  subsequent local receipt write fails (`_CompletedEffectReceiptFailure` /
  `recording_failed=True`). Reconciliation public names are re-exported here.

[Timeouts & Locks]
- Bounded by `Deadline(transaction_seconds, operation_limit, cancel=cancel)` across
  all Git and GitHub transport operations.

[Side-Effect Policy]
- Mutates local staging repo (`state_dir/repo`), user fork branch
  (`mindie-contribute/<domain>/<batch_id>`), and upstream GitHub PRs via `Transport`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import gitops
from .batch import (
    check_path,
    contribution_branch,
    render_commit_message,
    render_pr_body,
    render_pr_title,
    scan_outbound,
    validate_batch,
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
from mindie_knowledge.publication_contract import ContractMismatch, data_format, read_git_contract
from .reconcile import (
    _CompletedEffectReceiptFailure,
    _attach_retry,
    _close_ledger,
    _contribution_head,
    _expected_head,
    _finish_result,
    _own_prior_pr,
    _owned_head,
    _pr_number_absent,
    _proven_open_head,
    _remote_url,
    _resolve_unknown,
    _receipt,
    _record_completed,
    _valid_sha,
    _verdict_for_pr,
    inspect_batch,
    reconcile_batch,
)

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
    authority_guard=None,
) -> dict:
    """Publish one contribution batch through Git + GitHub, idempotently."""
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir, authority_guard=authority_guard)
    result = None
    try:
        result = _submit(batch, settings, state_dir, ledger, cancel=cancel, transport=transport)
        return result
    finally:
        _close_ledger(ledger, result)

def _submit(batch, settings, state_dir, ledger, *, cancel, transport) -> dict[str, Any]:
    if not sharing_enabled(settings):
        return _receipt(batch if isinstance(batch, Mapping) else {}, status="disabled",
                        detail="community sharing is disabled or unconfigured")
    checked = validate_batch(batch)
    deadline = Deadline(
        settings.get("transaction_seconds"),
        settings.get("operation_limit"),
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
            if resolved['status'] == 'unknown' or not checked["explicit_retry"]:
                return resolved
            # Only a proven failed result may proceed through explicit retry.
            # An explicit flag never turns an uncertain effect into no effect.
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

    # The repository/data contract is a read-only prerequisite, not an
    # attempted publication. A deployment mismatch cannot create an unknown
    # write or consume a frozen payload before any remote mutation happens.
    try:
        prepared = _publication_base(checked, settings, state_dir, deadline,
                                     repository=repository, write_repo=write_repo,
                                     remote_url=remote_url)
    except ContractMismatch as exc:
        return _receipt(checked, status="unavailable", detail=str(exc), extras={
            "error_code": exc.code, "failed_stage": "publication-contract",
            "external_write_attempted": False,
        })
    except TimeoutError:
        raise
    except (OSError, TimeoutError) as exc:
        return _receipt(checked, status="unavailable", detail=str(exc), extras={
            "failed_stage": "publication-contract", "external_write_attempted": False,
        })
    except CommunityError as exc:
        return _receipt(checked, status=exc.status, detail=str(exc), retry_at=exc.retry_at,
                        extras={"failed_stage": "publication-contract", "external_write_attempted": False})

    ledger.record_intent(
        batch_id=batch_id, revision=revision, domain=checked["domain"],
        repository=repository, branch=branch,
    )
    progress = {}
    try:
        result = _publish(
            checked, settings, state_dir, ledger, deadline, transport,
            repository=repository, write_repo=write_repo, remote_url=remote_url,
            branch=branch, pr_title=pr_title, pr_body=pr_body, commit_message=commit_message,
            prior=prior, prepared=prepared, progress=progress,
        )
    except _CompletedEffectReceiptFailure as exc:
        return _finish_result(checked, ledger, exc.result, recording_error=exc.cause)
    except UnknownOutcome as exc:
        return _finish_result(checked, ledger, dict(progress, status='unknown', detail=str(exc), retry_at=exc.retry_at))
    except CommunityError as exc:
        # A later gate/read failure cannot erase a push which already
        # completed. Preserve that fact even when creating the PR is blocked.
        pushed = progress.get('head_sha')
        detail = str(exc)
        if pushed:
            detail = 'Git push completed; subsequent publication step did not complete: ' + detail
        return _finish_result(checked, ledger, dict(status=exc.status, detail=detail,
                              retry_at=exc.retry_at, head_sha=pushed,
                              completed_steps=['git-push'] if pushed else [], external_write_completed=bool(pushed)))
    return _finish_result(checked, ledger, {**progress, **result})

def _publication_base(checked, settings, state_dir, deadline, *, repository, write_repo, remote_url):
    """Select and validate the exact upstream base before publication intent."""
    base_branch = settings.get("branch", "main")
    _settings_gate(settings)
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
    contract = read_git_contract(
        work_dir, base_tip, checked["domain"],
        deadline=deadline.limit, env=genv, cancel=deadline.cancel,
    )
    if data_format(contract["contract"]) != checked["data_format"]:
        raise ContractMismatch("contribution data format differs from the selected publication contract")
    return dict(work_dir=work_dir, genv=genv, base_tip=base_tip,
                read_url=read_url, base_branch=base_branch, contract=contract)

def _publish(checked, settings, state_dir, ledger, deadline, transport, *,
             repository, write_repo, remote_url, branch, pr_title, pr_body, commit_message,
             prior, prepared, progress):
    batch_id, revision = checked["batch_id"], checked["revision"]
    work_dir, genv = prepared["work_dir"], prepared["genv"]
    base_tip, read_url = prepared["base_tip"], prepared["read_url"]
    base_branch = prepared["base_branch"]
    ledger.record_step(batch_id, revision, "publication-contract:validated", prepared["contract"]["sha256"])
    if checked["base_commit"] and checked["base_commit"] != base_tip:
        ledger.record_step(batch_id, revision, "git:base-diverged",
                           f"batch base {checked['base_commit'][:12]} != remote {base_tip[:12]}")

    updating = bool(prior and prior.get("state") == "open")
    merged_prior = prior if prior and prior.get("merged") else None

    ledger.record_step(batch_id, revision, "gate:before-worktree")
    _settings_gate(settings)
    resumed = False
    own_resume = False
    resumed_files = {}
    if updating:
        # No checkout, commit, push, or PR write until the API head and the
        # upstream PR ref are the same commit. A retained fork branch is not
        # a substitute, for a fork or for a same-repository PR.
        checkout_ref = _proven_open_head(
            work_dir, prior, branch, settings, read_url, deadline, genv
        )
    elif checked["explicit_retry"] and gitops.remote_tip(remote_url, f"refs/heads/{branch}", deadline, env=genv):
        # Explicit retry with the branch already pushed: resume from the remote
        # tip and its recorded steps instead of re-pushing earlier commits.
        checkout_ref = f"origin/{branch}"
        resumed = True
    else:
        # A prior attempt of THIS lineage may have pushed the branch before
        # failing (e.g. auth refused the PR creation). Building on top of our
        # own proven pushed tip keeps the write fast-forward; a tip that is
        # not ours is a real divergence and parks as needs_review on push.
        tip = gitops.remote_tip(remote_url, f"refs/heads/{branch}", deadline, env=genv)
        prior_push = next((row for row in ledger.all_for_batch(batch_id)
                           if tip is not None and _expected_head(ledger, row) == tip), None)
        if prior_push is not None:
            resumed_files = {item["path"]: item.get("sha256")
                             for item in Ledger.parse_actual_files(prior_push) or []}
            checkout_ref = f"origin/{branch}"
            own_resume = True
        else:
            checkout_ref = base_tip

    # Updating an open contribution must not carry an independently changed
    # workflow/data policy from its head. Inspect before checkout or a write.
    selected_commit = gitops._git(["rev-parse", checkout_ref], deadline, cwd=work_dir, env=genv).strip()
    try:
        read_git_contract(work_dir, selected_commit, checked["domain"],
                          deadline=deadline.limit, env=genv, cancel=deadline.cancel)
    except ContractMismatch as exc:
        raise CommunityError(f"contribution head contract differs from its upstream base: {exc}",
                             status="needs_review") from exc
    except TimeoutError:
        raise
    except (OSError, TimeoutError) as exc:
        raise TransientError(f"cannot verify the contribution head contract: {exc}") from exc

    if checked["data_format"] == "graded":
        from ..graded.publisher import validate_projection
        validate_projection(checked, work_dir, selected_commit, deadline, genv)

    # Inspect Git objects before asking the host to materialize them. A path
    # or mode conflict must stay needs_review even when checkout cannot create
    # that path (for example a newline filename or symlink on Windows).
    tree_modes = (gitops.ls_tree(work_dir, checkout_ref, deadline, env=genv,
                                  task_ids=set(checked["task_revisions"]))
                  if checked["task_revisions"] else {})
    conflict = _task_tree_conflict(checked, tree_modes, own_branch_resume=own_resume,
                                   resumed_files=resumed_files)
    if conflict:
        raise CommunityError(conflict, status="needs_review")
    if resumed or own_resume:
        gitops.checkout_existing(work_dir, branch, deadline, env=genv)
        ledger.record_step(batch_id, revision,
                           "git:resumed-branch" if resumed else "git:resume-own-branch",
                           branch if resumed else tip)
    else:
        gitops.checkout_new(work_dir, branch, checkout_ref, deadline, env=genv)
    files, conflict = _prepare_files(checked, work_dir, own_branch_resume=own_resume,
                                     resumed_files=resumed_files, tree_modes=tree_modes)
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
            ledger.assert_authority()
            transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
            return {"status": "unchanged", "detail": "no content change; PR metadata refreshed",
                    "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha,
                    "files": actual_files, 'completed_steps': ['pr-update'], 'external_write_completed': True}
        if resumed or own_resume:
            # The exact commits are already on the remote branch; only the PR
            # step is outstanding. Skip the push entirely and create the PR.
            progress.update(head_sha=head_sha, completed_steps=['git-push'], external_write_completed=True)
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
        ledger.assert_authority()
        gitops.push_branch(work_dir, branch, deadline, env=genv, cancel=deadline.cancel)
        progress.update(head_sha=head_sha, completed_steps=['git-push'], external_write_completed=True)
        _record_completed(ledger, batch_id, revision, 'git:pushed', head_sha,
                          dict(status='failed', detail='Git push completed; PR publication did not start',
                               head_sha=head_sha, files=actual_files, completed_steps=['git-push'],
                               external_write_completed=True))

    # Any local receipt between the completed push and the next request must
    # preserve the same known result, including intent/gate records.
    pushed_result = dict(status='failed', detail='Git push completed; subsequent PR request did not start',
                         head_sha=head_sha, files=actual_files, completed_steps=['git-push'],
                         external_write_completed=True)
    _record_completed(ledger, batch_id, revision, 'gate:before-api', '', pushed_result)
    _settings_gate(settings)
    if updating:
        _record_completed(ledger, batch_id, revision, 'github:update-pr', f"#{prior['number']}", pushed_result)
        ledger.assert_authority()
        transport.update_pull_request(repository, prior["number"], title=pr_title, body=pr_body, deadline=deadline)
        progress.update(completed_steps=[*progress.get('completed_steps', []), 'pr-update'], external_write_completed=True)
        return {"status": "updated", "detail": f"updated open PR #{prior['number']}",
                "pr_number": prior["number"], "pr_url": prior.get("html_url"), "head_sha": head_sha,
                "files": actual_files}

    if merged_prior:
        pr_body = pr_body + f"\nFollow-up to merged PR #{merged_prior['number']}.\n"
    _record_completed(ledger, batch_id, revision, 'github:create-pr', '', pushed_result)
    head_ref = branch if write_repo == repository else f"{write_repo.split('/')[0]}:{branch}"
    ledger.assert_authority()
    pr = transport.create_pull_request(
        write_repo if write_repo == repository else repository,
        title=pr_title, body=pr_body, head=head_ref, base=base_branch, deadline=deadline,
    )
    result = {"status": "submitted", "detail": f"opened PR #{pr.get('number')}",
            "pr_number": pr.get("number"), "pr_url": pr.get("html_url"), "head_sha": head_sha,
            "files": actual_files, 'completed_steps': ['git-push', 'pr-create'], 'external_write_completed': True}
    _record_completed(ledger, batch_id, revision, 'github:pr-created', f"#{pr.get('number')}", result)
    return result

def _actual_entry_files(checked, work_dir):
    """Actual per-file committed identities, always read from worktree bytes.

    After apply/commit the worktree IS what the branch holds, for written,
    identical-skipped and merge-no-change paths alike — never the candidate
    payload. Hashes bind each exact file and the complete task revision.
    """
    actual = []
    for item in checked["files"]:
        path = item["path"]
        if path.startswith("feedback/"):
            continue
        if item.get("delete") is True:
            if gitops.read_tree_file(work_dir, path) is not None:
                raise CommunityError(f"deleted task block is still present: {path}")
            actual.append({"path": path, "sha256": None, "revision": None, "delete": True})
            continue
        text = gitops.read_tree_file(work_dir, path)
        if text is None or sha256_text(text) != item["sha256"]:
            raise CommunityError(f"committed task file differs from its validated package: {path}")
        if checked["data_format"] == "graded":
            from ..graded.contribution import DERIVED_PATHS, parse_unit
            revision = sha256_text(text) if path in DERIVED_PATHS else parse_unit(text, path).content_sha256
        else:
            task_id = path.split("/")[1]
            revision = checked["task_revisions"][task_id]
        actual.append({
            "path": path,
            "sha256": sha256_text(text),
            "revision": revision,
        })
    return actual

def _task_tree_conflict(checked, tree_modes, *, own_branch_resume=False, resumed_files=None):
    """Reject unsupported package shape from object metadata before checkout."""
    expected = {item["path"] for item in checked["files"]}
    for path, mode in tree_modes.items():
        if mode != "100644":
            return f"{path!r} is not a regular task material file; not overwriting"
        try:
            check_path(path)
        except CommunityError:
            return f"{path!r} is an unsupported task material path; not checking out"
        if path not in expected:
            parts = path.split("/", 2)
            if (own_branch_resume and path.startswith(f"tasks/{parts[1]}/blocks/")
                    and (resumed_files or {}).get(path) is not None):
                # Exact byte identity is still checked after checkout before
                # deleting a file from our own unconfirmed earlier push.
                continue
            return f"{path!r} is outside the new package and has no proven deletion base; not deleting"
    return None

def _prepare_files(checked, work_dir, *, own_branch_resume=False, resumed_files=None, tree_modes=None):
    """Write complete packages only over exact declared bases.

    Remote corrections stay authoritative. A changed or withdrawn task file
    makes the candidate a visible conflict. Feedback votes can merge by their
    durable identity. An own unconfirmed branch can resume only after the
    caller proves that lineage from its ledger and no PR exists.
    """
    out = []
    expected = {item["path"] for item in checked["files"]}
    for path, mode in (tree_modes or {}).items():
        parts = path.split("/", 2)
        if len(parts) < 3 or parts[0] != "tasks" or parts[1] not in checked["task_revisions"]:
            continue
        if mode != "100644":
            return None, f"{path} is not a regular task material file; not overwriting"
        if path in expected:
            continue
        current_sha = gitops.tree_sha256(work_dir, path)
        if (own_branch_resume and path.startswith(f"tasks/{parts[1]}/blocks/")
                and current_sha is not None and (resumed_files or {}).get(path) == current_sha):
            out.append(dict(path=path, content=None, sha256=None,
                            base_sha256=current_sha, delete=True))
        else:
            return None, f"{path} is outside the new package and has no proven deletion base; not deleting"

    for item in checked["files"]:
        path = item["path"]
        current_sha = gitops.tree_sha256(work_dir, path)
        if item.get("delete") is True:
            if current_sha is None:
                continue
            if current_sha != item["base_sha256"]:
                return None, f"{path} changed away from the declared deletion base; not deleting"
            out.append(item)
            continue
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
        ):
            # Each task file is bound to the complete package and exact base.
            out.append(item)
            continue
        if path.startswith("feedback/"):
            current = gitops.read_tree_file(work_dir, path)
            from ..graded.contribution import feedback_api
            api = feedback_api(item["content"])
            existing = api.validate_feedback(current, path)["votes"] if current else []
            new = api.validate_feedback(item["content"], path)["votes"]
            merged = api.render_feedback(api.merge_votes(existing, new))
            if sha256_text(merged) != current_sha:
                out.append({**item, "content": merged, "sha256": sha256_text(merged)})
            continue
        return None, (
            f"{path} changed away from the declared base on the remote "
            "(possible bot or maintainer edit); not overwriting"
        )
    return out, None
