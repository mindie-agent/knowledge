"""[Role] Bind graded-unit feedback to an actually observed content revision.

[Invariants]
- A feedback reference is the exact canonical pinned form
  ``mindie://DOMAIN/units/ID@BARE64HASH``; short, unpinned, fake or foreign
  references are never resolved.
- Observation is explicit: query/explain persist the compact identity of a
  returned unit in the existing ``state`` row and ``known_revisions`` table.
  Importing a feed alone never marks a revision observed.
- Votes live in the existing ``votes``/``grants`` tables. The observed content
  hash is the vote revision, so an older observation remains locally votable
  after the current unit changes, while public summaries count only the exact
  current content digest.
- Public feedback contains exactly the V2 fields and is keyed by opaque root +
  stable unit ID + content digest. A digest/revision disagreement for the same
  unit is rejected, never silently reinterpreted or counted twice.

[Timeouts & Locks]
- Reuses ``Store._write_txn`` and ``Store.lock``. No new database, state
  machine, sleep, timer, model call or network call.

[Side-Effect Policy]
- Local SQLite state/votes/grants only. Private reasons stay local; a
  publishable reason is refused by the existing deterministic scanner.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import re
import time
from typing import Any

from .units import UNIT_ID, GradedUnit, canonical
from ..materials.references import ReadReferenceError

SCHEMA_FEEDBACK = "mindie-graded-feedback/1"
RATINGS = ("up", "down")
PUBLIC_VOTE_FIELDS = (
    "vote_id",
    "root_id",
    "unit_id",
    "revision",
    "content_digest",
    "rating",
    "reason",
)
MAX_FEEDBACK_VOTES = 500
MAX_IDENTITY_CHARS = 256
OBSERVED_PREFIX = "graded:observed:"

_BARE_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_CONTENT_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_DOMAIN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_PINNED_REF = re.compile(
    r"mindie://(?P<domain>[a-z][a-z0-9-]{0,63})/units/"
    r"(?P<unit_id>[^/@\s]+)@(?P<hash>[a-f0-9]{64})\Z"
)
_OBSERVED_FIELDS = frozenset({"unit_id", "revision", "content_digest"})


class FeedbackError(ValueError):
    """A graded feedback request or public vote violates the V2 contract."""

    code = "graded_feedback_invalid"


class FeedbackReferenceError(FeedbackError, ReadReferenceError):
    """A feedback reference is not the exact pinned graded reference."""

    code = "reference_invalid"

    def __init__(self, message: str, *, read_ref: str | None = None):
        ValueError.__init__(self, message)
        self.read_ref = read_ref


def _reason_limit() -> int:
    # Imported lazily so Store can import this module without a cycle.
    from ..loop.store import MAX_VOTE_REASON

    return MAX_VOTE_REASON


def _mapping(value: Any, label: str) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    keys = getattr(value, "keys", None)
    if callable(keys):
        try:
            return {key: value[key] for key in keys()}
        except (IndexError, KeyError, TypeError):
            pass
    raise FeedbackError(f"{label} must be an object")


def _identity_text(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_IDENTITY_CHARS
    ):
        raise FeedbackError(f"{name} must be a nonempty opaque identity")
    return value


def _unit_id(value: Any) -> str:
    if not isinstance(value, str) or UNIT_ID.fullmatch(value) is None:
        raise FeedbackError("unit_id must be an exact stable graded unit identifier")
    return value


def _content_digest(value: Any) -> str:
    if not isinstance(value, str) or _CONTENT_DIGEST.fullmatch(value) is None:
        raise FeedbackError("content_digest must be 'sha256:' plus a lowercase SHA256")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise FeedbackError("revision must be a positive integer")
    return value


def _rating(value: Any) -> str:
    if value not in RATINGS:
        raise FeedbackError(f"rating must be one of {RATINGS}")
    return value


def _reason(value: Any) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FeedbackError("reason must be text")
    limit = _reason_limit()
    value = value.strip()
    if len(value) > limit:
        raise FeedbackError(f"reason must be at most {limit} characters")
    return value


def _scan_publishable_reason(reason: str) -> None:
    from ..redact import scan_text

    findings = scan_text(reason)
    if findings:
        rules = ", ".join(sorted({finding.rule for finding in findings}))
        raise FeedbackError(f"publishable vote reason fails the privacy scan: {rules}")


def _normalize_public_vote(raw: Any, label: str = "vote") -> dict:
    vote = _mapping(raw, label)
    unknown = set(vote) - set(PUBLIC_VOTE_FIELDS)
    if unknown:
        raise FeedbackError(f"{label} has unknown fields: {sorted(unknown)}")
    required = {"vote_id", "root_id", "unit_id", "revision", "content_digest", "rating"}
    missing = required - set(vote)
    if missing:
        raise FeedbackError(f"{label} is missing fields: {sorted(missing)}")
    return {
        "vote_id": _identity_text(vote.get("vote_id"), f"{label}.vote_id"),
        "root_id": _identity_text(vote.get("root_id"), f"{label}.root_id"),
        "unit_id": _unit_id(vote.get("unit_id")),
        "revision": _revision(vote.get("revision")),
        "content_digest": _content_digest(vote.get("content_digest")),
        "rating": _rating(vote.get("rating")),
        "reason": _reason(vote.get("reason", "")),
    }


def _vote_key(vote: Mapping[str, Any]) -> tuple[str, str, str]:
    return (vote["root_id"], vote["unit_id"], vote["content_digest"])


def _check_consistency(votes: Iterable[Mapping[str, Any]]) -> None:
    """Same unit content must keep one integer revision, and vice versa."""
    by_digest: dict[tuple[str, str], int] = {}
    by_revision: dict[tuple[str, int], str] = {}
    for vote in votes:
        digest_key = (vote["unit_id"], vote["content_digest"])
        prior_revision = by_digest.get(digest_key)
        if prior_revision is not None and prior_revision != vote["revision"]:
            raise FeedbackError(
                "votes for the same unit and content digest disagree on revision"
            )
        by_digest[digest_key] = vote["revision"]

        revision_key = (vote["unit_id"], vote["revision"])
        prior_digest = by_revision.get(revision_key)
        if prior_digest is not None and prior_digest != vote["content_digest"]:
            raise FeedbackError(
                "votes for the same unit and revision disagree on content digest"
            )
        by_revision[revision_key] = vote["content_digest"]


def validate_bindings(votes, units):
    """Validate the complete snapshot's votes against current unit identities.

    Historical votes may name retired content or absent units. They cannot
    reuse a current content hash with another revision, or a current revision
    with another hash. Validate across files before replacing a readable feed.
    """
    votes = list(votes)
    _check_consistency(votes)
    current = units if isinstance(units, Mapping) else {unit.id: unit for unit in units}
    for vote in votes:
        unit = current.get(vote["unit_id"])
        if unit is None:
            continue
        same_digest = vote["content_digest"] == unit.metadata["content_digest"]
        same_revision = vote["revision"] == unit.revision
        if same_digest != same_revision:
            raise FeedbackError("feedback revision and digest contradict the current unit")


def validate_observed_units(store, units):
    """Refuse contradictory imported identities before switching the catalog."""
    for unit in units:
        row = store.db.execute("SELECT value FROM state WHERE key=?",
                               (_observed_key(unit.id, unit.content_sha256),)).fetchone()
        if row is not None:
            observed = _decode_observed(row[0], unit_id=unit.id, bare=unit.content_sha256)
            if observed["revision"] != unit.revision:
                raise FeedbackError("incoming unit contradicts an already observed stable unit revision")


def validate_feedback(text: str, path: str) -> dict:
    """Validate one public V2 feedback document without rewriting its votes."""
    if not isinstance(text, str):
        raise FeedbackError(f"{path}: feedback must be UTF-8 text")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise FeedbackError(f"{path}: not valid JSON") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_FEEDBACK:
        raise FeedbackError(f"{path} must declare schema {SCHEMA_FEEDBACK}")
    unknown = set(data) - {"schema", "votes"}
    if unknown:
        raise FeedbackError(f"{path}: unknown feedback fields: {sorted(unknown)}")
    votes = data.get("votes")
    if not isinstance(votes, list) or len(votes) > MAX_FEEDBACK_VOTES:
        raise FeedbackError(f"{path}: votes must be a list of at most {MAX_FEEDBACK_VOTES}")

    checked = []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(votes):
        vote = _normalize_public_vote(raw, f"{path}: votes[{index}]")
        if vote["vote_id"] in seen_ids:
            raise FeedbackError(f"{path}: duplicate vote_id {vote['vote_id']!r}")
        key = _vote_key(vote)
        if key in seen_keys:
            raise FeedbackError(f"{path}: duplicate vote for the same root/unit/content")
        seen_ids.add(vote["vote_id"])
        seen_keys.add(key)
        checked.append(vote)
    _check_consistency(checked)
    return {"schema": SCHEMA_FEEDBACK, "votes": checked}


def merge_votes(existing: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Merge public votes by opaque root + stable unit ID + content digest.

    A newer rating/reason replaces the same key, so duplicates never multiply
    counts. A different content digest is a distinct observation and is kept,
    while a digest/revision disagreement for one unit is rejected.
    """
    combined: list[dict] = []
    for source in (existing, new):
        if source is None:
            continue
        if isinstance(source, Mapping):
            source = [source]
        for raw in source:
            combined.append(_normalize_public_vote(raw))
    _check_consistency(combined)
    merged: dict[tuple[str, str, str], dict] = {}
    for vote in combined:
        merged[_vote_key(vote)] = vote
    return [merged[key] for key in sorted(merged)]


