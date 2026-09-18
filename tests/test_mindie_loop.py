"""Behavioral checks for the new single-domain loop, independent of OpenViking."""

import copy
import json
import subprocess
import sys
import threading
import time

import pytest

from mindie_knowledge.loop.cli import capture_hook
from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical, digest, session_key
from mindie_knowledge.loop.transport import Service, rpc


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path, "vllm-ascend")
    yield instance
    instance.close()


def experience(
    store,
    title="ACL graph investigation",
    content="Compare eager and graph execution before investigating capture.",
    producer="producer-a",
):
    return store.add(
        kind="experience",
        title=title,
        content=content,
        producers=[session_key(producer)],
    )


def completed_use(store, doc, consumer="consumer-b"):
    use = store.use(
        ref=doc["id"],
        session_id=consumer,
        application="Compared eager and graph execution",
        evidence="Eager passed, graph failed at capture; isolated the failing execution mode.",
    )
    store.capture(
        consumer, "turn-1", "The comparison isolated graph capture for investigation."
    )
    return use["use_id"]


def test_knowledge_applicability_is_separate_from_experience(store):
    with pytest.raises(ValueError, match="knowledge requires"):
        store.add(kind="knowledge", title="ACL graph", content="Versioned reference")
    store.add(
        kind="knowledge",
        title="ACL graph",
        content="Versioned reference",
        source={"url": "https://example.com/docs", "revision": "abc123"},
        conditions={"CANN": "9"},
    )
    exp = experience(store)
    results = store.query("ACL graph", conditions={"CANN": "8"})["results"]
    assert [r["ref"] for r in results] == [store.ref(exp["id"])]
    assert store.query("ACL graph", conditions={"CANN": "9"})["results"][0][
        "conditions"
    ] in ({}, {"CANN": "9"})


def test_capture_and_feedback_idempotency_and_independence(store):
    doc = experience(store)
    assert experience(store)["id"] == doc["id"]
    own = completed_use(store, doc, "producer-a")
    with pytest.raises(ValueError, match="producer"):
        store.judge(own, judge_id="fresh-judge", verdict="helpful", reason="self-use")
    use = completed_use(store, doc)
    assert completed_use(store, doc) == use
    for ident in ("producer-a", "consumer-b"):
        with pytest.raises(ValueError, match="independent"):
            store.judge(
                use, judge_id=ident, verdict="helpful", reason="not independent"
            )
    store.judge(
        use,
        judge_id="judge-c",
        verdict="helpful",
        reason="Observed a narrower investigation",
    )
    store.judge(
        use,
        judge_id="judge-d",
        verdict="unhelpful",
        reason="Duplicate cannot add another vote",
    )
    assert store.weight(doc["id"])["helpful"] == 1
    assert store.weight(doc["id"])["unhelpful"] == 0
    assert len(store.status()["captures"]) == 2


def test_unknown_is_neutral_and_repeated_negative_withdraws(store):
    doc = experience(store)
    before = store.query("ACL graph")["results"][0]["score"]
    use = completed_use(store, doc)
    store.judge(
        use,
        judge_id="judge-u",
        verdict="unknown",
        reason="Insufficient evidence of contribution",
    )
    assert store.query("ACL graph")["results"][0]["score"] == before
    for i in range(3):
        use = completed_use(store, doc, f"independent-negative-{i}")
        store.judge(
            use,
            judge_id=f"judge-{i}",
            verdict="unhelpful",
            reason="Observed wasted effort",
        )
    assert store.weight(doc["id"])["withdrawn"]
    assert store.query("ACL graph")["results"] == []
    assert store.get(doc["id"])["content"]  # retained for inspection


def test_snapshots_reject_foreign_or_tampered_data_before_install(store, tmp_path):
    doc = experience(store)
    store.publish(doc["id"])
    snapshot = store.snapshot()
    other = Store(tmp_path / "other", "ascendc")
    target = Store(tmp_path / "target", "vllm-ascend")
    try:
        with pytest.raises(ValueError, match="domain"):
            other.install_snapshot(snapshot)
        with pytest.raises(ValueError, match="outside"):
            other.get(store.ref(doc["id"]))
        bad = copy.deepcopy(snapshot)
        bad["entries"].append(dict(doc, id="0" * 64))
        bad["version"] = digest({k: v for k, v in bad.items() if k != "version"})
        with pytest.raises(ValueError, match="digest"):
            target.install_snapshot(bad)
        assert target.status()["entries"] == 0
        target.install_snapshot(snapshot)
        target.install_snapshot(snapshot)
        assert target.status()["entries"] == 1
        assert target.get(doc["id"]) == doc
    finally:
        other.close()
        target.close()


