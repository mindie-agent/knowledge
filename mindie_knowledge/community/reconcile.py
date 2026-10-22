"""[Role] Read-only publication reconciliation for interrupted Git/GitHub writes.

[Invariants]
- Confirmation requires the saved expected head at the remote PR, or Git proof
  that it is an ancestor of a moved head; branch existence alone is not proof.
- Exhaustion of a lookup budget never becomes proven failure: no evidence stays
  ``unknown`` and only a closed unmerged PR is ``rejected``.
- A succeeded remote effect is preserved even when the following local receipt
  write fails; the receipt is updated separately and reports that failure.

[Timeouts & Locks]
- Every Git/GitHub read is bounded by the caller's ``Deadline``; the ledger is
  opened once per public operation and closed in ``finally``.

[Side-Effect Policy]
- No push, PR creation or PR update is issued here. Only local ledger receipts
  are written; remote reads are bounded, read-only API/Git operations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import gitops
from .common import (
    CommunityError,
    Deadline,
    TransientError,
    UnknownOutcome,
    finite_epoch,
)
from .ledger import Ledger
from .transport import Transport, transport_from_settings


def _sharing_enabled(settings):
    """Resolve the live gate through ``publish`` so its test/adapter seam holds."""
    from . import publish
    return publish.sharing_enabled(settings)


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

class _CompletedEffectReceiptFailure(Exception):
    """A remote effect returned successfully; its following receipt failed."""
    def __init__(self, result, cause):
        self.result, self.cause = result, cause
        super().__init__('completed external effect receipt failed')

def _record_completed(ledger, batch_id, revision, step, detail, result):
    try:
        ledger.record_step(batch_id, revision, step, detail)
    except Exception as exc:
        raise _CompletedEffectReceiptFailure(result, exc) from exc

def _finish_result(checked, ledger, result, *, recording_error=None):
    """Keep a known business result even if its independent receipt fails."""
    result = dict(result)
    try:
        ledger.finish_publication(
            checked['batch_id'], checked['revision'], status=result['status'],
            detail=result.get('detail', ''), pr_number=result.get('pr_number'),
            pr_url=result.get('pr_url'), head_sha=result.get('head_sha'),
        )
    except Exception as exc:
        if recording_error is None:
            recording_error = exc
        else:
            recording_error.add_note('final publication receipt also failed: ' + type(exc).__name__)
    if recording_error is not None:
        from mindie_knowledge.loop.dfx import failure
        failure('knowledge.publish', stage='receipt', category='publication_recording_failed',
                exception=recording_error, reportable=False)
        result['detail'] = result.get('detail', '') + '; local publication receipt failed (' + type(recording_error).__name__ + ')'
        result.update(recording_failed=True, failed_stage='receipt', error_code='publication_recording_failed')
    return _receipt(checked, status=result['status'], detail=result.get('detail', ''),
                    pr_url=result.get('pr_url'), head_sha=result.get('head_sha'),
                    retry_at=result.get('retry_at'),
                    extras={key: value for key, value in result.items()
                            if key not in {'status', 'detail', 'pr_url', 'head_sha', 'retry_at'}})

def _close_ledger(ledger, result):
    import sys
    primary_error = sys.exc_info()[1]
    try:
        ledger.close()
    except Exception as exc:
        if primary_error is not None:
            primary_error.add_note('publication ledger cleanup also failed: ' + type(exc).__name__)
        elif result is not None:
            from mindie_knowledge.loop.dfx import failure
            failure('knowledge.publish', stage='cleanup', category='publication_cleanup_failed',
                    exception=exc, reportable=False)
            result.update(cleanup_failed=True)
            result['detail'] += '; publication ledger cleanup failed (' + type(exc).__name__ + ')'
        else:
            raise

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

    Recorded PR/branch pairs include the suffixed branches created after a
    merge. A prefix match alone never establishes ownership. Without a receipt,
    only the initial deterministic branch is discoverable.
    """
    recorded = {}
    for row in ledger.all_for_batch(batch_id):
        if row.get("pr_number") and row.get("repository") == repository:
            recorded.setdefault(row["pr_number"], set()).add(row["branch"])
    numbers = set(recorded)
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
    own = [pr for pr in found
           if pr.get("head", {}).get("ref") in recorded.get(pr.get("number"), {branch})]
    open_prs = [pr for pr in own if pr.get("state") == "open"]
    if len(open_prs) > 1:
        raise CommunityError("multiple recorded open PRs for one contribution; inspect before appending", status="needs_review")
    if open_prs:
        return open_prs[0]
    return max(own, key=lambda pr: pr["number"]) if own else None

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
    result = dict(ledger.receipt(row), status=status, detail=detail,
                  pr_url=pr_url, head_sha=expected, pr_number=row.get('pr_number'))
    return _attach_retry(_finish_result(row, ledger, result), retry_at)

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
    result = dict(ledger.receipt(row), status=status, detail=detail,
                  pr_url=pr_url, head_sha=expected, pr_number=row.get("pr_number"))
    return _attach_retry(_finish_result(row, ledger, result), retry_at)

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
    result = None
    try:
        if not _sharing_enabled(settings):
            result = {"status": "disabled", "batch_id": batch_id, "revision": None,
                    "pr_url": None, "head_sha": None,
                    "detail": "community sharing is disabled or unconfigured"}
            return result
        transport = transport or transport_from_settings(settings, state_dir)
        deadline = Deadline(settings.get("transaction_seconds"),
                            settings.get("operation_limit"))
        rows = [
            row for row in ledger.all_for_batch(batch_id)
            if row["status"] in ("intent", "unknown", "failed", "rejected")
        ]
        if not rows:
            latest = ledger.latest_for_batch(batch_id)
            if latest is None:
                result = {"status": "unknown", "batch_id": batch_id, "revision": None,
                        "pr_url": None, "head_sha": None,
                        "detail": "no publication record for this batch"}
            else:
                result = ledger.receipt(latest)
            return result
        for row in rows:
            result = _inspect_row(ledger, row, settings, state_dir, transport, deadline)
            if result.get("recording_failed"):
                break
        return result
    finally:
        _close_ledger(ledger, result)

