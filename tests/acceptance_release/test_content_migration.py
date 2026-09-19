"""Local, model-free checks for content migration. Isolated fixtures + public corpus."""

from __future__ import annotations

import json
import sqlite3
import shutil
from pathlib import Path

import pytest

from mindie_knowledge.content_migration import apply, main, plan, undo
from mindie_knowledge.loop.store import Store, session_key

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MCP = REPO / "corpus" / "references" / "mcp-stdio-newline-delimited-jsonrpc.md"
PEAKS = REPO / "corpus" / "references" / "ascend910b4-cann-9.0.0-platform-config-peaks.md"


def _roots(tmp_path: Path):
    source = tmp_path / "source"
    store = tmp_path / "store"
    state = tmp_path / "state"
    source.mkdir()
    return source, store, state


def _copy_notes(dest: Path) -> None:
    shutil.copytree(FIXTURES / "notes", dest, dirs_exist_ok=True)


def test_plan_is_dry_and_apply_default_does_not_write(tmp_path):
    source, store_root, state = _roots(tmp_path)
    _copy_notes(source)
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    assert planned["status"] == "planned"
    assert planned["publish"] is False
    assert not (store_root / "vllm-ascend" / "ledger.sqlite3").exists()
    job = planned["job"]
    dry = apply(state, job, commit=False)
    assert dry["apply"]["status"] == "dry-run"
    assert dry["apply"]["written"] == []
    assert not (store_root / "vllm-ascend").exists()
    assert (source / "gate-v1.md").read_text(encoding="utf-8").startswith("# Device gate")


def test_commit_idempotent_undo_and_originals_untouched(tmp_path):
    source, store_root, state = _roots(tmp_path)
    _copy_notes(source)
    original = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    job = planned["job"]
    actions = {item["path"]: item["action"] for item in planned["items"]}
    assert {actions["gate-v1.md"], actions["gate-dup.md"]} == {"add", "duplicate_retain"}
    ids = {item["path"]: item.get("content_id") for item in planned["items"]}
    assert ids["gate-v1.md"] == ids["gate-dup.md"]
    assert actions["gate-v2.md"] == "add"
    assert actions["plain.md"] == "unmappable"
    assert actions["secret.md"] == "skip_private"
    assert actions["candidate.md"] == "skip_private"
    assert actions["observation.md"] == "add"
    committed = apply(state, job, commit=True)
    assert committed["apply"]["status"] == "applied"
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 3  # v1, v2, observation
        again = apply(state, job, commit=True)
        assert again["apply"]["status"] == "unchanged"
        assert store.status()["entries"] == 3
        pub = store.db.execute("SELECT count(*) FROM publication").fetchone()[0]
        assert pub == 0
    finally:
        store.close()
    undone = undo(state, job)
    assert undone["undo"]["status"] == "undone"
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 0
    finally:
        store.close()
    after = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert after == original


def test_source_change_refuses_apply_without_writing(tmp_path):
    source, store_root, state = _roots(tmp_path)
    _copy_notes(source)
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    (source / "gate-v1.md").write_text("# Device gate\n\nchanged\n", encoding="utf-8")
    result = apply(state, planned["job"], commit=True)
    assert result["apply"]["status"] == "refused"
    assert not (store_root / "vllm-ascend" / "ledger.sqlite3").exists()


