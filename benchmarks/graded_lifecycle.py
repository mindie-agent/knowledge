"""[Role] Verify the published graded Feed lifecycle in a real local Git repo.

[Invariants] Conditions, cross-directory inheritance and withdrawn references
use production readers. Synthetic content establishes protocol behavior only.
[Timeouts & Locks] Git/Feed share 15s; the suite allows 20s including cleanup.
SQLite uses the core WAL/5s connection.
[Side-Effect Policy] Writes only a temporary repository/store and the requested
benchmark result; no network, model, native host or hardware invocation.
"""
from contextlib import closing
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mindie_knowledge.graded.units import GradedUnit
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.materials.references import ReadReferenceError
from mindie_knowledge.publication_contract import GRADED_SCHEMA, make_contract, render_contract


def remaining(deadline):
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("graded lifecycle exceeded its shared 15s budget")
    return budget


def run_git(repo, *args, deadline):
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=min(5, remaining(deadline)),
                                   stderr=subprocess.PIPE, text=True).strip()


def unit(ident, domain_path, *, parents=()):
    body = "Synthetic decode graph rule for release protocol verification.\n"
    metadata = dict(
        schema="mindie-graded-unit/1", id=ident, tier="L2", category="sop",
        evidence_strength="single_observation", domain_path=domain_path,
        inherits_from=list(parents), title="Synthetic decode graph verification",
        triggers=dict(error_signatures=[], keywords=["decode", "graph"]),
        conditions=dict(soc=["Ascend910B"], cann=">=8.0,<9", workload_scope="decode"),
        provenance=dict(source_type="ecosystem_repo", source_locator="release-fixture:graded-lifecycle",
                        source_digest=hashlib.sha256(body.encode()).hexdigest(),
                        evidence_refs=["synthetic-protocol-observation"], supersedes=[]),
    )
    return GradedUnit.from_parts(metadata, body, domain_path + "/" + ident + ".md")


def main():
    started = time.perf_counter()
    deadline = time.monotonic() + 15
    git = partial(run_git, deadline=deadline)
    runtime_revision = git(ROOT, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="mindie-graded-release-") as temporary:
        root = Path(temporary)
        remote = root / "publication"
        remote.mkdir()
        git(remote, "init", "-q", "-b", "main")
        git(remote, "config", "user.name", "Release protocol fixture")
        git(remote, "config", "user.email", "release@example.invalid")
        git(remote, "config", "core.autocrlf", "false")
        git(remote, "config", "core.eol", "lf")
        parent = unit("unit-release-premise", "principles/execution")
        child = unit("unit-release-decode", "serving/decode", parents=[parent.id])
        for value in (parent, child):
            path = remote / value.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value.render(), encoding="utf-8")
        (remote / "publication-contract.json").write_text(render_contract(
            make_contract("vllm-ascend", runtime_revision, schema=GRADED_SCHEMA)), encoding="utf-8")
        git(remote, "add", ".")
        git(remote, "commit", "-qm", "Publish synthetic graded protocol corpus")
        first = git(remote, "rev-parse", "HEAD")
        with closing(Store(root / "reader", "vllm-ascend")) as store:
            feed = Feed(store, dict(repository="fixture/graded-release", domain="vllm-ascend",
                                    ref="main", url=str(remote)))
            synced = feed.sync(force=True, timeout=remaining(deadline))
            assert synced["status"] == "synced" and synced["commit"] == first, synced
            assert synced["contract_schema"] == GRADED_SCHEMA and synced["entries"] == 2
            observed = dict(soc="Ascend910B", cann="8.1", workload_scope="decode")
            found = store.query("decode graph", conditions=observed)["results"]
            selected = next(value for value in found if value["id"] == child.id)
            assert [value["id"] for value in selected["inherited_rules"]] == [parent.id]
            assert store.explain(selected["ref"])["inherits_from_closure"] == [parent.id]
            assert store.query("decode graph", conditions={**observed, "cann": "9.1"})["results"] == []
            assert store.query("decode graph", conditions={"soc": "Ascend910B"})["results"] == []
            assert len(store.query("decode graph", conditions={"soc": "Ascend910B"},
                                   include_unknown=True)["results"]) == 2

            # A deleted premise cannot be interpreted as a valid child-only feed.
            (remote / parent.path).unlink()
            git(remote, "add", "-A")
            git(remote, "commit", "-qm", "Reject an unresolved inheritance edge")
            rejected = feed.sync(force=True, timeout=remaining(deadline))
            assert rejected["status"] == "invalid" and rejected["retained_commit"] == first, rejected
            assert store.explain(selected["ref"])["id"] == child.id

            # Complete withdrawal empties membership while cache bytes may remain.
            (remote / child.path).unlink()
            git(remote, "add", "-A")
            git(remote, "commit", "-qm", "Withdraw the complete synthetic graph")
            withdrawn = feed.sync(force=True, timeout=remaining(deadline))
            assert withdrawn["status"] == "synced" and withdrawn["entries"] == 0, withdrawn
            assert store.query("decode graph", conditions=observed, include_unknown=True)["results"] == []
            for old_ref in [selected["ref"], selected["inherited_rules"][0]["ref"]]:
                try:
                    store.explain(old_ref)
                except ReadReferenceError as exc:
                    assert exc.code == "removed_or_superseded"
                else:
                    raise AssertionError("withdrawn reference exposed cached knowledge")
    result = dict(status="pass", metrics=[dict(name="graded_feed_lifecycle", unit="ms/run",
        value=(time.perf_counter() - started) * 1000, iterations=1)])
    result_path = Path(os.environ["MINDIE_BENCH_RESULT_PATH"])
    # The fixed release Measurement schema accepts only status and metrics;
    # retain detailed local acceptance independently of that numeric payload.
    evidence = dict(status="pass", evidence_scope="synthetic_local_git_protocol_only",
        contract=GRADED_SCHEMA, source_revision=runtime_revision, condition_match=True,
        mismatch_excluded=True, missing_fact_excluded=True, cross_directory_inheritance=True,
        invalid_graph_retained_previous=True, complete_withdrawal=True, old_references_rejected=2)
    result_path.with_name(result_path.stem + "-acceptance.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    result_path.write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