def test_raw_capture_use_and_judge_prose_are_not_in_snapshot(store):
    doc = experience(store)
    use = completed_use(store, doc)
    store.judge(
        use, judge_id="judge-c", verdict="helpful", reason="PRIVATE_JUDGE_PROSE"
    )
    store.publish(doc["id"])
    public = canonical(store.snapshot())
    assert "PRIVATE_JUDGE_PROSE" not in public
    assert "Eager passed, graph failed" not in public
    assert "consumer-b" not in public
    assert store.snapshot()["feedback"][0]["verdict"] == "helpful"


def test_consumer_summary_dedup_preserves_original_provenance_and_vote(store):
    doc = experience(store)
    use = completed_use(store, doc)
    repeated = experience(store, producer="consumer-b")
    assert repeated["producers"] == [session_key("producer-a")]
    store.judge(
        use,
        judge_id="judge-c",
        verdict="helpful",
        reason="Observed useful contribution",
    )
    assert store.weight(doc["id"])["helpful"] == 1


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("timed out waiting for background maintenance")


@pytest.fixture
def services(tmp_path):
    # Deterministic protocol fixture, not evidence of live model quality.
    runner = tmp_path / "agent.py"
    runner.write_text(
        "import json,sys\np=json.load(sys.stdin)\n"
        'print(json.dumps({"entries":[{"title":"ACL graph comparison","content":p["session_summary"]}]} '
        'if p["role"]=="organize" else {"verdict":"helpful","reason":"The supplied comparison narrowed the investigation."}))\n'
    )
    created = []

    def make(name, upstream=None):
        store = Store(tmp_path / name, "vllm-ascend")
        engine = Engine(
            store,
            agent_command=[sys.executable, str(runner)],
            auto_publish=not bool(upstream),
        )
        service = Service(engine, upstream=upstream)
        thread = threading.Thread(target=service.serve, daemon=True)
        thread.start()
        wait_for(lambda: engine.thread.is_alive())
        created.append((service, thread))
        return service

    yield make
    for service, thread in reversed(created):
        service.close()
        thread.join(3)
        service.engine.thread.join(3)
        service.store.close()


def test_http_capture_distribution_independent_use_judging_and_next_retrieval(services):
    authority = services("authority-a")
    a = authority.connection
    assert (
        rpc(
            a,
            "capture",
            dict(session_id="unrelated", turn_id="t", summary="private chat"),
        )["status"]
        == "skipped"
    )
    rpc(a, "query", dict(query="ACL graph", session_id="producer-a"))
    rpc(
        a,
        "capture",
        dict(
            session_id="producer-a",
            turn_id="t1",
            summary="Compare eager and graph execution first; an eager pass and graph capture failure narrowed investigation to graph capture.",
        ),
    )
    wait_for(lambda: authority.store.snapshot()["entries"])
    doc = authority.store.snapshot()["entries"][0]
    b = services("consumer-b", a)
    wait_for(lambda: b.sync()["status"] == "synced")
    before = rpc(
        b.connection, "query", dict(query="ACL graph", session_id="consumer-b")
    )["results"][0]
    use = rpc(
        b.connection,
        "use",
        dict(
            ref=before["ref"],
            session_id="consumer-b",
            application="Compared eager and graph",
            evidence="Eager passed, graph capture failed; isolated the failing mode.",
        ),
    )
    rpc(
        b.connection,
        "capture",
        dict(
            session_id="consumer-b",
            turn_id="t2",
            summary="The comparison isolated graph capture for investigation.",
        ),
    )
    wait_for(lambda: b.sync()["status"] == "synced")
    wait_for(lambda: authority.store.weight(doc["id"])["helpful"] == 1)
    assert not b.store.status()["feedback"]  # only authority judges before sync
    c = services("consumer-c", a)
    wait_for(lambda: c.sync()["status"] == "synced")
    after = rpc(
        c.connection, "query", dict(query="ACL graph", session_id="consumer-c")
    )["results"][0]
    assert after["ref"] == before["ref"]
    assert after["score"] > before["score"]
    assert after["usefulness"]["helpful"] == 1
    b.sync()
    b.sync()
    assert authority.store.weight(doc["id"])["helpful"] == 1
    assert authority.store.status()["feedback"][0]["use_id"] == use["use_id"]


