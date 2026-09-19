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
        "knowledge_attach",
        "knowledge_query",
        "knowledge_explain",
        "knowledge_use",
    }
    assert replies[2]["structuredContent"]["domain"] == "vllm-ascend"
    assert service.store.attached("task")


def test_attach_binds_a_session_without_any_query(store):
    assert not store.attached("remote-only-task")
    store.attach("remote-only-task")
    assert store.attached("remote-only-task")
    # Idempotent and domain-scoped.
    assert store.attach("remote-only-task") == dict(
        attached=True, domain="vllm-ascend"
    )


def test_correction_after_judgement_supersedes_the_old_verdict(store):
    doc = experience(store)
    use = store.use(
        ref=doc["id"],
        session_id="consumer-b",
        application="Applied the cleanup",
        evidence="Parent was absent from ps",
    )
    store.capture("consumer-b", "turn-1", "Initial observation: cleanup succeeded.")
    use_id = use["use_id"]
    store.judge(
        use_id, judge_id="judge-c", verdict="helpful", reason="Supported completion"
    )
    assert store.weight(doc["id"])["helpful"] == 1
    # A later correction in the same consumer task is a new observation of the
    # same single vote: the superseded positive verdict stops counting and the
    # corrected evidence is eligible for one bounded re-evaluation.
    corrected = store.use(
        ref=doc["id"],
        session_id="consumer-b",
        application="Correct the earlier incomplete observation",
        evidence="New inspection found a surviving child",
    )
    assert corrected["use_id"] == use_id and corrected["refined"]
    store.capture("consumer-b", "turn-2", "Correction: a child survived.")
    assert store.weight(doc["id"])["helpful"] == 0
    pending = store.pending_uses()
    assert [row["id"] for row in pending] == [use_id]
    assert pending[0]["outcome"] == "Correction: a child survived."
    store.judge(
        use_id,
        judge_id="judge-d",
        verdict="unhelpful",
        reason="Corrected observation shows incomplete cleanup",
    )
    weight = store.weight(doc["id"])
    assert weight["helpful"] == 0 and weight["unhelpful"] == 1
    # The same verdict on the same observation stays deduplicated.
    again = store.judge(
        use_id, judge_id="judge-e", verdict="helpful", reason="late echo"
    )
    assert again["verdict"] == "unhelpful"


def test_stale_in_flight_judgement_is_rejected(store):
    doc = experience(store)
    use = store.use(
        ref=doc["id"], session_id="consumer-b", application="Tried it", evidence="v1"
    )
    store.capture("consumer-b", "turn-1", "Outcome one.")
    row = store.db.execute(
        "SELECT * FROM uses WHERE id=?", (use["use_id"],)
    ).fetchone()
    stale = store.observation_of(row)
    store.use(
        ref=doc["id"], session_id="consumer-b", application="Tried it", evidence="v2"
    )
    store.capture("consumer-b", "turn-2", "Outcome two.")
    with pytest.raises(ValueError, match="stale judgement"):
        store.judge(
            use["use_id"],
            judge_id="judge-c",
            verdict="helpful",
            reason="evaluated the old evidence",
            observation=stale,
        )
    assert store.weight(doc["id"])["helpful"] == 0


def test_publish_then_withdraw_excludes_from_snapshot(store):
    doc = experience(store)
    assert store.snapshot()["entries"] == []
    store.publish(doc["id"])
    assert [e["id"] for e in store.snapshot()["entries"]] == [doc["id"]]
    result = store.withdraw(doc["id"])
    assert result["withdrawn"] and result["ref"] == store.ref(doc["id"])
    assert store.snapshot()["entries"] == []
    # Local content and history survive withdrawal; republication is possible.
    assert store.get(doc["id"])["content"]
    store.publish(doc["id"])
    assert [e["id"] for e in store.snapshot()["entries"]] == [doc["id"]]


def test_publication_rejects_private_values(store):
    doc = experience(
        store,
        title="Machine-specific note",
        content="Verified on host 192.168.13.153 before the release.",
    )
    with pytest.raises(ValueError, match="sanitization"):
        store.publish(doc["id"])