def reconcile_batch(batch_id: str, settings: dict, state_dir: Path, *, transport: Transport | None = None,
                    cancel: Any = None, authority_guard=None) -> dict:
    """Read-only reconciliation of every unresolved intent of one batch id."""
    state_dir = Path(state_dir)
    ledger = Ledger(state_dir, authority_guard=authority_guard)
    result = None
    try:
        if not _sharing_enabled(settings):
            result = {"status": "disabled", "batch_id": batch_id, "revision": None,
                    "pr_url": None, "head_sha": None,
                    "detail": "community sharing is disabled or unconfigured"}
            return result
        transport = transport or transport_from_settings(settings, state_dir)
        deadline = Deadline(settings.get("transaction_seconds"),
                            settings.get("operation_limit"), cancel=cancel)
        unresolved = ledger.unresolved_for_batch(batch_id)
        if not unresolved:
            latest = ledger.latest_for_batch(batch_id)
            if latest is None:
                result = {"status": "unknown", "batch_id": batch_id, "revision": None,
                        "pr_url": None, "head_sha": None, "detail": "no publication record for this batch"}
            else:
                result = ledger.receipt(latest)
            return result
        result = None
        for row in unresolved:
            result = _resolve_unknown(ledger, row, settings, state_dir, transport, deadline)
            if result["status"] == "unknown" or result.get("recording_failed"):
                break
        return result
    finally:
        _close_ledger(ledger, result)

def _remote_url(settings: Mapping[str, Any], write_repo: str) -> str:
    dev = settings.get("dev_remotes") or {}
    if write_repo in dev:
        return dev[write_repo]
    return f"https://github.com/{write_repo}.git"
