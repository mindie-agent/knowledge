"""Synthetic domain cases for citation-aware, directly readable retrieval.

These are authored fixtures, not real task records or model outputs. They cover
first observations, paraphrases, failed reuse and maintenance without a model.
"""
import hashlib
import json
import traceback
from pathlib import Path

import pytest

from mindie_knowledge.materials import MaterialStore
from mindie_knowledge.materials.provenance import QueryContinuationError, citations
from mindie_knowledge.materials.references import block_ref, feedback_ref, task_ref


def ident(label):
    return hashlib.sha256(label.encode()).hexdigest()


def record(store, label, body, *, title=None, summary=None):
    task_id = ident(label)
    block_id = ident(label + ":block")
    task = store.append_batch(task_id, [dict(
        block_id=block_id, text=body, source_range=dict(part=0),
        title=title or label, summary=summary or "Synthetic observation.",
    )], "Synthetic task navigation.", title=title or label)
    store.retain_current(task_id, {"draft": task["entry"]["revision"]})
    return task


def ref(task):
    block = task["blocks"][0]
    return block_ref("demo", task["task_id"], block["block_id"], block["sha256"])


@pytest.fixture
def materials(tmp_path):
    value = MaterialStore(tmp_path, "demo")
    yield value
    value.close()


def test_literal_citations_are_full_id_data_not_short_prefix_aliases():
    literal = task_ref("demo", ident("original"))
    fixed = feedback_ref("demo", ident("original"), "f" * 64)
    assert citations(f"<{literal}> [{fixed}]({fixed})") == [literal, fixed]
    assert citations(literal + "0") == []
    assert citations(literal + "/unknown") == []
    assert citations("mindie://demo/0123456789abcdef@" + "f" * 64) == []


def test_paraphrases_group_with_source_but_failed_reuse_keeps_its_own_block(materials):
    original = record(materials, "original", "RMSNorm float16 contiguous kernel matched the CPU reference.")
    readback = record(materials, "readback", f"I read {ref(original)}. RMSNorm float16 contiguous kernel matched; no new run.")
    failure = record(materials, "failed reuse", f"Using {ref(original)}, the RMSNorm float16 contiguous kernel failed on another input.")
    hits = materials.search("RMSNorm float16 contiguous")
    assert len(hits) == 1
    group = hits[0]
    assert group["ref"] == ref(original)
    assert group["task_ref"] == task_ref("demo", original["task_id"])
    assert group["feedback_ref"] == feedback_ref("demo", original["task_id"], original["entry"]["revision"])
    assert group["group_basis"] == "citation"
    related = {item["entry_id"]: item for item in group["related"]}
    assert set(related) == {readback["task_id"], failure["task_id"]}
    assert "failed on another input" in related[failure["task_id"]]["excerpt"]
    assert related[failure["task_id"]]["ref"] == ref(failure)
    assert related[failure["task_id"]]["feedback_ref"] != group["feedback_ref"]


def test_new_query_term_is_not_swallowed_by_a_common_source_term(materials):
    original = record(materials, "original", "RMSNorm float16 contiguous kernel matched the CPU reference.")
    new = record(materials, "new failure", f"Using {ref(original)}, RMSNorm disjoint-stride failed on the new input.")
    hits = materials.search("RMSNorm disjoint-stride")
    found = next(item for item in hits if item["entry_id"] == new["task_id"])
    assert found["ref"] == ref(new)
    assert found["group_basis"] == "task"
    assert found["cites"][0]["current_source_ref"] == ref(original)


def test_fallible_source_metadata_cannot_establish_a_body_match(materials):
    original = record(materials, "original", "Only installation information is present.",
                      title="RMSNorm float16", summary="A misleading fallible index mentions RMSNorm float16.")
    citing = record(materials, "later", f"Reading {ref(original)} led to an RMSNorm float16 question.")
    hits = materials.search("RMSNorm float16")
    assert {hit["entry_id"] for hit in hits} == {original["task_id"], citing["task_id"]}
    indexed = next(item for item in hits if item["entry_id"] == original["task_id"])
    assert indexed["match_basis"] == "index"
    assert "misleading" not in indexed["excerpt"]