def render_feedback(votes: Iterable[Mapping[str, Any]]) -> str:
    """Render a deterministic public V2 feedback document."""
    ordered = merge_votes((), votes)
    return json.dumps(
        {"schema": SCHEMA_FEEDBACK, "votes": ordered},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def parse_feedback_ref(ref: Any, domain: str | None = None) -> dict:
    """Require one canonical pinned graded observation reference."""
    if not isinstance(domain, str) or _DOMAIN.fullmatch(domain) is None:
        raise FeedbackReferenceError("feedback domain must be a canonical domain name")
    if not isinstance(ref, str):
        raise FeedbackReferenceError("feedback requires the exact pinned graded reference")
    match = _PINNED_REF.fullmatch(ref)
    if match is None:
        raise FeedbackReferenceError(
            "feedback requires mindie://DOMAIN/units/ID@BARE64HASH"
        )
    if match.group("domain") != domain:
        raise FeedbackReferenceError("feedback reference is outside the selected domain")
    unit_id = match.group("unit_id")
    if UNIT_ID.fullmatch(unit_id) is None:
        raise FeedbackReferenceError("feedback requires an exact stable graded unit identifier")
    bare = match.group("hash")
    return {
        "kind": "feedback",
        "domain": domain,
        "unit_id": unit_id,
        "revision": bare,
        "content_sha256": bare,
        "content_digest": "sha256:" + bare,
    }


def _unit_fields(unit: GradedUnit) -> tuple[str, int, str, str]:
    """Return stable id, integer revision, bare content hash and public digest."""
    try:
        unit_id = unit.id
        revision = unit.revision
        bare = unit.content_sha256
        digest = unit.metadata["content_digest"]
    except (AttributeError, KeyError, TypeError) as exc:
        raise FeedbackError("observed value is not a valid graded unit") from exc
    unit_id = _unit_id(unit_id)
    revision = _revision(revision)
    if not isinstance(bare, str) or _BARE_SHA256.fullmatch(bare) is None:
        raise FeedbackError("observed unit has no exact content hash")
    digest = _content_digest(digest)
    if digest != "sha256:" + bare:
        raise FeedbackError("observed unit content_digest disagrees with its content hash")
    return unit_id, revision, bare, digest


def _observed_key(unit_id: str, bare: str) -> str:
    return OBSERVED_PREFIX + unit_id + ":" + bare


def _decode_observed(raw: Any, *, unit_id: str | None = None, bare: str | None = None) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise FeedbackError("stored graded observation is unreadable") from None
    if not isinstance(value, dict) or set(value) != _OBSERVED_FIELDS:
        raise FeedbackError("stored graded observation is not a compact identity")
    observed = {
        "unit_id": _unit_id(value["unit_id"]),
        "revision": _revision(value["revision"]),
        "content_digest": _content_digest(value["content_digest"]),
    }
    if unit_id is not None and observed["unit_id"] != unit_id:
        raise FeedbackError("stored graded observation names another unit")
    if bare is not None and observed["content_digest"] != "sha256:" + bare:
        raise FeedbackError("stored graded observation digest disagrees with its key")
    return observed


def _read_observed(store, unit_id: str, bare: str) -> dict:
    """Read one explicit observation. Caller holds Store.lock or _write_txn."""
    unit_id = _unit_id(unit_id)
    if not isinstance(bare, str) or _BARE_SHA256.fullmatch(bare) is None:
        raise FeedbackError("observed revision must be the bare content hash")
    row = store.db.execute(
        "SELECT value FROM state WHERE key=?", (_observed_key(unit_id, bare),)
    ).fetchone()
    if row is None:
        raise FeedbackError(
            "feedback requires a graded revision actually returned to the caller"
        )
    observed = _decode_observed(row[0], unit_id=unit_id, bare=bare)
    known = store.db.execute(
        "SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?",
        (unit_id, bare),
    ).fetchone()
    if known is None:
        raise FeedbackError("observed graded revision is missing from known_revisions")
    return observed


def observe(store, unit: GradedUnit) -> dict:
    """Persist one returned unit's compact identity in the existing store.

    The body is never copied. This is deliberately explicit: a feed import
    does not call this function, so imported material stays unobserved until a
    caller actually receives that revision.
    """
    unit_id, revision, bare, digest = _unit_fields(unit)
    identity = {
        "unit_id": unit_id,
        "revision": revision,
        "content_digest": digest,
    }
    key = _observed_key(unit_id, bare)
    # Repeated query/explain calls are the hot path: an existing observation
    # plus its known revision is already complete and needs no write transaction.
    with store.lock:
        row = store.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is not None:
            existing = _decode_observed(row[0], unit_id=unit_id, bare=bare)
            if existing != identity:
                raise FeedbackError(
                    "graded observation identity changed for the same content hash"
                )
            known = store.db.execute(
                "SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?",
                (unit_id, bare),
            ).fetchone()
            if known is not None:
                return identity
    with store._write_txn():
        row = store.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is not None:
            existing = _decode_observed(row[0], unit_id=unit_id, bare=bare)
            if existing != identity:
                raise FeedbackError(
                    "graded observation identity changed for the same content hash"
                )
        else:
            store.db.execute(
                "INSERT INTO state VALUES(?,?)", (key, canonical(identity))
            )
        store.db.execute(
            "INSERT OR IGNORE INTO known_revisions VALUES(?,?)", (unit_id, bare)
        )
    return identity


def _vote_id(root: str, unit_id: str, bare: str) -> str:
    return hashlib.sha256(canonical(["vote", root, unit_id, bare]).encode("utf-8")).hexdigest()


def record_vote(store, *, root_hash: str, ref: str, rating: str, reason: str,
                publishable: bool, generation: str | None = None) -> dict:
    """Record one local vote bound to the exact observed graded revision.

    The observed content hash is stored as ``votes.revision`` under the stable
    unit ID, so older observed feedback remains distinct. A publishable reason
    must clear the existing deterministic scan; a private reason is stored
    locally and is never scanned or exposed in the public response.
    """
    rating = _rating(rating)
    if not isinstance(reason, str):
        raise FeedbackError("reason must be text")
    reason = _reason(reason)
    if type(publishable) is not bool:
        raise FeedbackError("publishable must be a boolean")
    if generation is not None and publishable and (
        not isinstance(generation, str) or not generation.strip()
    ):
        raise FeedbackError("generation must be a nonempty sharing generation")
    root = _identity_text(root_hash, "root_hash")
    parsed = parse_feedback_ref(ref, store.domain)
    unit_id = parsed["unit_id"]
    bare = parsed["content_sha256"]
    digest = parsed["content_digest"]
    if publishable:
        _scan_publishable_reason(reason)

    with store._write_txn():
        observed = _read_observed(store, unit_id, bare)
        if observed["content_digest"] != digest:
            raise FeedbackError(
                "feedback reference digest disagrees with the observed identity"
            )
        opaque = store.opaque_for(root)
        store.db.execute(
            "INSERT OR REPLACE INTO votes VALUES(?,?,?,?,?,?,NULL,?)",
            (
                opaque,
                unit_id,
                bare,
                rating,
                reason,
                1 if publishable else 0,
                time.time(),
            ),
        )
        if publishable and generation:
            store.db.execute(
                "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                (
                    "vote",
                    store.vote_identity(opaque, unit_id),
                    bare,
                    generation,
                    time.time(),
                ),
            )
        vote = {
            "vote_id": _vote_id(opaque, unit_id, bare),
            "root_id": opaque,
            "unit_id": unit_id,
            "revision": observed["revision"],
            "content_digest": observed["content_digest"],
            "rating": rating,
            "publishable": publishable,
        }
    return vote


