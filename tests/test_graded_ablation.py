"""Consume complete receipts emitted by the real Bench C4 evaluator."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from graded_fixture import decision, unit
from mindie_knowledge.graded.distill import _sha, validate_decision


def prepared(receipt):
    content = json.dumps(receipt)
    selected = dict(kind="ablation_receipt", outcome="verified", source_locator="fixture:c4",
        source_digest=hashlib.sha256(content.encode()).hexdigest(), content=content,
        evidence_refs=["fixture-c4-receipt"], repetitions=3)
    request = dict(schema_version="mindie-distill-request/1", session_digest="a" * 64,
        task_state="completed", evidence_inputs=["ablation_receipt"], sanitized_inputs=[selected],
        redaction_rules=[], redaction_completed=True, model_calls=0)
    request["request_digest"] = _sha(request)
    conditions = deepcopy(receipt["runs"][0]["conditions"])
    if isinstance(conditions.get("workload"), list):
        conditions["workload"] = {"exact_set": conditions["workload"]}
    value = unit(tier="L4", category="mutator", evidence_strength="reproduced_empirical",
        conditions=conditions, provenance=dict(source_type="ablation_receipt",
        source_locator=selected["source_locator"], source_digest=selected["source_digest"],
        evidence_refs=selected["evidence_refs"], supersedes=[]))
    return request, decision(request, value)


def receipt(name="all_retained"):
    return json.loads((Path(__file__).parent / "fixtures/graded-ablation-verified.json").read_text())[name]


def test_all_retained_independent_final_is_valid_without_a_pruning_claim():
    value = receipt()
    assert value["final_combination_verified"] and not value["pruned_combination_verified"]
    request, proposal = prepared(value)
    assert validate_decision(proposal, request)["units"][0]["target_tier"] == "L4"


def test_older_v1_receipt_requires_its_actual_verified_pruning_chain():
    value = receipt("pruned")
    value.pop("final_combination")
    value.pop("final_combination_verified")
    request, proposal = prepared(value)
    assert validate_decision(proposal, request)["action"] == "propose_graded_units"
    value["pruning_verifications"] = []
    request, proposal = prepared(value)
    with pytest.raises(ValueError, match="final-combination"):
        validate_decision(proposal, request)


def test_unknown_historical_runtime_premise_cannot_admit_automatic_l4():
    value = receipt()
    for run in value["runs"]:
        run["conditions"]["driver"] = "unknown"
    request, proposal = prepared(value)
    with pytest.raises(ValueError, match="final-combination"):
        validate_decision(proposal, request)


@pytest.mark.parametrize("change", ["full_reused", "wrong_factors", "wrong_conditions", "final_unverified"])
def test_final_receipt_must_bind_its_distinct_verified_run(change):
    value = deepcopy(receipt())
    if change == "full_reused":
        value["final_combination"]["verification_run_id"] = value["full_candidate_run_id"]
    elif change == "wrong_factors":
        value["final_combination"]["retained_factors"] = []
    elif change == "wrong_conditions":
        value["runs"][-1]["conditions"]["machine"] = "another-simulator"
    else:
        value["final_combination_verified"] = False
    request, proposal = prepared(value)
    with pytest.raises(ValueError, match="final-combination"):
        validate_decision(proposal, request)


def test_l4_multi_case_applicability_requires_complete_paired_workload():
    value = receipt()
    cases = [dict(input_shape='1x37x4x128', workload_case_sha256='a' * 64),
             dict(input_shape='2x131x4x128', workload_case_sha256='b' * 64)]
    for run in value['runs']:
        run['conditions'].update(input_shape=[x['input_shape'] for x in cases],
                                 workload_case_sha256=[x['workload_case_sha256'] for x in cases])
    request, proposal = prepared(value)
    with pytest.raises(ValueError, match='final-combination'):
        validate_decision(proposal, request)

    for run in value['runs']:
        run['conditions'].pop('input_shape')
        run['conditions'].pop('workload_case_sha256')
        run['conditions']['workload'] = cases
    # Actual run conditions contain observations; only the rule uses exact_set.
    selected = json.dumps(value)
    source_digest = hashlib.sha256(selected.encode()).hexdigest()
    request['sanitized_inputs'][0].update(content=selected, source_digest=source_digest)
    request.pop('request_digest')
    request['request_digest'] = _sha(request)
    row = proposal['units'][0]
    frontmatter = row['frontmatter']
    frontmatter['conditions'] = {**value['runs'][0]['conditions'], 'workload': {'exact_set': cases}}
    frontmatter['provenance']['source_digest'] = source_digest
    frontmatter.pop('content_digest', None)
    assert validate_decision(proposal, request)['action'] == 'propose_graded_units'
    frontmatter['conditions'].pop('workload')
    with pytest.raises(ValueError, match='final-combination'):
        validate_decision(proposal, request)


def case_block_receipt():
    return json.loads((Path(__file__).parent / 'fixtures/graded-ablation-a2-case-block.json').read_text())


def test_captured_a2_case_block_receipt_binds_final_comparisons_and_complete_workload():
    value = case_block_receipt()
    request, proposal = prepared(value)
    assert validate_decision(proposal, request)['action'] == 'propose_graded_units'
    final = value['final_combination']
    assert final['final_speedup_geomean'] == pytest.approx(1.6767473221869322)
    assert final['baseline_comparison']['reference_run_id'] != final['retention_comparison']['reference_run_id']
    # Query applicability remains complete even if an OR-list of shapes is present.
    from mindie_knowledge.graded.conditions import match_conditions
    required = proposal['units'][0]['frontmatter']['conditions']
    observed = deepcopy(value['runs'][0]['conditions'])
    observed['workload'] = observed['workload'][:1]
    assert match_conditions(required, observed).status == 'mismatch'


@pytest.mark.parametrize('change', [
    'raw_interval', 'unsynchronized', 'missing_sample', 'wrong_case', 'missing_block_source',
    'baseline_identity', 'timing_digest', 'ratio', 'envelope', 'uncertainty', 'missing_workload',
])
def test_c6_rejects_v2_measurement_identity_and_comparison_tampering(change):
    value = case_block_receipt()
    final = value['final_combination']
    run = next(row for row in value['runs'] if row['run_id'] == final['verification_run_id'])
    if change == 'raw_interval':
        run['timing']['samples'][0]['ended_ns'] += 100
    elif change == 'unsynchronized':
        run['timing']['samples'][0]['synchronized_after'] = False
    elif change == 'missing_sample':
        run['sample_groups'][0]['source_sample_indices'].pop()
    elif change == 'wrong_case':
        run['sample_groups'][0]['case_sha256'] = run['sample_groups'][-1]['case_sha256']
    elif change == 'missing_block_source':
        run['sample_groups'][0]['source_sha256'] = ''
    elif change == 'baseline_identity':
        final['baseline_comparison']['reference_run_id'] = value['full_candidate_run_id']
    elif change == 'timing_digest':
        final['retention_comparison']['candidate_timing_sha256'] = 'a' * 64
    elif change == 'ratio':
        final['baseline_comparison']['speedup_geomean'] = 1.776276267851459
    elif change == 'envelope':
        final['baseline_comparison']['cases'][0]['observed_block_ratio_lower'] = 2.0
    elif change == 'uncertainty':
        final['uncertainty_verified'] = False
    else:
        for row in value['runs']:
            row['conditions'].pop('workload')
    request, proposal = prepared(value)
    with pytest.raises(ValueError, match='final-combination'):
        validate_decision(proposal, request)