def test_failed_judge_does_not_automatically_retry_or_create_a_vote(store):
    doc = experience(store)
    completed_use(store, doc)
    engine = Engine(store, agent_command=[sys.executable, "-c", "raise SystemExit(1)"])
    engine.evaluate()
    engine.evaluate()
    assert not store.status()["feedback"]
    assert len(store.status()["failed_judges"]) == 1
    assert len(engine.errors) == 1


def test_running_mcp_reconnects_after_owned_service_restart(store, tmp_path):
    experience(store)
    config = tmp_path / "config.json"
    config.write_text(
        canonical(
            dict(
                root=str(store.root.parent),
                domain=store.domain,
                agent_command=[sys.executable, "-c", "pass"],
            )
        )
    )

    def start():
        engine = Engine(store, agent_command=[sys.executable, "-c", "pass"])
        service = Service(engine, connection_path=store.root / "connection.json")
        thread = threading.Thread(target=service.serve, daemon=True)
        thread.start()
        wait_for(lambda: engine.thread.is_alive())
        return service, thread

    service, thread = start()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mindie_knowledge.loop.cli",
            "mcp",
            "--config",
            str(config),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    def query(number):
        process.stdin.write(
            canonical(
                dict(
                    jsonrpc="2.0",
                    id=number,
                    method="tools/call",
                    params=dict(
                        name="knowledge_query",
                        arguments=dict(query="ACL graph", session_id="consumer"),
                    ),
                )
            )
            + "\n"
        )
        process.stdin.flush()
        return json.loads(process.stdout.readline())["result"]

    try:
        assert query(1)["structuredContent"]["results"]
        service.close()
        thread.join(3)
        service, thread = start()
        assert query(2)["structuredContent"]["results"]
    finally:
        process.terminate()
        process.wait(5)
        service.close()
        thread.join(3)


def test_only_explicitly_published_replica_entries_are_contributed(services):
    authority = services("publisher")
    replica = services("contributor", authority.connection)
    local = experience(replica.store, content="Unpublished material remains local.")
    shared = experience(
        replica.store,
        title="CPU helper imports",
        content="Load a pure helper directly to avoid unrelated package initialization.",
    )
    replica.store.publish(shared["id"])
    wait_for(lambda: replica.sync()["status"] == "synced")
    ids = {e["id"] for e in authority.store.snapshot()["entries"]}
    assert shared["id"] in ids
    assert local["id"] not in ids
    replica.sync()
    assert authority.store.status()["entries"] == 1


def test_offline_hook_is_bounded_and_creates_no_queue(tmp_path):
    config = tmp_path / "engine.json"
    config.write_text(
        canonical(
            dict(
                root=str(tmp_path / "absent"),
                domain="vllm-ascend",
                agent_command=["missing"],
            )
        )
    )
    start = time.monotonic()
    capture_hook(
        config,
        dict(
            hook_event_name="Stop",
            session_id="a",
            turn_id="t",
            last_assistant_message="summary",
        ),
    )
    assert time.monotonic() - start < 1
    assert not (tmp_path / "absent").exists()


def test_mcp_initialization_and_scoped_tool_contract(services, tmp_path):
    service = services("mcp")
    root = service.store.root.parent
    (service.store.root / "connection.json").write_text(canonical(service.connection))
    config = tmp_path / "mcp.json"
    config.write_text(
        canonical(dict(root=str(root), domain="vllm-ascend", agent_command=["unused"]))
    )
    calls = [
        dict(jsonrpc="2.0", id=1, method="initialize"),
        dict(jsonrpc="2.0", id=2, method="tools/list"),
        dict(
            jsonrpc="2.0",
            id=3,
            method="tools/call",
            params=dict(
                name="knowledge_query", arguments=dict(query="graph", session_id="task")
            ),
        ),
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mindie_knowledge.loop.cli",
            "mcp",
            "--config",
            str(config),
        ],
        input="\n".join(canonical(c) for c in calls) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    replies = [json.loads(line)["result"] for line in completed.stdout.splitlines()]
    assert replies[0]["serverInfo"]["name"] == "mindie-knowledge"
    assert {t["name"] for t in replies[1]["tools"]} == {
        "knowledge_query",
        "knowledge_explain",
        "knowledge_use",
    }
    assert replies[2]["structuredContent"]["domain"] == "vllm-ascend"
    assert service.store.attached("task")