def _outbound_identity(row: Mapping[str, Any], label: str) -> tuple[str, str, str, int | None]:
    """Return root, unit id, bare hash and an optional already-public revision."""
    root = _identity_text(
        row.get("root_opaque") or row.get("root_id"), f"{label}.root_id"
    )
    if "entry_id" in row:
        unit_id = _unit_id(row.get("entry_id"))
        bare = row.get("revision")
        if not isinstance(bare, str) or _BARE_SHA256.fullmatch(bare) is None:
            raise FeedbackError(
                "internal graded vote revision must be the observed content hash"
            )
        return root, unit_id, bare, None
    if "unit_id" in row and "content_digest" in row:
        unit_id = _unit_id(row.get("unit_id"))
        digest = _content_digest(row.get("content_digest"))
        return root, unit_id, digest.split(":", 1)[1], _revision(row.get("revision"))
    raise FeedbackError(
        "graded vote requires entry_id/revision or unit_id/content_digest"
    )


def outbound_votes(store, votes: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Convert supplied internal graded vote rows into public V2 votes.

    The public integer revision and digest come from the observation that was
    persisted when the unit was returned, never from the current catalogue or
    from vote totals. Membership and publish eligibility stay with the caller,
    but an explicitly private row never carries its reason into public output.
    """
    if votes is None:
        return []
    if isinstance(votes, Mapping):
        votes = [votes]
    public = []
    with store.lock:
        for index, raw in enumerate(votes):
            row = _mapping(raw, f"internal vote[{index}]")
            root, unit_id, bare, expected_revision = _outbound_identity(
                row, f"internal vote[{index}]"
            )
            observed = _read_observed(store, unit_id, bare)
            if expected_revision is not None and observed["revision"] != expected_revision:
                raise FeedbackError(
                    "supplied vote revision disagrees with the observed revision"
                )
            reason = row.get("reason", "")
            if "publishable" in row and not row.get("publishable"):
                reason = ""
            public.append(
                {
                    "vote_id": row.get("vote_id") or _vote_id(root, unit_id, bare),
                    "root_id": root,
                    "unit_id": unit_id,
                    "revision": observed["revision"],
                    "content_digest": observed["content_digest"],
                    "rating": row.get("rating"),
                    "reason": reason,
                }
            )
    return merge_votes((), public)


def _countable_vote(raw: Any, label: str) -> dict:
    vote = _mapping(raw, label)
    return {
        "root_id": _identity_text(vote.get("root_id"), f"{label}.root_id"),
        "unit_id": _unit_id(vote.get("unit_id")),
        "revision": _revision(vote.get("revision")),
        "content_digest": _content_digest(vote.get("content_digest")),
        "rating": _rating(vote.get("rating")),
    }


def summarize_votes(votes: Iterable[Mapping[str, Any]], unit: GradedUnit) -> dict:
    """Count up/down only for this unit's exact current content digest.

    One vote per opaque root is counted. Older content digests stay local but
    are never treated as current feedback; totals are descriptive counts, not
    truth, confidence or an applicability decision.
    """
    unit_id, revision, _bare, digest = _unit_fields(unit)
    if votes is None:
        votes = ()
    if isinstance(votes, Mapping):
        votes = [votes]
    by_root: dict[str, dict] = {}
    for index, raw in enumerate(votes):
        vote = _countable_vote(raw, f"votes[{index}]")
        if vote["unit_id"] != unit_id or vote["content_digest"] != digest:
            continue
        if vote["revision"] != revision:
            raise FeedbackError(
                "current-content vote disagrees with the observed unit revision"
            )
        by_root[vote["root_id"]] = vote
    up = sum(1 for vote in by_root.values() if vote["rating"] == "up")
    down = sum(1 for vote in by_root.values() if vote["rating"] == "down")
    return {
        "unit_id": unit_id,
        "revision": revision,
        "content_digest": digest,
        "up": up,
        "down": down,
        "total": up + down,
    }


__all__ = [
    "FeedbackError",
    "FeedbackReferenceError",
    "SCHEMA_FEEDBACK",
    "merge_votes",
    "observe",
    "outbound_votes",
    "parse_feedback_ref",
    "record_vote",
    "render_feedback",
    "summarize_votes",
    "validate_feedback",
]