def test_plan_does_not_upgrade_existing_ledger(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(FIXTURES / "notes" / "observation.md", source / "observation.md")
    shutil.copy(FIXTURES / "notes" / "observation.meta.json", source / "observation.meta.json")
    domain = store_root / "vllm-ascend"
    domain.mkdir(parents=True)
    domain.chmod(0o755)
    ledger = domain / "ledger.sqlite3"
    conn = sqlite3.connect(ledger)
    conn.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, document TEXT NOT NULL)")
    conn.execute("INSERT INTO entries VALUES('00' * 32, '{}')")
    conn.commit()
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    mode_before = domain.stat().st_mode & 0o777
    names_before = {p.name for p in domain.iterdir()}
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    apply(state, planned["job"], commit=False)
    assert domain.stat().st_mode & 0o777 == mode_before
    conn = sqlite3.connect("file:" + ledger.as_posix() + "?mode=ro", uri=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == journal
    conn.close()
    assert {p.name for p in domain.iterdir()} == names_before


def test_partial_commit_is_recoverable_and_does_not_delete(tmp_path, monkeypatch):
    source, store_root, state = _roots(tmp_path)
    _copy_notes(source)
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    job = planned["job"]
    from mindie_knowledge.loop import store as store_mod
    from mindie_knowledge import content_migration as cm

    original_add = store_mod.Store.add
    calls = {"n": 0}

    def flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("injected failure")
        return original_add(self, **kwargs)

    monkeypatch.setattr(store_mod.Store, "add", flaky)
    with pytest.raises(ValueError, match="injected"):
        cm.apply(state, job, commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 1
    finally:
        store.close()
    original = (FIXTURES / "notes" / "gate-v1.md").read_bytes()
    assert (source / "gate-v1.md").read_bytes() == original
    monkeypatch.setattr(store_mod.Store, "add", original_add)
    resumed = apply(state, job, commit=True)
    assert resumed["apply"]["status"] == "applied"
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 3
    finally:
        store.close()


def test_undo_retains_published_mcp_stdio_entry(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(MCP, source / MCP.name)
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    item = next(row for row in planned["items"] if row["path"] == MCP.name)
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        with store.lock, store.db:
            store.db.execute("INSERT OR IGNORE INTO publication VALUES(?)", (item["content_id"],))
        assert store.db.execute("SELECT count(*) FROM publication").fetchone()[0] == 1
    finally:
        store.close()
    result = undo(state, planned["job"])
    assert result["undo"]["status"] == "undo_partial"
    assert result["undo"]["refused"]
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 1
        assert store.db.execute("SELECT count(*) FROM publication").fetchone()[0] == 1
        assert store.get(item["content_id"])["title"]
    finally:
        store.close()
    assert (source / MCP.name).read_bytes() == MCP.read_bytes()


def test_undo_retains_use_feedback_and_producer_changes(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(FIXTURES / "notes" / "observation.md", source / "observation.md")
    shutil.copy(FIXTURES / "notes" / "observation.meta.json", source / "observation.meta.json")
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    item = planned["items"][0]
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        store.use(
            ref=item["content_id"],
            session_id="consumer-b",
            application="Traced capability",
            evidence="Gate rejected before kernel",
        )
        store.add(
            kind="experience",
            title=item["title"],
            content=item["content"],
            producers=[session_key("later-producer")],
        )
    finally:
        store.close()
    result = undo(state, planned["job"])
    assert result["undo"]["status"] == "undo_partial"
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.status()["entries"] == 1
        doc = store.get(item["content_id"])
        assert session_key("later-producer") in doc["producers"]
        assert store.db.execute("SELECT count(*) FROM uses WHERE entry_id=?", (item["content_id"],)).fetchone()[0] == 1
    finally:
        store.close()


def test_public_mcp_stdio_maps_to_knowledge(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(MCP, source / MCP.name)
    planned = plan(
        source_root=source,
        store_root=store_root,
        domain="vllm-ascend",
        state_dir=state,
        origin_repository="mindie-agent/knowledge",
        source_revision="5e1d189a5af8a00c5802ddf4b438345e3e7e5688",
        repo_root=REPO,
    )
    item = next(row for row in planned["items"] if row["path"] == MCP.name)
    assert item["action"] == "add"
    assert item["kind"] == "knowledge"
    assert "modelcontextprotocol.io" in item["source"]["url"]
    assert item["source"]["revision"] == "2025-11-25"
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        doc = store.get(item["content_id"])
        assert doc["kind"] == "knowledge"
        assert doc["source"]["url"].startswith("https://modelcontextprotocol.io")
        hits = store.query("newline-delimited JSON-RPC")["results"]
        assert hits
        wrong = store.query("newline-delimited JSON-RPC", conditions={"cann": "8"})["results"]
        # condition key absent on this note, so it is not filtered by cann
        assert wrong
    finally:
        store.close()


def test_peaks_conditions_and_version_filter(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(PEAKS, source / PEAKS.name)
    planned = plan(
        source_root=source,
        store_root=store_root,
        domain="vllm-ascend",
        state_dir=state,
        origin_repository="mindie-agent/knowledge",
        source_revision="5e1d189a5af8a00c5802ddf4b438345e3e7e5688",
        repo_root=REPO,
    )
    item = planned["items"][0]
    assert item["kind"] == "knowledge"
    assert item["action"] == "add"
    assert "245.76" in item["content"]
    assert item["conditions"]["cann"] == "9.0.0"
    assert item["conditions"]["soc"].startswith("Ascend910B4")
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        applicable = store.query("fp16_dense_matmul_peak", conditions={"cann": "9.0.0"})["results"]
        assert applicable and applicable[0]["kind"] == "knowledge"
        inapplicable = store.query("fp16_dense_matmul_peak", conditions={"cann": "8.0.0"})["results"]
        assert inapplicable == []
    finally:
        store.close()


def test_feed_layout_with_constructed_origin(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copytree(FIXTURES / "feed", source, dirs_exist_ok=True)
    planned = plan(
        source_root=source,
        store_root=store_root,
        domain="vllm-ascend",
        state_dir=state,
        origin_repository="mindie-agent/knowledge",
        source_revision="feedrev",
    )
    by_path = {item["path"]: item for item in planned["items"]}
    assert by_path["maintenance/run.md"]["action"] == "skip_maintenance"
    assert by_path["topics/gate.md"]["kind"] == "knowledge"
    assert by_path["topics/gate.md"]["action"] == "add"
    assert by_path["cases/debug.md"]["kind"] == "experience"
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        kinds = {row["kind"] for row in store.query("Device gate")["results"]}
        assert kinds == {"knowledge", "experience"}
    finally:
        store.close()


def test_cli_help_and_status_roundtrip(tmp_path, capsys):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(FIXTURES / "notes" / "observation.md", source / "observation.md")
    shutil.copy(FIXTURES / "notes" / "observation.meta.json", source / "observation.meta.json")
    assert main([
        "plan",
        "--source-root", str(source),
        "--store-root", str(store_root),
        "--state-dir", str(state),
        "--domain", "vllm-ascend",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    job = payload["job"]
    assert main(["status", "--state-dir", str(state), "--job", job]) == 0
    assert json.loads(capsys.readouterr().out)["job"] == job


def test_consumption_echo_not_a_new_producer(tmp_path):
    store = Store(tmp_path / "store", "vllm-ascend")
    try:
        doc = store.add(
            kind="experience",
            title="Device gate investigation",
            content="Trace the hardware capability before diagnosing kernels. Runtime remains unknown.",
            producers=[session_key("producer-a")],
        )
        store.use(
            ref=doc["id"],
            session_id="consumer-b",
            application="Traced capability",
            evidence="Gate rejected before kernel",
        )
        echoed = store.add(
            kind="experience",
            title=doc["title"],
            content=doc["content"],
            producers=[session_key("consumer-b")],
        )
        assert echoed["id"] == doc["id"]
        assert session_key("consumer-b") not in echoed["producers"]
        assert session_key("producer-a") in echoed["producers"]
    finally:
        store.close()


def test_sidecar_body_condition_conflict_retained(tmp_path):
    source, store_root, state = _roots(tmp_path)
    (source / "note.md").write_text(
        "# Conflict note\n\nBody.\n\n## Conditions\n\n- cann: 8.0.0\n",
        encoding="utf-8",
    )
    (source / "note.meta.json").write_text(
        json.dumps(
            {
                "kind": "knowledge",
                "source": {"url": "https://example.invalid/note", "revision": "1"},
                "conditions": {"cann": "9.0.0"},
            }
        ),
        encoding="utf-8",
    )
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    item = planned["items"][0]
    assert item["action"] == "conflict_retain"
    assert item["conditions"]["cann"] == "9.0.0"
    assert "cann: 8.0.0" in item["content"]
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        doc = store.get(item["content_id"])
        assert doc["conditions"]["cann"] == "9.0.0"
        assert "cann: 8.0.0" in doc["content"]
    finally:
        store.close()


def test_abc_protocol_local_ledger_is_not_effect_success(tmp_path):
    store = Store(tmp_path / "store", "vllm-ascend")
    try:
        doc = store.add(
            kind="experience",
            title="Trace capability first",
            content="Trace the hardware capability before diagnosing kernels.",
            producers=[session_key("producer-a")],
        )
        use = store.use(
            ref=doc["id"],
            session_id="consumer-b",
            application="Traced capability",
            evidence="Gate rejected before kernel",
        )
        store.capture("consumer-b", "turn-1", "Capability trace found the rejection")
        store.judge(
            use["use_id"],
            judge_id="judge-c",
            verdict="unknown",
            reason="No original C artifacts in this local protocol check",
        )
        before = store.query("hardware capability")["results"][0]["score"]
        # A later consumer's helpful vote would change score; that still is not effect success.
        use2 = store.use(
            ref=doc["id"],
            session_id="consumer-d",
            application="Traced again",
            evidence="Same gate",
        )
        store.capture("consumer-d", "turn-1", "Repeated the trace")
        store.judge(
            use2["use_id"],
            judge_id="judge-e",
            verdict="helpful",
            reason="Score signal only",
        )
        after = store.query("hardware capability")["results"][0]["score"]
        assert after >= before
        from tests.acceptance_release.evidence import audit

        result = audit(
            "abc-protocol",
            artifacts={
                "protocol_recorded": True,
                "declared_effect_success": False,
                "store_score_increased": after > before,
            },
        )
        assert result["effect_success"] is False
        assert result["evidence_verdict"] == "supports"
        assert "unknown" in result["labels"]
    finally:
        store.close()


def test_no_hit_query(tmp_path):
    source, store_root, state = _roots(tmp_path)
    shutil.copy(MCP, source / MCP.name)
    planned = plan(source_root=source, store_root=store_root, domain="vllm-ascend", state_dir=state)
    apply(state, planned["job"], commit=True)
    store = Store(store_root, "vllm-ascend")
    try:
        assert store.query("nonexistent_operator_zz991")["results"] == []
    finally:
        store.close()
