"""[Role] Retain scoped candidate rejection history in the existing Store.

[Invariants] Rejections and rollbacks are observations, never adopted knowledge.
Each summary is <=4 KiB; queries return at most five newest matching records.
[Timeouts & Locks] Reuses Store's WAL connection, 5s busy timeout and transactions.
[Side-Effect Policy] Writes compact local state receipts only; no second database.
"""
from __future__ import annotations

import hashlib
import json

from .units import canonical, locator_parts

MAX_HISTORY_ITEMS = 5
MAX_HISTORY_BYTES = 4096
PREFIX = "graded:rejected:"


def scope_digest(scope):
    """Private matching identity remains stable when prompt scope is redacted."""
    return {key: hashlib.sha256(value.encode()).hexdigest() for key, value in scope.items()}


def history_scope(inputs=(), related_units=(), *, topic=None, target_signature=None):
    """Use declared scope, then the already selected related unit; never guess facts."""
    if topic is None and target_signature is None:
        for item in (*inputs, *(related_units or ())):
            topic, target_signature = item.get("topic"), item.get("target_signature")
            if topic is not None or target_signature is not None:
                break
    if topic is None and target_signature is None and inputs:
        locator = inputs[0].get("source_locator")
        parts = locator_parts(locator)
        topic = (parts["repository"] + ":" + parts.get("path", "")
                 if parts else locator)
    result = {}
    for key, value in (("topic", topic), ("target_signature", target_signature)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > 512:
                raise ValueError(f"{key} must be nonempty text within 512 bytes")
            result[key] = value
    return result


class GradedStore:
    """A compact-history view over the authoritative loop.Store."""

    def __init__(self, store):
        self.store = store

    def record_candidate(self, *, topic=None, target_signature=None, unit_id,
                         gate_reason, status="rejected", missing_counterexample_conditions=(),
                         candidate_summary="", candidate_id=None, _scope_digest=None):
        scope = history_scope(topic=topic, target_signature=target_signature)
        if not scope:
            raise ValueError("candidate history requires topic or target_signature")
        if status not in {"rejected", "rolled_back"}:
            raise ValueError("candidate history records only rejected or rolled_back observations")
        row = {**scope, "unit_id": unit_id, "gate_reason": gate_reason, "status": status,
               "missing_counterexample_conditions": list(missing_counterexample_conditions),
               "candidate_summary": candidate_summary, "_scope_digest": _scope_digest or scope_digest(scope)}
        if not isinstance(unit_id, str) or not unit_id or not isinstance(gate_reason, str) or not gate_reason:
            raise ValueError("candidate history requires unit_id and gate_reason")
        if len(canonical(row).encode()) > MAX_HISTORY_BYTES:
            raise ValueError("candidate history summary exceeds 4 KiB")
        ident = candidate_id or hashlib.sha256(canonical(row).encode()).hexdigest()
        key = PREFIX + hashlib.sha256(str(ident).encode()).hexdigest()
        with self.store._write_txn():
            # An identical failed application retry is not a new observation.
            self.store.db.execute("INSERT OR IGNORE INTO state VALUES(?,?)", (key, canonical(row)))
        return row

    def recent_rejected(self, *, topic=None, target_signature=None, limit=MAX_HISTORY_ITEMS):
        scope = history_scope(topic=topic, target_signature=target_signature)
        if not scope or limit <= 0:
            return []
        clauses = ["key GLOB ?"]
        params = [PREFIX + "*"]
        for name, value in scope_digest(scope).items():
            clauses.append(f"json_extract(value, '$._scope_digest.{name}')=?")
            params.append(value)
        params.append(min(int(limit), MAX_HISTORY_ITEMS))
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT value FROM state WHERE " + " AND ".join(clauses)
                + " ORDER BY rowid DESC LIMIT ?", params).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != "_scope_digest"}
                for row in rows]


def record_rejection(store, request, decision, error):
    """Keep actionable static failures, without recording local I/O as bad knowledge."""
    if not isinstance(decision, dict) or not isinstance(decision.get("units"), list):
        return
    scope = request.get("history_scope", {})
    for row in decision.get("units", []):
        if not isinstance(row, dict):
            continue
        metadata = row.get("frontmatter", {})
        if not isinstance(metadata, dict):
            continue
        unit_id = metadata.get("id") or row.get("target_unit_id")
        if not unit_id or (getattr(error, "unit_id", None) and error.unit_id != unit_id):
            continue
        candidate_scope = scope or history_scope(
            topic=metadata.get("topic", metadata.get("domain_path")),
            target_signature=metadata.get("target_signature", unit_id))
        GradedStore(store).record_candidate(
            **candidate_scope, unit_id=unit_id, gate_reason=str(error)[:1024],
            missing_counterexample_conditions=getattr(error, "missing_conditions", ()),
            candidate_summary=str(metadata.get("title", ""))[:256],
            candidate_id=request["request_digest"] + ":" + hashlib.sha256(canonical(row).encode()).hexdigest(),
            _scope_digest=request.get("history_scope_digest"),
        )