def test_historical_task_citation_groups_current_source_without_claiming_old_bytes(materials):
    original = record(materials, "original", "Qwen inference generated 64 tokens on the NPU.")
    old = feedback_ref("demo", original["task_id"], "f" * 64)
    citing = record(materials, "historical readback", f"I read {old}. Qwen inference generated 64 tokens.")
    hit = materials.search("Qwen inference tokens")[0]
    assert hit["entry_id"] == original["task_id"]
    related = next(item for item in hit["related"] if item["entry_id"] == citing["task_id"])
    assert related["cites"] == [dict(cited_ref=old, citation_status="version_unavailable",
                                     source_task_id=original["task_id"], source_domain="demo",
                                     current_source_ref=task_ref("demo", original["task_id"]))]
    assert related["ref"] == ref(citing)
    assert old not in {hit["ref"], related["ref"]}


def test_historical_source_with_different_current_terms_does_not_swallow_new_observation(materials):
    original = record(materials, "original", "Qwen configuration now concerns a different tokenizer.")
    old = feedback_ref("demo", original["task_id"], "f" * 64)
    citing = record(materials, "historical observation", f"I read {old}. Qwen inference generated 64 tokens.")
    hits = materials.search("Qwen inference tokens")
    assert citing["task_id"] in {item["entry_id"] for item in hits}
    assert not any(item["entry_id"] == original["task_id"] and item["related"] for item in hits)


@pytest.mark.parametrize("shape", ["multiple", "missing", "cross-domain", "cycle", "withdrawn-block"])
def test_ambiguous_or_unavailable_lineage_remains_ordinary_matches(materials, shape):
    a_id, b_id = ident("A"), ident("B")
    a_ref, b_ref = task_ref("demo", a_id), task_ref("demo", b_id)
    if shape == "cycle":
        a = record(materials, "A", f"RMSNorm calibration cites {b_ref}.")
        b = record(materials, "B", f"RMSNorm calibration cites {a_ref}.")
    else:
        a = record(materials, "A", "RMSNorm calibration is recorded here.")
        if shape == "multiple":
            other = record(materials, "other", "RMSNorm calibration differs on another platform.")
            citation = f"{ref(a)} and {ref(other)}"
        elif shape == "missing":
            citation = task_ref("demo", ident("missing"))
        elif shape == "cross-domain":
            citation = task_ref("another-domain", a_id)
        else:
            citation = block_ref("demo", a_id, ident("removed-block"), "f" * 64)
        b = record(materials, "B", f"RMSNorm calibration cites {citation}.")
    hits = materials.search("RMSNorm calibration")
    assert a["task_id"] in {item["entry_id"] for item in hits}
    assert b["task_id"] in {item["entry_id"] for item in hits}
    assert all(item["group_basis"] == "task" for item in hits)


