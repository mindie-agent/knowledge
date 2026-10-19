"""[Role] Execute C6 through the existing native worker and SummaryLedger.

[Invariants] One admitted paid call per evidence request. Returned decisions
resume local application; failed or uncertain calls are never replayed here.
[Timeouts & Locks] Existing summary_budget bounds identity/model subprocesses;
Gitleaks uses its 30s budget outside SQLite transactions. Writes use Store locks.
[Side-Effect Policy] Runs the configured worker; applies local graded units and
prepares an inspectable contribution plan. No Git/GitHub write occurs.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from ..loop import summary_budget
from ..loop.dfx import failure
from ..loop.process import bounded_run
from ..loop.transcript_redaction import redact
from ..materials.summarizer import SummaryLedger, outcome, validate_identity, validate_outcome
from .distill import (apply_decision, noop_decision, prepare_request, validate_decision,
                      result_view, value_gate, with_worker_identity)
from .units import canonical, locator_parts


def _local_failure(response, attempt, error, stage, *, recording_error=None, decision=None):
    diagnostic = failure("knowledge.distill", stage=stage, category="local_failure", exception=error)
    returned = response["status"] == "returned"
    result = {"status": ("local_receipt_failed" if stage == "receipt" else "local_application_failed")
                       if returned else response["status"],
            "attempt_id": attempt["attempt_id"],
            "model_execution": "completed" if returned else response["status"],
            "model_status": response["status"], "model_replayed": False,
            "model_calls": response["model_calls"], "usage": response["usage"],
            "billing_status": response["billing_status"], "publication": "not_requested",
            "local_error": {"stage": stage, "type": type(error).__name__, "message": str(error)[:500]},
            "recording_failed": stage == "receipt" or recording_error is not None,
            "diagnostic": diagnostic}
    if returned:
        result["result"] = decision if decision is not None else response["result"]
        result["result_admission"] = ("local_application_pending" if decision is not None
                                      else "pending_local_redaction")
    return result_view(result)[0]


def run(engine, inputs, *, session_digest, task_state, authorization=None, cancel=None):
    store = engine.store

    def revalidate():
        if authorization is not None:
            engine._gate_live()
            engine._revalidate(authorization)

    revalidate()
    private_paths = ((authorization["scope"],) if authorization else ()) + (str(Path.home()),)
    request = prepare_request(inputs, session_digest=session_digest, task_state=task_state,
                              redactor_executable=engine.redactor_executable, redaction_key=store.redaction_key(),
                              private_paths=private_paths, cancel=cancel)
    gate = value_gate(request)
    if gate:
        return apply_decision(store, request, noop_decision(request, gate))
    if not engine.summary_command:
        raise ValueError("C6 requires a configured native summary worker")
    # The same command supplies its declared capability before receiving any
    # new protocol. An older paired installation is rejected before model use.
    identity = validate_identity(json.loads(bounded_run(
        [*engine.summary_command, "--identity"], "{}", timeout=summary_budget.IDENTITY_SECONDS,
        max_output=8192, cancel=cancel)))
    request = with_worker_identity(request, identity)
    ledger = SummaryLedger(store.db)
    receipt_key = "graded:applied:" + request["batch_id"]
    with store._write_txn():
        revalidate()
        attempt = ledger.prepare(request)
        if attempt["status"] == "complete":
            row = store.db.execute("SELECT value FROM state WHERE key=?", (receipt_key,)).fetchone()
            if row is None:
                raise ValueError("completed distillation is missing its local application receipt")
            return dict(json.loads(row[0]), replay="existing_local_receipt")
        if attempt["status"] in {"failed", "outcome_unknown", "invoking"}:
            return {"status": attempt["status"], "attempt_id": attempt["attempt_id"],
                    "model_replayed": False, "publication": "not_requested"}
        invoke = attempt["status"] == "prepared"
        if invoke:
            ledger.claim(attempt["attempt_id"])
    if invoke:
        timeout = summary_budget.worker_timeout()
        returned = None
        cleanup = {}

        def save_result(raw):
            nonlocal returned
            response = json.loads(raw)
            validate_outcome(response, request)
            returned = response
            try:
                with store._write_txn():
                    ledger.record(attempt["attempt_id"], response)
            except Exception as exc:
                exc.mindie_execution = "completed"
                exc.mindie_result_status = response["status"]
                raise

        try:
            raw = bounded_run(engine.summary_command, canonical(request), timeout=timeout,
                              max_output=393216, cancel=cancel, on_result=save_result,
                              cleanup_receipt=cleanup,
                              child_env={summary_budget.DEADLINE_ENV: str(time.monotonic() + timeout)})
            if ledger.get(attempt["attempt_id"])["status"] == "invoking":
                save_result(raw)
        except Exception as exc:
            if returned is not None:
                return _local_failure(returned, attempt, exc, "receipt")
            with store._write_txn():
                current = ledger.get(attempt["attempt_id"])
                if current["status"] == "invoking" and getattr(exc, "mindie_execution", None) != "completed":
                    if getattr(exc, "mindie_execution", None) == "not_started":
                        ledger.record(attempt["attempt_id"], outcome(request, status="failed", error="configuration",
                                                                   model_calls=0, error_reason="native_start_failed"))
                    else:
                        ledger.uncertain(attempt["attempt_id"], "unknown")
            raise
    attempt = ledger.get(attempt["attempt_id"])
    if attempt["status"] != "returned":
        return {"status": attempt["status"], "attempt_id": attempt["attempt_id"],
                "model_replayed": False, "publication": "not_requested"}
    response = json.loads(attempt["response"])
    decision = None
    try:
        pending = json.loads(canonical(response["result"]))
        for item in pending.get("units", []):
            provenance = item["frontmatter"]["provenance"]
            provenance["source_locator"] = locator_parts(provenance["source_locator"]) or provenance["source_locator"]
        clean, _ = redact(canonical(pending), executable=engine.redactor_executable,
                          key=store.redaction_key(), private_paths=private_paths, cancel=cancel)
        pending = json.loads(clean)
        for item in pending.get("units", []):
            provenance = item["frontmatter"]["provenance"]
            locator = provenance["source_locator"]
            if isinstance(locator, dict):
                provenance["source_locator"] = f"{locator['repository']}@{locator['revision']}:{locator['path']}"
        decision = validate_decision(pending, request)
        with store._write_txn():
            revalidate()
            result = apply_decision(store, request, decision,
                                    generation=authorization.get("generation") if authorization else None)
            receipt = {key: value for key, value in result.items() if key != "publication_plan"}
            receipt.update(attempt_id=attempt["attempt_id"], model_calls=response["model_calls"],
                           usage=response["usage"], billing_status=response["billing_status"])
            if invoke and cleanup:
                receipt.update(cleanup)
            store.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (receipt_key, canonical(receipt)))
            ledger.complete(attempt["attempt_id"])
        return {**result, **receipt}
    except Exception as exc:
        recording_error = None
        try:
            with store._write_txn():
                if ledger.get(attempt["attempt_id"])["status"] == "returned":
                    ledger.local_failure(attempt["attempt_id"], "apply_failed")
        except Exception as receipt_error:
            recording_error = receipt_error
        return _local_failure(response, attempt, exc, "apply", recording_error=recording_error,
                              decision=decision)
