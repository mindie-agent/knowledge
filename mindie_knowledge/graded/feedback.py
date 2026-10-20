"""[Role] Bind graded-unit feedback to an actually observed id/revision.

[Invariants]
- A feedback reference is the canonical form mindie://DOMAIN/units/ID@INTEGER.
  Short, unpinned, fake or foreign references are never resolved.
- Observation is explicit: query/explain persist {unit_id, revision} in the
  existing state row and known_revisions table. A feed import never observes.
- Votes live in the existing votes/grants tables keyed by stable unit ID and
  integer revision, so an older observation stays distinct and locally votable.
- Usage events are append-only for an observed revision and deduplicate by
  (root opaque, unit_id, revision, event_type, run_id). Evaluated outcomes are
  supported/refuted/inconclusive; inconclusive never increases either side.
- Public feedback uses the existing id/revision identity. No caller computes or
  passes content, source, contract or closure digests.

[Timeouts & Locks] Reuses Store._write_txn and Store.lock; no new timers.
[Side-Effect Policy] Local SQLite state/votes/grants only; private reasons stay
local and publishable reasons use the existing deterministic scanner.
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
EVENT_TYPES = ("discovered", "loaded", "applied", "evaluated")
EVAL_OUTCOMES = ("supported", "refuted", "inconclusive")
EVIDENCE_SOURCES = ("agent_declared", "run_manifest_verified", "ablation_verified")
RATINGS = ("up", "down")
PUBLIC_VOTE_FIELDS = ("vote_id", "root_id", "unit_id", "revision", "rating", "reason")
MAX_FEEDBACK_VOTES = 500
MAX_IDENTITY_CHARS = 256
OBSERVED_PREFIX = "graded:observed:"
EVENT_PREFIX = "graded:event:"
_EVENT_FIELDS = frozenset({"event_id", "root_id", "unit_id", "revision", "event_type",
                           "run_id", "outcome", "step_id", "reason", "evidence_source"})
_EVENT_LEGACY_FIELDS = _EVENT_FIELDS - {"evidence_source"}

_DOMAIN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_REF = re.compile(
    r"mindie://(?P<domain>[a-z][a-z0-9-]{0,63})/units/"
    r"(?P<unit_id>[^/@\s]+)@(?P<revision>[1-9][0-9]*)\Z"
)
_OBSERVED_FIELDS = frozenset({"unit_id", "revision"})
_OBSERVED_DELIVERIES = frozenset({"query_deliveries", "explain_deliveries"})


class FeedbackError(ValueError):
    """A graded feedback request or public vote violates its contract."""

    code = "graded_feedback_invalid"


class FeedbackReferenceError(FeedbackError, ReadReferenceError):
    """A feedback reference is not the canonical graded reference."""

    code = "reference_invalid"

    def __init__(self, message: str, *, read_ref: str | None = None):
        ValueError.__init__(self, message)
        self.read_ref = read_ref


def _reason_limit() -> int:
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
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_IDENTITY_CHARS):
        raise FeedbackError(f"{name} must be a nonempty opaque identity")
    return value


def _unit_id(value: Any) -> str:
    if not isinstance(value, str) or UNIT_ID.fullmatch(value) is None:
        raise FeedbackError("unit_id must be an exact stable graded unit identifier")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise FeedbackError("revision must be a positive integer")
    return value


def _stored_revision(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return _revision(value)
    if isinstance(value, str) and value.isdigit():
        return _revision(int(value))
    raise FeedbackError("stored graded revision must be a positive integer")


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
    required = {"vote_id", "root_id", "unit_id", "revision", "rating"}
    missing = required - set(vote)
    if missing:
        raise FeedbackError(f"{label} is missing fields: {sorted(missing)}")
    return {
        "vote_id": _identity_text(vote.get("vote_id"), f"{label}.vote_id"),
        "root_id": _identity_text(vote.get("root_id"), f"{label}.root_id"),
        "unit_id": _unit_id(vote.get("unit_id")),
        "revision": _revision(vote.get("revision")),
        "rating": _rating(vote.get("rating")),
        "reason": _reason(vote.get("reason", "")),
    }


def _vote_key(vote: Mapping[str, Any]) -> tuple[str, str, int]:
    return (vote["root_id"], vote["unit_id"], vote["revision"])


def validate_bindings(votes, units):
    """Validate public votes against current stable IDs/revisions."""
    current = units if isinstance(units, Mapping) else {unit.id: unit for unit in units}
    for raw in votes or ():
        vote = _normalize_public_vote(raw)
        unit = current.get(vote["unit_id"])
        if unit is not None and vote["revision"] < 1:
            raise FeedbackError("feedback revision contradicts the current unit")


def validate_observed_units(store, units):
    """Observed revisions are explicit; importing never invents an observation."""
    for unit in units:
        row = store.db.execute(
            "SELECT value FROM state WHERE key=?", (_observed_key(unit.id, unit.revision),)
        ).fetchone()
        if row is not None:
            _decode_observed(row[0], unit_id=unit.id, revision=unit.revision)


def validate_feedback(text: str, path: str) -> dict:
    if not isinstance(text, str):
        raise FeedbackError(f"{path}: feedback must be UTF-8 text")
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise FeedbackError(f"{path}: not valid JSON") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_FEEDBACK:
        raise FeedbackError(f"{path} must declare schema {SCHEMA_FEEDBACK}")
    if set(data) - {"schema", "votes"}:
        raise FeedbackError(f"{path}: unknown feedback fields")
    votes = data.get("votes")
    if not isinstance(votes, list) or len(votes) > MAX_FEEDBACK_VOTES:
        raise FeedbackError(f"{path}: votes must be a list of at most {MAX_FEEDBACK_VOTES}")
    checked, seen_ids, seen_keys = [], set(), set()
    for index, raw in enumerate(votes):
        vote = _normalize_public_vote(raw, f"{path}: votes[{index}]")
        if vote["vote_id"] in seen_ids:
            raise FeedbackError(f"{path}: duplicate vote_id {vote['vote_id']!r}")
        key = _vote_key(vote)
        if key in seen_keys:
            raise FeedbackError(f"{path}: duplicate vote for the same root/unit/revision")
        seen_ids.add(vote["vote_id"])
        seen_keys.add(key)
        checked.append(vote)
    return {"schema": SCHEMA_FEEDBACK, "votes": checked}


def merge_votes(existing: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]]) -> list[dict]:
    combined = []
    for source in (existing, new):
        if source is None:
            continue
        if isinstance(source, Mapping):
            source = [source]
        combined.extend(_normalize_public_vote(raw) for raw in source)
    merged = {_vote_key(vote): vote for vote in combined}
    return [merged[key] for key in sorted(merged)]


def render_feedback(votes: Iterable[Mapping[str, Any]]) -> str:
    ordered = merge_votes((), votes)
    return json.dumps({"schema": SCHEMA_FEEDBACK, "votes": ordered},
                      ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def parse_feedback_ref(ref: Any, domain: str | None = None) -> dict:
    if not isinstance(domain, str) or _DOMAIN.fullmatch(domain) is None:
        raise FeedbackReferenceError("feedback domain must be a canonical domain name")
    if not isinstance(ref, str):
        raise FeedbackReferenceError("feedback requires an observed graded reference")
    match = _REF.fullmatch(ref)
    if match is None:
        raise FeedbackReferenceError("feedback requires mindie://DOMAIN/units/ID@REVISION")
    if match.group("domain") != domain:
        raise FeedbackReferenceError("feedback reference is outside the selected domain")
    unit_id = match.group("unit_id")
    if UNIT_ID.fullmatch(unit_id) is None:
        raise FeedbackReferenceError("feedback requires an exact stable graded unit identifier")
    return {"kind": "feedback", "domain": domain, "unit_id": unit_id,
            "revision": _revision(int(match.group("revision")))}


def _unit_fields(unit: GradedUnit) -> tuple[str, int]:
    try:
        return _unit_id(unit.id), _revision(unit.revision)
    except (AttributeError, KeyError, TypeError) as exc:
        raise FeedbackError("observed value is not a valid graded unit") from exc


def _observed_key(unit_id: str, revision: int) -> str:
    return f"{OBSERVED_PREFIX}{unit_id}:{revision}"


def _decode_observed(raw: Any, *, unit_id: str | None = None, revision: int | None = None) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise FeedbackError("stored graded observation is unreadable") from None
    if (not isinstance(value, dict) or not _OBSERVED_FIELDS <= set(value)
            or set(value) - (_OBSERVED_FIELDS | _OBSERVED_DELIVERIES)):
        raise FeedbackError("stored graded observation is not a compact identity")
    observed = {"unit_id": _unit_id(value["unit_id"]),
                "revision": _stored_revision(value["revision"])}
    for name in _OBSERVED_DELIVERIES:
        if name in value:
            count = value[name]
            if type(count) is not int or count < 0:
                raise FeedbackError("stored graded delivery count is invalid")
            observed[name] = count
    if unit_id is not None and observed["unit_id"] != unit_id:
        raise FeedbackError("stored graded observation names another unit")
    if revision is not None and observed["revision"] != revision:
        raise FeedbackError("stored graded observation revision disagrees with its key")
    return observed


def _read_observed(store, unit_id: str, revision: int) -> dict:
    unit_id, revision = _unit_id(unit_id), _revision(revision)
    row = store.db.execute("SELECT value FROM state WHERE key=?",
                           (_observed_key(unit_id, revision),)).fetchone()
    if row is None:
        raise FeedbackError("feedback requires a graded revision actually returned to the caller")
    observed = _decode_observed(row[0], unit_id=unit_id, revision=revision)
    known = store.db.execute("SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?",
                             (unit_id, revision)).fetchone()
    if known is None:
        raise FeedbackError("observed graded revision is missing from known_revisions")
    return observed


def _event_type(value: Any) -> str:
    if value not in EVENT_TYPES:
        raise FeedbackError(f"event_type must be one of {EVENT_TYPES}")
    return value


def _optional_text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > MAX_IDENTITY_CHARS:
        raise FeedbackError(f"{name} must be bounded text")
    return value.strip()


def _eval_outcome(value: Any) -> str:
    if value not in EVAL_OUTCOMES:
        raise FeedbackError(f"evaluated usage requires outcome in {EVAL_OUTCOMES}")
    return value


def _event_id(opaque: str, unit_id: str, revision: int, event_type: str, run_id: str) -> str:
    return hashlib.sha256(canonical(
        ["usage_event", opaque, unit_id, revision, event_type, run_id]
    ).encode("utf-8")).hexdigest()


def _event_key(unit_id: str, revision: int, event_id: str) -> str:
    return f"{EVENT_PREFIX}{unit_id}:{revision}:{event_id}"


def _decode_event(raw: Any) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise FeedbackError("stored usage event is unreadable") from None
    if (not isinstance(value, dict) or not _EVENT_LEGACY_FIELDS <= set(value)
            or set(value) - _EVENT_FIELDS):
        raise FeedbackError("stored usage event is not a compact identity")
    outcome = value["outcome"]
    if outcome is not None:
        outcome = _eval_outcome(outcome)
    evidence_source = value.get("evidence_source", "agent_declared")
    if evidence_source not in EVIDENCE_SOURCES:
        raise FeedbackError(f"evidence_source must be one of {EVIDENCE_SOURCES}")
    return {"event_id": _identity_text(value["event_id"], "event_id"),
            "root_id": _identity_text(value["root_id"], "root_id"),
            "unit_id": _unit_id(value["unit_id"]),
            "revision": _stored_revision(value["revision"]),
            "event_type": _event_type(value["event_type"]),
            "run_id": _optional_text(value["run_id"], "run_id"),
            "outcome": outcome,
            "step_id": _optional_text(value["step_id"], "step_id"),
            "reason": _reason(value["reason"]),
            "evidence_source": evidence_source}


def _write_usage_event(store, *, opaque: str, observed: dict, event_type: str,
                       run_id: str, outcome: str | None, step_id: str, reason: str,
                       evidence_source: str = "agent_declared") -> dict:
    if evidence_source not in EVIDENCE_SOURCES:
        raise FeedbackError(f"evidence_source must be one of {EVIDENCE_SOURCES}")
    unit_id, revision = observed["unit_id"], observed["revision"]
    event_id = _event_id(opaque, unit_id, revision, event_type, run_id)
    key = _event_key(unit_id, revision, event_id)
    row = store.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if row is not None:
        return {**_decode_event(row[0]), "duplicate": True}
    event = {"event_id": event_id, "root_id": opaque, "unit_id": unit_id,
             "revision": revision, "event_type": event_type, "run_id": run_id,
             "outcome": outcome, "step_id": step_id, "reason": reason,
             "evidence_source": evidence_source}
    store.db.execute("INSERT INTO state VALUES(?,?)", (key, canonical(event)))
    return {**event, "duplicate": False}


def record_usage_event(store, *, root_hash: str, ref: str, event_type: str,
                       run_id: str = "", outcome: str | None = None,
                       step_id: str = "", reason: str = "",
                       evidence_source: str = "agent_declared") -> dict:
    """Append one idempotent 4-stage usage/evaluation event for an observed unit."""
    root = _identity_text(root_hash, "root_hash")
    event_type = _event_type(event_type)
    run_id = _optional_text(run_id, "run_id")
    step_id = _optional_text(step_id, "step_id")
    reason = _reason(reason)
    if evidence_source not in EVIDENCE_SOURCES:
        raise FeedbackError(f"evidence_source must be one of {EVIDENCE_SOURCES}")
    if event_type == "evaluated":
        outcome = _eval_outcome(outcome)
        if not run_id:
            raise FeedbackError("evaluated usage requires a nonempty run_id")
    elif outcome is not None:
        raise FeedbackError("outcome is only valid for an evaluated usage event")
    parsed = parse_feedback_ref(ref, store.domain)
    with store._write_txn():
        observed = _read_observed(store, parsed["unit_id"], parsed["revision"])
        opaque = store.opaque_for(root)
        return _write_usage_event(store, opaque=opaque, observed=observed,
                                  event_type=event_type, run_id=run_id, outcome=outcome,
                                  step_id=step_id, reason=reason,
                                  evidence_source=evidence_source)


def summarize_usage_events(store, unit_id: str, revision: int | None = None, *,
                           include_delivery: bool = False) -> dict:
    """Derive counts from deduplicated events; inconclusive never becomes evidence."""
    unit_id = _unit_id(unit_id)
    if revision is not None:
        revision = _revision(revision)
    if type(include_delivery) is not bool:
        raise FeedbackError("include_delivery must be a boolean")
    counts = {event_type: 0 for event_type in EVENT_TYPES}
    outcomes = {outcome: 0 for outcome in EVAL_OUTCOMES}
    evidence_sources = {source: 0 for source in EVIDENCE_SOURCES}
    deliveries = {name: 0 for name in _OBSERVED_DELIVERIES}
    with store.lock:
        rows = store.db.execute(
            "SELECT value FROM state WHERE key LIKE ?", (f"{EVENT_PREFIX}{unit_id}:%",)
        ).fetchall()
        for row in rows:
            event = _decode_event(row[0])
            if revision is not None and event["revision"] != revision:
                continue
            counts[event["event_type"]] += 1
            evidence_sources[event["evidence_source"]] += 1
            if event["event_type"] == "evaluated":
                outcomes[event["outcome"]] += 1
        if include_delivery:
            observed_rows = store.db.execute(
                "SELECT value FROM state WHERE key LIKE ?",
                (f"{OBSERVED_PREFIX}{unit_id}:%",)
            ).fetchall()
            for row in observed_rows:
                observed = _decode_observed(row[0], unit_id=unit_id)
                if revision is not None and observed["revision"] != revision:
                    continue
                for name in _OBSERVED_DELIVERIES:
                    deliveries[name] += observed.get(name, 0)
    summary = {"unit_id": unit_id, "revision": revision, **counts, **outcomes}
    if include_delivery:
        summary.update(deliveries)
        summary["evidence_sources"] = evidence_sources
    return summary


def observe(store, unit: GradedUnit, *, delivery=None, run_id=None) -> dict:
    unit_id, revision = _unit_fields(unit)
    identity = {"unit_id": unit_id, "revision": revision}
    if delivery not in {None, "query", "explain"}:
        raise FeedbackError("delivery must be query, explain, or None")
    counter = None if delivery is None else f"{delivery}_deliveries"
    key = _observed_key(unit_id, revision)
    with store.lock:
        row = store.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is not None:
            observed = _decode_observed(row[0], unit_id=unit_id, revision=revision)
            if any(observed[name] != value for name, value in identity.items()):
                raise FeedbackError("graded observation identity changed for the same revision")
            known = store.db.execute("SELECT 1 FROM known_revisions WHERE entry_id=? AND revision=?",
                                     (unit_id, revision)).fetchone()
            if known is not None and counter is None:
                return identity
    with store._write_txn():
        row = store.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is not None:
            observed = _decode_observed(row[0], unit_id=unit_id, revision=revision)
            if any(observed[name] != value for name, value in identity.items()):
                raise FeedbackError("graded observation identity changed for the same revision")
            if counter is not None:
                updated = {**observed, counter: observed.get(counter, 0) + 1}
                store.db.execute("UPDATE state SET value=? WHERE key=?",
                                 (canonical(updated), key))
        else:
            stored = dict(identity)
            if counter is not None:
                stored[counter] = 1
            store.db.execute("INSERT INTO state VALUES(?,?)", (key, canonical(stored)))
        store.db.execute("INSERT OR IGNORE INTO known_revisions VALUES(?,?)", (unit_id, revision))
    return identity


def _vote_id(root: str, unit_id: str, revision: int) -> str:
    return hashlib.sha256(canonical(["vote", root, unit_id, revision]).encode("utf-8")).hexdigest()


def record_vote(store, *, root_hash: str, ref: str, rating: str | None = None,
                reason: str = "", publishable: bool = False, generation: str | None = None,
                event_type: str | None = None, run_id: str = "",
                outcome: str | None = None, step_id: str = "",
                evidence_source: str = "agent_declared") -> dict:
    """Record a rating and/or one usage event; both are atomically idempotent."""
    if rating is None and event_type is None:
        raise FeedbackError("feedback requires rating or event_type")
    reason = _reason(reason)
    if type(publishable) is not bool:
        raise FeedbackError("publishable must be a boolean")
    if generation is not None and publishable and (
            not isinstance(generation, str) or not generation.strip()):
        raise FeedbackError("generation must be a nonempty sharing generation")
    if rating is not None:
        rating = _rating(rating)
        if publishable:
            _scan_publishable_reason(reason)
    root = _identity_text(root_hash, "root_hash")
    parsed = parse_feedback_ref(ref, store.domain)
    unit_id, revision = parsed["unit_id"], parsed["revision"]
    if event_type is not None:
        event_type = _event_type(event_type)
        run_id, step_id = _optional_text(run_id, "run_id"), _optional_text(step_id, "step_id")
        if evidence_source not in EVIDENCE_SOURCES:
            raise FeedbackError(f"evidence_source must be one of {EVIDENCE_SOURCES}")
        if event_type == "evaluated":
            outcome = _eval_outcome(outcome)
            if not run_id:
                raise FeedbackError("evaluated usage requires a nonempty run_id")
        elif outcome is not None:
            raise FeedbackError("outcome is only valid for an evaluated usage event")
    with store._write_txn():
        observed = _read_observed(store, unit_id, revision)
        opaque = store.opaque_for(root)
        event = None
        if event_type is not None:
            event = _write_usage_event(store, opaque=opaque, observed=observed,
                                       event_type=event_type, run_id=run_id,
                                       outcome=outcome, step_id=step_id, reason=reason,
                                       evidence_source=evidence_source)
        if rating is None:
            return event
        store.db.execute("INSERT OR REPLACE INTO votes VALUES(?,?,?,?,?,?,NULL,?)",
                         (opaque, unit_id, str(revision), rating, reason,
                          1 if publishable else 0, time.time()))
        if publishable and generation:
            store.db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                             ("vote", store.vote_identity(opaque, unit_id),
                              str(revision), generation, time.time()))
        vote = {"vote_id": _vote_id(opaque, unit_id, revision), "root_id": opaque,
                "unit_id": unit_id, "revision": observed["revision"],
                "rating": rating, "publishable": publishable}
    if event is not None:
        vote["usage_event"] = event
    return vote


def _outbound_identity(row: Mapping[str, Any], label: str) -> tuple[str, str, int]:
    root = _identity_text(row.get("root_opaque") or row.get("root_id"), f"{label}.root_id")
    if "entry_id" in row:
        return root, _unit_id(row.get("entry_id")), _stored_revision(row.get("revision"))
    if "unit_id" in row:
        return root, _unit_id(row.get("unit_id")), _revision(row.get("revision"))
    raise FeedbackError("graded vote requires entry_id/revision or unit_id/revision")


def outbound_votes(store, votes: Iterable[Mapping[str, Any]]) -> list[dict]:
    if votes is None:
        return []
    if isinstance(votes, Mapping):
        votes = [votes]
    public = []
    with store.lock:
        for index, raw in enumerate(votes):
            row = _mapping(raw, f"internal vote[{index}]")
            root, unit_id, revision = _outbound_identity(row, f"internal vote[{index}]")
            observed = _read_observed(store, unit_id, revision)
            reason = row.get("reason", "")
            if "publishable" in row and not row.get("publishable"):
                reason = ""
            public.append({"vote_id": row.get("vote_id") or _vote_id(root, unit_id, revision),
                           "root_id": root, "unit_id": unit_id,
                           "revision": observed["revision"], "rating": row.get("rating"),
                           "reason": reason})
    return merge_votes((), public)


def _countable_vote(raw: Any, label: str) -> dict:
    vote = _mapping(raw, label)
    return {"root_id": _identity_text(vote.get("root_id"), f"{label}.root_id"),
            "unit_id": _unit_id(vote.get("unit_id")),
            "revision": _revision(vote.get("revision")),
            "rating": _rating(vote.get("rating"))}


def summarize_votes(votes: Iterable[Mapping[str, Any]], unit: GradedUnit) -> dict:
    unit_id, revision = _unit_fields(unit)
    if votes is None:
        votes = ()
    if isinstance(votes, Mapping):
        votes = [votes]
    by_root = {}
    for index, raw in enumerate(votes):
        vote = _countable_vote(raw, f"votes[{index}]")
        if vote["unit_id"] != unit_id or vote["revision"] != revision:
            continue
        by_root[vote["root_id"]] = vote
    up = sum(1 for vote in by_root.values() if vote["rating"] == "up")
    down = sum(1 for vote in by_root.values() if vote["rating"] == "down")
    return {"unit_id": unit_id, "revision": revision, "up": up, "down": down, "total": up + down}


__all__ = [
    "EVAL_OUTCOMES", "EVENT_TYPES", "EVIDENCE_SOURCES", "FeedbackError",
    "FeedbackReferenceError", "SCHEMA_FEEDBACK", "merge_votes", "observe",
    "outbound_votes", "parse_feedback_ref", "record_usage_event", "record_vote",
    "render_feedback", "summarize_usage_events", "summarize_votes",
    "validate_feedback",
]