def test_single_source_chain_groups_without_counting_citations_as_evidence(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    b = record(materials, "B", f"RMSNorm calibration readback from {ref(a)}.")
    c = record(materials, "C", f"RMSNorm calibration readback from {ref(b)}.")
    hit = materials.search("RMSNorm calibration")[0]
    assert hit["entry_id"] == a["task_id"]
    assert {item["entry_id"] for item in hit["related"]} == {b["task_id"], c["task_id"]}
    assert hit["group_score"] == max([hit["score"], *(item["score"] for item in hit["related"])])


def test_related_cursor_recomputes_pages_and_expires_when_corpus_changes(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    copies = [record(materials, f"copy {number}", f"RMSNorm calibration readback {number} from {ref(a)}.")
              for number in range(7)]
    first = materials.search("RMSNorm calibration", limit=1)[0]
    seen = [item["ref"] for item in first["related"]]
    cursor = first["related_next"]
    first_cursor = cursor
    assert cursor and len(first["related"]) == 2
    assert all("navigation" not in item and "summary" not in item for item in first["related"])
    materials.close()  # cursor and citation relationships survive a reader restart
    while cursor:
        page = materials.search(continuation=cursor, limit=2)[0]
        assert page["ref"] == ref(a)
        assert all("navigation" in item and "summary" in item for item in page["related"])
        seen.extend(item["ref"] for item in page["related"])
        cursor = page["related_next"]
    assert set(seen) == {ref(item) for item in copies}
    assert len(seen) == len(set(seen))
    with pytest.raises(QueryContinuationError, match="continuation_invalid"):
        materials.search("different query", continuation=first_cursor)
    record(materials, "unrelated new task", "New corpus generation.")
    materials.search("RMSNorm calibration")
    with pytest.raises(QueryContinuationError, match="continuation_expired"):
        materials.search(continuation=first_cursor)
    assert materials.index_status()["phase"] == "ready"


def test_warm_query_and_related_pages_do_not_reread_block_bodies(materials, monkeypatch):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    for number in range(4):
        record(materials, f"copy {number}", f"RMSNorm calibration readback {number} from {ref(a)}.")
    first = materials.search("RMSNorm calibration")[0]
    original = Path.read_bytes

    def no_blocks(path, *args, **kwargs):
        if path.parent.name == "blocks":
            raise AssertionError("warm query read an authoritative body")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", no_blocks)
    assert materials.search("RMSNorm calibration")[0]["ref"] == ref(a)
    assert materials.search(continuation=first["related_next"])[0]["related"]


def test_exact_task_and_block_lookups_do_not_redirect_to_a_source(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    b = record(materials, "B", f"RMSNorm calibration readback from {ref(a)}.")
    for query in (ref(b), task_ref("demo", b["task_id"]), b["task_id"]):
        hit = materials.search(query)[0]
        assert hit["ref"] == ref(b)
        assert hit["match_basis"] == "identity"
        assert not hit["related"]


def _entry_ids(results):
    found = []

    def walk(items):
        for item in items:
            if item.get("entry_id"):
                found.append(item["entry_id"])
            walk(item.get("related") or ())

    walk(results)
    return found


def _cites(results):
    found = []

    def walk(items):
        for item in items:
            found.extend(item.get("cites") or ())
            walk(item.get("related") or ())

    walk(results)
    return found


def test_present_source_corruption_is_not_misreported_as_missing_citation(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    b = record(materials, "B", f"RMSNorm calibration readback from {ref(a)}.")
    materials.search("RMSNorm calibration")
    path = materials.root / "tasks" / a["task_id"] / "blocks" / (a["blocks"][0]["block_id"] + ".md")
    original = path.read_bytes()
    path.write_bytes(original + b"corrupt")
    found = materials.search("RMSNorm calibration")
    assert a["task_id"] not in _entry_ids(found)
    assert b["task_id"] in _entry_ids(found)
    fault = found.material_faults[0]
    assert fault["task_id"] == a["task_id"]
    assert fault["reason"] == "block_hash_mismatch"
    assert fault["error_code"] == "material_corrupt"
    cites = [item for item in _cites(found) if item["source_task_id"] == a["task_id"]]
    assert cites
    assert {item["citation_status"] for item in cites} == {"unreadable"}
    assert all(item["citation_status"] != "missing" for item in _cites(found))
    assert cites[0]["current_source_ref"] == ref(a)
    assert path.read_bytes() == original + b"corrupt"
    assert materials.index_status()["phase"] == "failed"
    assert materials._index._chunk_body_terms
    with pytest.raises(ValueError, match="hash mismatch"):
        materials.get_document(a["task_id"])


def test_citation_only_identifiers_do_not_establish_body_match(materials):
    a = record(materials, "A", "Calibration succeeded.")
    b = record(materials, "B", f"Only a citation: {ref(a)}.")
    # Identity lookup selects A itself, never a body merely containing its ID.
    hit = materials.search(a["task_id"])[0]
    assert hit["ref"] == ref(a)
    assert b["task_id"] not in {item["entry_id"] for item in hit["related"]}
    missing = ident("missing")
    record(materials, "missing citation", f"A removed source was {task_ref('demo', missing)}.")
    assert materials.search(missing) == []


def test_header_only_block_cannot_replace_a_real_source_anchor(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    original_ref = ref(a)
    appended = materials.append_batch(a["task_id"], [dict(
        block_id=ident("unrelated block"), text="Only plugin installation was checked here.",
        source_range=dict(part=1), title="RMSNorm calibration", summary="RMSNorm calibration " * 20,
    )], "Fallible navigation", title="A")
    materials.retain_current(a["task_id"], {"draft": appended["entry"]["revision"]})
    record(materials, "B", f"RMSNorm calibration readback from {original_ref}.")
    hit = materials.search("RMSNorm calibration")[0]
    assert hit["ref"] == original_ref
    assert hit["match_basis"] == "body"


def test_store_query_keeps_read_refs_and_feedback_revisions_separate_without_writes(store):
    first = store.create_draft(kind="experience", title="Synthetic calibration", summary="Test fixture.",
                               content="RMSNorm calibration matched the reference.", owner="a" * 64)
    original = store.query("RMSNorm calibration")["results"][0]
    second = store.create_draft(kind="experience", title="Synthetic failed reuse", summary="Test fixture.",
                                content=f"RMSNorm calibration failed after reading {original['ref']}.", owner="b" * 64)
    changes = store.db.total_changes
    hit = store.query("RMSNorm calibration")["results"][0]
    assert store.db.total_changes == changes
    assert hit["entry_id"] == first["entry_id"]
    assert "/blocks/" in hit["ref"] and "/blocks/" not in hit["feedback_ref"]
    related = next(item for item in hit["related"] if item["entry_id"] == second["entry_id"])
    assert related["feedback_ref"] == feedback_ref(store.domain, second["entry_id"], second["revision"])
    assert related["origin"] == "draft"
    assert "failed" in store.explain(related["ref"])["content"]


def test_compact_related_preserves_own_conditions_and_historical_source_status(store):
    original = store.create_draft(
        kind="experience", title="Initial calibration", summary="A fallible first index.",
        content="RMSNorm float16 calibration matched the reference.",
        conditions={"torch_version": "1.0"}, owner="a" * 64,
    )
    old = feedback_ref(store.domain, original["entry_id"], "f" * 64)
    body = f"RMSNorm float16 calibration failed on a different shape after reading {old}."
    correction = store.create_draft(
        kind="experience", title="Different conditions", summary="A fallible later index.",
        content=body, conditions={"torch_version": "2.0"}, owner="b" * 64,
    )
    group = store.query("RMSNorm float16 calibration")["results"][0]
    assert group["entry_id"] == original["entry_id"]
    related = group["related"][0]
    assert related["entry_id"] == correction["entry_id"]
    assert related["conditions"] == {"torch_version": "2.0"}
    assert related["feedback_ref"] == feedback_ref(store.domain, correction["entry_id"], correction["revision"])
    assert related["match_basis"] == "body" and "failed on a different shape" in related["excerpt"]
    assert not {"title", "summary", "navigation", "block_summary", "source_range"}.intersection(related)
    assert related["cites"][0]["cited_ref"] == old
    assert related["cites"][0]["citation_status"] == "version_unavailable"
    assert related["cites"][0]["current_source_ref"] == task_ref(store.domain, original["entry_id"])
    assert store.explain(related["ref"])["content"] == body


def test_query_rpc_distinguishes_cursor_input_from_present_material_failure(store, tmp_path):
    import threading
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.transport import RequestRejected, Service, rpc
    from mindie_knowledge.materials.references import MaterialReadError, parse_read_ref
    from conftest import make_admission, write_settings

    store.create_draft(kind="experience", title="Synthetic calibration", summary="Test fixture.",
                       content="RMSNorm calibration matched the reference.", owner="a" * 64)
    hit = store.query("RMSNorm calibration")["results"][0]
    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", enabled=False, roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    engine = Engine(store, settings_path=settings, admission=admission)
    service = Service(engine, admission=admission)
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    identity = dict(_session_id="manual-A", _session_verified=True)
    try:
        with pytest.raises(RequestRejected) as caught:
            rpc(service.connection, "query", dict(identity, continuation="malformed"))
        assert caught.value.error_code == "continuation_invalid"
        selected = parse_read_ref(hit["ref"])
        path = store.materials.root / "tasks" / selected["task_id"] / "blocks" / (selected["block_id"] + ".md")
        path.write_bytes(path.read_bytes() + b"not the admitted bytes")
        # Invalid caller data is rejected before attempting to inspect bodies.
        with pytest.raises(RequestRejected, match="limit"):
            rpc(service.connection, "query", dict(identity, query="RMSNorm", limit=0))
        with pytest.raises(MaterialReadError) as caught:
            rpc(service.connection, "query", dict(identity, query="RMSNorm"))
        assert caught.value.code == "material_corrupt"
        assert not isinstance(caught.value, RequestRejected)
        with pytest.raises(MaterialReadError) as direct:
            store.query("RMSNorm")
        assert isinstance(direct.value.__cause__, ValueError)
    finally:
        service.http.shutdown()
        service.http.server_close()
        thread.join(timeout=5)


def test_one_corrupt_catalog_header_does_not_hide_the_other_task(materials):
    good = record(materials, "good header", "shared-catalog-token healthy body")
    bad = record(materials, "bad header", "shared-catalog-token damaged body")
    catalog = materials._current_catalog()
    assert catalog._seen == -1
    saved = catalog.db.execute(
        "SELECT header FROM tasks WHERE task_id=?", (bad["task_id"],)).fetchone()[0]
    catalog.db.execute("UPDATE tasks SET header=? WHERE task_id=?", ("{broken", bad["task_id"]))
    catalog.db.commit()
    block = materials.root / "tasks" / bad["task_id"] / "blocks" / (bad["blocks"][0]["block_id"] + ".md")
    before = block.read_bytes()
    found = materials.search("shared-catalog-token")
    assert _entry_ids(found) == [good["task_id"]]
    fault = found.material_faults[0]
    assert fault["task_id"] == bad["task_id"]
    assert fault["reason"] == "manifest_unreadable"
    assert fault["error_code"] == "material_corrupt"
    assert str(materials.root) not in json.dumps(fault)
    assert block.read_bytes() == before
    catalog.db.execute("UPDATE tasks SET header=? WHERE task_id=?", (saved, bad["task_id"]))
    catalog.db.commit()
    restored = materials.search("damaged body")
    assert bad["task_id"] in _entry_ids(restored)
    assert not restored.material_faults


def _open_query_service(store, tmp_path):
    import threading
    from mindie_knowledge.loop.activation import Admission
    from mindie_knowledge.loop.engine import Engine
    from mindie_knowledge.loop.transport import Service
    from conftest import make_admission, write_settings

    settings = tmp_path / "community.json"
    write_settings(settings, consent_choice="contribute", enabled=False, roots=[tmp_path])
    admission = Admission(make_admission(tmp_path, project_root=tmp_path))
    engine = Engine(store, settings_path=settings, admission=admission)
    service = Service(engine, admission=admission)
    thread = threading.Thread(target=service.http.serve_forever, daemon=True)
    thread.start()
    return service, thread


def _shutdown_query_service(service, thread):
    service.http.shutdown()
    service.http.server_close()
    thread.join(timeout=5)


def test_one_unreadable_material_does_not_fail_the_graded_query(store, tmp_path, monkeypatch):
    """AD #312: knowledge.rpc query must not die at _query_materials for one bad package."""
    from graded_fixture import unit
    from mindie_knowledge.loop.transport import rpc
    from mindie_knowledge.materials.references import MaterialReadError, block_ref

    owner = "a" * 64
    good = store.create_draft(kind="experience", title="Healthy", summary="Readable.",
                              content="HealthyMarkerToken stays in the result.", owner=owner)
    bad = store.create_draft(kind="experience", title="Damaged", summary="Unreadable.",
                             content="DamagedMarkerToken must not be served.", owner=owner)
    task = store.materials.read_task(bad["entry_id"])
    block = task["blocks"][0]
    path = store.materials.root / "tasks" / bad["entry_id"] / "blocks" / (block["block_id"] + ".md")
    original = path.read_bytes()
    path.write_bytes(original + b"corrupt")
    isolated = store.query("HealthyMarkerToken")
    assert good["entry_id"] in _entry_ids(isolated["results"])
    assert bad["entry_id"] not in _entry_ids(isolated["results"])
    assert isolated["material_faults"][0]["task_id"] == bad["entry_id"]
    assert isolated["material_faults"][0]["reason"] == "block_hash_mismatch"
    assert "material_faults names each task" in isolated["note"]
    assert path.read_bytes() == original + b"corrupt"
    bad_ref = block_ref(store.domain, bad["entry_id"], block["block_id"], block["sha256"])
    with pytest.raises(MaterialReadError) as explained:
        store.explain(bad_ref)
    assert explained.value.code == "material_corrupt"
    store.graded.install([unit()])
    recorded = []
    monkeypatch.setattr("mindie_knowledge.loop.transport.failure",
                        lambda *args, **kwargs: recorded.append(kwargs))
    service, thread = _open_query_service(store, tmp_path)
    identity = dict(_session_id="manual-A", _session_verified=True)
    try:
        remote = rpc(service.connection, "query", dict(identity, query="HealthyMarkerToken"))
    finally:
        _shutdown_query_service(service, thread)
    assert good["entry_id"] in _entry_ids(remote["results"])
    assert bad["entry_id"] not in _entry_ids(remote["results"])
    assert remote["material_faults"][0]["reason"] == "block_hash_mismatch"
    assert str(store.materials.root) not in json.dumps(remote["material_faults"])
    assert not any(item.get("category") == "material_corrupt" for item in recorded)
    again = store.query("HealthyMarkerToken")
    assert again["material_faults"][0]["task_id"] == bad["entry_id"]
    path.write_bytes(original)
    restored = store.query("DamagedMarkerToken")
    assert bad["entry_id"] in _entry_ids(restored["results"])
    assert "material_faults" not in restored


def test_catalog_wide_pointer_damage_still_fails_query_on_the_reported_stack(store, tmp_path, monkeypatch):
    """The reported do_POST → _query_materials fingerprint stays a hard failure."""
    from graded_fixture import unit
    from mindie_knowledge.loop.transport import rpc
    from mindie_knowledge.materials.references import MaterialReadError

    store.create_draft(kind="experience", title="Preserved", summary="Observation.",
                       content="material canary stays on disk", owner="a" * 64)
    store.graded.install([unit()])
    path = store.materials.root / "current.json"
    path.write_text("{}\n", encoding="utf-8")
    captured = {}

    def grab(*_args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("mindie_knowledge.loop.transport.failure", grab)
    service, thread = _open_query_service(store, tmp_path)
    identity = dict(_session_id="manual-A", _session_verified=True)
    try:
        with pytest.raises(MaterialReadError) as caught:
            rpc(service.connection, "query", dict(identity, query="material"))
    finally:
        _shutdown_query_service(service, thread)
    assert caught.value.code == "material_corrupt"
    assert "do_POST" not in {frame.name for frame in traceback.extract_tb(caught.value.__traceback__)}
    assert path.read_text(encoding="utf-8") == "{}\n"
    server = captured["exception"]
    assert captured["stage"] == "query"
    assert captured["category"] == "material_corrupt"
    assert isinstance(server, MaterialReadError)
    assert isinstance(server.__cause__, ValueError)
    names = [frame.name for frame in traceback.extract_tb(server.__traceback__)]
    cursor = 0
    for function in ("do_POST", "call", "_dispatch", "query", "_query_materials"):
        cursor = names.index(function, cursor) + 1