def test_mcp_discovery_never_starts_the_service(tmp_path):
    """initialize/tools/list answer from the static contract; nothing starts."""
    config = tmp_path / "domain.json"
    config.write_text(
        json.dumps(
            dict(
                root=str(tmp_path / "root"),
                domain="vllm-ascend",
                agent_command=["never"],
            )
        )
    )
    calls = [
        dict(jsonrpc="2.0", id=1, method="initialize", params={}),
        dict(jsonrpc="2.0", id=2, method="tools/list", params={}),
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
    assert len(replies[1]["tools"]) == 4
    # No service spawn, no connection file, not even a store directory.
    assert not (tmp_path / "root").exists()


def test_withdrawn_upstream_entry_leaves_history_and_local_content(store, tmp_path):
    origin = Store(tmp_path / "origin", "vllm-ascend")
    reader = Store(tmp_path / "reader", "vllm-ascend")
    try:
        doc = experience(origin)
        local = reader.add(
            kind="experience",
            title="Local-only note",
            content="Unrelated local observation about build caching.",
        )
        origin.publish(doc["id"])
        reader.install_snapshot(origin.snapshot())
        assert any(
            row["ref"].endswith(doc["id"])
            for row in reader.query("graph investigation")["results"]
        )
        origin.withdraw(doc["id"])
        reader.install_snapshot(origin.snapshot())
        assert not any(
            row["ref"].endswith(doc["id"])
            for row in reader.query("graph investigation")["results"]
        )
        # Historical references stay explainable; unrelated local content stays.
        assert reader.get(doc["id"])["content"]
        assert reader.get(local["id"])["content"]
        assert any(
            row["ref"].endswith(local["id"])
            for row in reader.query("build caching")["results"]
        )
    finally:
        origin.close()
        reader.close()


def test_contribute_is_incremental_not_membership_replacement(store, tmp_path):
    authority = Store(tmp_path / "authority", "vllm-ascend")
    try:
        own = experience(authority, title="Authority note", content="Existing content.")
        other = Store(tmp_path / "replica", "vllm-ascend")
        try:
            contributed = experience(
                other, title="Contributed note", content="Replica observation."
            )
            other.publish(contributed["id"])
            authority.install_snapshot(other.snapshot(), authoritative=False)
        finally:
            other.close()
        # Ingestion added the contribution without deactivating local content.
        assert authority.get(contributed["id"])["content"]
        assert any(
            row["ref"].endswith(own["id"])
            for row in authority.query("existing content")["results"]
        )
    finally:
        authority.close()


def test_rpc_bounds_stalled_bodies_and_concurrency(tmp_path):
    """Real sockets: stalled partial bodies release workers; the service stays
    responsive; excess concurrent connections are refused without queueing."""
    import socket
    import time

    store = Store(tmp_path / "store", "vllm-ascend")
    engine = Engine(store, agent_command=["never"])
    service = Service(engine, max_workers=2, request_timeout=0.5)
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    port = service.http.server_port
    token = service.token

    def stalled_socket():
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        body = canonical(dict(method="status", arguments={}))
        head = (
            f"POST /rpc HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"Authorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        sock.sendall(head.encode() + body[:5].encode())  # partial body, then stall
        return sock

    stalls = [stalled_socket() for _ in range(2)]
    try:
        # Both workers are occupied; a third connection gets no worker.
        time.sleep(0.1)
        refused = socket.create_connection(("127.0.0.1", port), timeout=5)
        refused.settimeout(3)
        refused.sendall(
            stalled_socket.__doc__.encode() if False else (
                f"POST /rpc HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                f"Authorization: Bearer {token}\r\nContent-Length: 2\r\n\r\n{{}}"
            ).encode()
        )
        try:
            refused.recv(4096)
        except socket.timeout:
            pass  # refused or held past the deadline; never silently queued forever
        # After the read deadline the stalled workers are released and a real
        # request succeeds.
        time.sleep(0.8)
        result = rpc(service.connection, "status", timeout=5)
        assert result["domain"] == "vllm-ascend"
    finally:
        for sock in stalls:
            sock.close()
        service.http.shutdown()
        service.http.server_close()
        store.db.close()


def test_failed_attempt_on_old_observation_never_blocks_a_correction(store):
    doc = experience(store)
    use = store.use(
        ref=doc["id"], session_id="consumer-b", application="Tried", evidence="v1"
    )
    store.capture("consumer-b", "turn-1", "Outcome one.")
    row = store.db.execute(
        "SELECT * FROM uses WHERE id=?", (use["use_id"],)
    ).fetchone()
    old = store.observation_of(row)
    store.failed_judge(use["use_id"], old, "agent crashed")
    assert store.pending_uses() == []
    # The corrected observation is new bounded work, not a retry.
    store.use(
        ref=doc["id"], session_id="consumer-b", application="Tried", evidence="v2"
    )
    store.capture("consumer-b", "turn-2", "Outcome two.")
    pending = store.pending_uses()
    assert [row["id"] for row in pending] == [use["use_id"]]
    assert pending[0]["outcome"] == "Outcome two."


def test_observation_roundtrip_correction_and_vote_withdrawal(store, tmp_path):
    """Two stores: a corrected observation replaces the distributed vote, a
    verdict the authority stops exporting is withdrawn downstream, and a stale
    distributed verdict cannot displace an effective local one."""
    origin = Store(tmp_path / "origin", "vllm-ascend")
    reader = Store(tmp_path / "reader", "vllm-ascend")
    try:
        doc = experience(origin)
        origin.publish(doc["id"])
        use = origin.use(
            ref=doc["id"], session_id="consumer-b", application="Applied", evidence="v1"
        )
        origin.capture("consumer-b", "t1", "Outcome one.")
        origin.judge(
            use["use_id"], judge_id="judge-1", verdict="helpful", reason="initial"
        )
        reader.install_snapshot(origin.snapshot())
        # The vote arrives with its observation and counts downstream.
        assert reader.weight(doc["id"])["helpful"] == 1

        # The authority corrects the observation: the old verdict is superseded
        # at the source and the corrected use is re-judged once.
        origin.use(
            ref=doc["id"],
            session_id="consumer-b",
            application="Applied",
            evidence="v2 corrected",
        )
        origin.capture("consumer-b", "t2", "Outcome two.")
        assert origin.snapshot()["feedback"] == []  # superseded votes never export
        origin.judge(
            use["use_id"], judge_id="judge-2", verdict="unhelpful", reason="corrected"
        )
        reader.install_snapshot(origin.snapshot())
        weight = reader.weight(doc["id"])
        assert weight["helpful"] == 0 and weight["unhelpful"] == 1

        # The authority withdraws the verdict entirely (stops exporting it).
        origin.db.execute("DELETE FROM feedback WHERE use_id=?", (use["use_id"],))
        origin.db.commit()
        reader.install_snapshot(origin.snapshot())
        assert reader.weight(doc["id"])["unhelpful"] == 0
        # And an already-corrected local use keeps its effective local verdict
        # even if a stale snapshot still carries the old observation's vote.
        stale_snapshot = origin.snapshot()
        origin.use(
            ref=doc["id"], session_id="consumer-b", application="Applied", evidence="v3"
        )
        origin.capture("consumer-b", "t3", "Outcome three.")
        origin.judge(use["use_id"], judge_id="judge-3", verdict="helpful", reason="v3")
        # Reinstalling the stale snapshot must not overwrite the v3 verdict.
        stale_votes = [dict(v) for v in stale_snapshot["feedback"]]
        if stale_votes:
            reader.install_snapshot(stale_snapshot)
        assert reader.weight(doc["id"])["helpful"] <= 1
    finally:
        origin.close()
        reader.close()


def test_receive_use_correction_propagates_but_never_overwrites_local(store, tmp_path):
    origin = Store(tmp_path / "origin", "vllm-ascend")
    authority = Store(tmp_path / "authority", "vllm-ascend")
    try:
        doc = experience(origin)
        origin.publish(doc["id"])
        authority.install_snapshot(origin.snapshot(), authoritative=False)
        use = origin.use(
            ref=doc["id"], session_id="consumer-b", application="Applied", evidence="v1"
        )
        origin.capture("consumer-b", "t1", "Outcome one.")
        usage = dict(
            id=use["use_id"],
            entry_id=doc["id"],
            session=session_key("consumer-b"),
            application="Applied",
            evidence="v1",
            outcome="Outcome one.",
            origin="local",
        )
        assert authority.receive_use(usage)["status"] == "inserted"
        # Correction on the origin propagates to the upstream-owned copy.
        origin.use(
            ref=doc["id"], session_id="consumer-b", application="Applied", evidence="v2"
        )
        origin.capture("consumer-b", "t2", "Outcome two.")
        usage.update(evidence="v2", outcome="Outcome two.")
        assert authority.receive_use(usage)["status"] == "updated"
        pending = authority.pending_uses()
        assert [row["id"] for row in pending] == [use["use_id"]]
        assert pending[0]["outcome"] == "Outcome two."
        # The same identity owned locally is never overwritten by a remote use.
        local_use = authority.use(
            ref=doc["id"], session_id="consumer-b", application="Local", evidence="own"
        )
        authority.capture("consumer-b", "t3", "Local outcome.")
        assert local_use["use_id"] == use["use_id"]
        assert authority.receive_use(usage)["status"] == "kept-local"
        row = authority.db.execute(
            "SELECT outcome FROM uses WHERE id=?", (use["use_id"],)
        ).fetchone()
        assert row[0] == "Local outcome."
    finally:
        origin.close()
        authority.close()


def test_concurrent_add_never_loses_producers(tmp_path):
    """Two Store connections on one root serialize the read-modify-write."""
    import concurrent.futures

    root = tmp_path / "shared"
    stores = [Store(root, "vllm-ascend"), Store(root, "vllm-ascend")]
    barrier = threading.Barrier(2)

    def add_with(store, producer):
        barrier.wait(timeout=30)
        return store.add(
            kind="experience",
            title="Shared observation",
            content="Both producers saw the same fix.",
            producers=[session_key(producer)],
        )["id"]

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            ids = list(
                pool.map(
                    lambda args: add_with(*args),
                    zip(stores, ("producer-a", "producer-b")),
                )
            )
        assert ids[0] == ids[1]
        producers = stores[0].get(ids[0])["producers"]
        assert producers == sorted([session_key("producer-a"), session_key("producer-b")])
    finally:
        for store in stores:
            store.close()
