"""Orthogonal tests derived from root-only profiling of four real K3 tasks.

Only anonymous sizes/stage facts are used; no transcript text or source identity
is copied. K3-01: initial adaptation (945 records, 306267 public bytes). K3-02:
benchmark/checkpoint correction (320, 102083). K3-03: repeated progress and a
pending prefix-cache investigation (643, 283102). K3-04: resumed precision work
(1195, 461158). Failures below are controlled perturbations of those dimensions,
not claims that the original sessions encountered these protocol failures.
"""
import json
import sqlite3
import unittest

from langchain_core.messages import AIMessage
from mindie_knowledge.materials import summarizer as summary


CASES = {
    "K3-01": (945, 306267, "Initial adaptation hypothesis remains unverified."),
    "K3-02": (320, 102083, "A later checkpoint correction changes the initial benchmark interpretation."),
    "K3-03": (643, 283102, "Repeated progress does not establish a prefix-cache outcome; investigation pending."),
    "K3-04": (1195, 461158, "The same precision task continues after interruption; latest outcome unresolved."),
}


def case_blocks(case, *, sized=False):
    records, public_bytes, fact = CASES[case]
    size = public_bytes if sized else 900
    result, offset = [], 0
    while offset < size:
        count = min(summary.MAX_BLOCK_BYTES, size - offset)
        marker = f"ANONYMOUS {case} part {len(result)}: {fact}\n"
        text = (marker + "progress observation\n" * (count // 20 + 1))[:count]
        block_id = summary.digest([case, offset, text])
        result.append(dict(block_id=block_id, text=text,
                           source_range=dict(case=case, public_start=offset,
                                             public_end=offset + count, source_records=records)))
        offset += count
    return result


def policy():
    return summary.policy_identity(model="fixture-small-model", effort="low", implementation={"fixture": "K3-anonymous-v1"})


def request(case, blocks=None, previous=None):
    return summary.make_request(task_id=summary.digest(case), body_version=summary.digest([case, "current"]),
                                blocks=blocks or case_blocks(case), prior_navigation=previous, identity=policy())


class RecordingModel:
    def __init__(self, case):
        self.case = case
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        blocks = [json.loads(message.content) for message in messages if message.id]
        result = dict(blocks=[dict(block_id=block["block_id"], title=self.case,
                                   summary=CASES[self.case][2]) for block in blocks],
                      navigation=dict(title=self.case, summary=CASES[self.case][2]))
        return AIMessage(content=summary.canonical(result))


def returned(req, model=None):
    model = model or RecordingModel("K3-04")
    result = summary.summarize_batch(req, model)
    return summary.outcome(req, status="returned", result=result, raw_result=summary.canonical(result),
                           model_calls=1, usage_known=True,
                           usage=dict(input_tokens=120, cached_input_tokens=20, output_tokens=30), elapsed_ms=10)


class MaterialSummarizerTests(unittest.TestCase):
    def test_k3_01_initial_batch_reads_every_block_in_one_call(self):
        model = RecordingModel("K3-01")
        req = request("K3-01")
        result = summary.summarize_batch(req, model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual([message.id for message in model.calls[0] if message.id],
                         [block["block_id"] for block in req["blocks"]])
        self.assertEqual([item["block_id"] for item in result["blocks"]],
                         [block["block_id"] for block in req["blocks"]])
        self.assertEqual(result["navigation"]["title"], "K3-01")

    def test_k3_02_correction_uses_previous_navigation_without_old_body(self):
        initial = case_blocks("K3-01")
        first = summary.summarize_batch(request("K3-02", initial), RecordingModel("K3-01"))
        model = RecordingModel("K3-02")
        correction = case_blocks("K3-02")
        result = summary.summarize_batch(request("K3-02", correction, first["navigation"]), model)
        prompt = summary.render_prompt([dict(role=message.type, content=message.content) for message in model.calls[0]])
        self.assertIn(first["navigation"]["summary"], prompt)
        self.assertIn("checkpoint correction", prompt)
        self.assertNotIn(initial[0]["text"], prompt)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["navigation"]["title"], "K3-02")

    def test_k3_03_high_volume_partition_has_no_trim_or_final_merge(self):
        blocks = case_blocks("K3-03", sized=True)
        batches = summary.partition_blocks(blocks)
        self.assertEqual(sum(len(block["text"].encode()) for block in blocks), 283102)
        self.assertEqual([block for batch in batches for block in batch], blocks)
        model, previous, seen = RecordingModel("K3-03"), None, []
        for batch in batches:
            req = request("K3-03", batch, previous)
            result = summary.summarize_batch(req, model)
            previous = result["navigation"]
            latest = model.calls[-1]
            seen.extend(json.loads(message.content) for message in latest if message.id)
            self.assertLessEqual(len(summary.render_prompt([
                dict(role=message.type, content=message.content) for message in latest]).encode()), summary.MAX_PROMPT_BYTES)
        self.assertEqual(len(model.calls), len(batches))
        self.assertEqual(seen, [{key: block[key] for key in ("block_id", "text")} for block in blocks])

    def test_k3_04_returned_output_survives_scanner_failure_without_recalling_model(self):
        db = sqlite3.connect(":memory:")
        ledger, req, model = summary.SummaryLedger(db, initialize=True), request("K3-04"), RecordingModel("K3-04")
        with db:
            attempt = ledger.prepare(req)
            ledger.claim(attempt["attempt_id"])
        response = returned(req, model)
        with db:
            ledger.record(attempt["attempt_id"], response)
            ledger.local_failure(attempt["attempt_id"], "scanner_unavailable")
        restored = summary.SummaryLedger(db)
        self.assertEqual(restored.recover(), 0)
        saved = restored.prepare(req)
        self.assertEqual(saved["status"], "returned")
        self.assertEqual(json.loads(saved["response"])["raw_result"], response["raw_result"])
        with self.assertRaises(summary.SummaryConflict):
            restored.claim(attempt["attempt_id"])
        with db:
            restored.complete(attempt["attempt_id"])
        self.assertNotIn("raw_result", json.loads(restored.get(attempt["attempt_id"])["response"]))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(restored.usage_totals(req["task_id"])["model_calls"], 1)
        db.close()

    def test_k3_04_interruption_is_unknown_until_explicit_retry_and_cost_accumulates(self):
        db = sqlite3.connect(":memory:")
        ledger, req = summary.SummaryLedger(db, initialize=True), request("K3-04")
        with db:
            attempt = ledger.prepare(req)
            ledger.claim(attempt["attempt_id"])
            self.assertEqual(ledger.recover(), 1)
        self.assertEqual(ledger.prepare(req)["status"], "outcome_unknown")
        with self.assertRaises(summary.SummaryConflict):
            ledger.claim(attempt["attempt_id"])
        with db:
            retry = ledger.retry(attempt["attempt_id"], req)
            ledger.claim(retry["attempt_id"])
            ledger.record(retry["attempt_id"], returned(req))
            ledger.complete(retry["attempt_id"])
        self.assertEqual(ledger.usage_totals(req["task_id"]), dict(
            model_calls=2, unknown_calls=1, input_tokens=120, cached_input_tokens=20, output_tokens=30))
        db.close()

    def test_k3_02_partial_or_mismatched_index_is_not_success(self):
        req = request("K3-02")
        response = returned(req, RecordingModel("K3-02"))
        response["result"]["blocks"] = []
        with self.assertRaises(summary.SummaryResultError):
            summary.validate_outcome(response, req)

    def test_k3_04_policy_upgrade_cannot_reuse_a_prior_attempt_silently(self):
        db = sqlite3.connect(":memory:")
        ledger, req = summary.SummaryLedger(db, initialize=True), request("K3-04")
        with db:
            ledger.prepare(req)
        changed = summary.make_request(task_id=req["task_id"], body_version=req["body_version"], blocks=req["blocks"],
                                       prior_navigation=None, identity=summary.policy_identity(
                                           model="fixture-small-model", effort="low", implementation={"fixture": "K3-revised-policy"}))
        self.assertNotEqual(changed["input_digest"], req["input_digest"])
        with self.assertRaises(summary.SummaryConflict):
            ledger.prepare(changed)
        db.close()

    def test_k3_01_rejected_route_attempt_has_unknown_usage_but_no_unknown_paid_call(self):
        db = sqlite3.connect(":memory:")
        ledger, req = summary.SummaryLedger(db, initialize=True), request("K3-01")
        with db:
            attempt = ledger.prepare(req)
            ledger.claim(attempt["attempt_id"])
            ledger.record(attempt["attempt_id"], summary.outcome(
                req, status="failed", error="configuration", model_calls=1,
                billing_status="rejected", usage=None, usage_known=False))
        receipt = json.loads(ledger.get(attempt["attempt_id"])["response"])
        self.assertIsNone(receipt["usage"])
        self.assertEqual(ledger.usage_totals(req["task_id"])["model_calls"], 1)
        self.assertEqual(ledger.usage_totals(req["task_id"])["unknown_calls"], 0)
        db.close()


if __name__ == "__main__":
    unittest.main()


def test_k3_real_failure_extra_duplicate_block_is_rejected_and_schema_limits_count():
    ids = [summary.digest(['anonymous-k3-04', i]) for i in range(3)]
    schema = summary.output_schema(ids)['properties']['blocks']
    assert schema['minItems'] == schema['maxItems'] == 3
    result = dict(blocks=[dict(block_id=i, title='Reference', summary='Reported observation') for i in [*ids, ids[-1]]],
                  navigation=dict(title='Reference', summary='Incomplete task'))
    import pytest
    with pytest.raises(summary.SummaryResultError, match='incomplete') as caught:
        summary.validate_result(result, ids)
    assert caught.value.reason == 'block_count_mismatch'


def test_summary_failure_details_are_static_and_cannot_expose_source():
    import pytest
    req = request("K3-04")
    failed = summary.outcome(req, status="failed", error="invalid_result",
                             error_reason="block_identity_mismatch", model_calls=1,
                             raw_result="PRIVATE_MODEL_OUTPUT", usage_known=True,
                             usage=dict(input_tokens=120, cached_input_tokens=0, output_tokens=30))
    assert summary.failure_detail(failed) == dict(error="invalid_result", reason="block_identity_mismatch",
        stage="index-validation", message="returned block IDs are missing, duplicated, unexpected or out of order")
    assert "PRIVATE_MODEL_OUTPUT" not in json.dumps(summary.failure_detail(failed))
    summary.validate_outcome(failed, req)
    with pytest.raises(summary.SummaryResultError, match="failure reason"):
        summary.outcome(req, status="failed", error="invalid_result", error_reason="PRIVATE_SOURCE_VALUE")
    with pytest.raises(ValueError, match="failure diagnostic"):
        summary.failure_detail(dict(error="invalid_result", error_reason="PRIVATE_SOURCE_VALUE"))
