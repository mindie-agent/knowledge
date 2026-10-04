"""Synthetic domain cases for citation-aware, directly readable retrieval.

These are authored fixtures, not real task records or model outputs. They cover
first observations, paraphrases, failed reuse and maintenance without a model.
"""
import hashlib
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
    materials.close()  # cursor and citation relationships survive a reader restart
    while cursor:
        page = materials.search(continuation=cursor, limit=2)[0]
        assert page["ref"] == ref(a)
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


def test_present_source_corruption_is_not_misreported_as_missing_citation(materials):
    a = record(materials, "A", "RMSNorm calibration matched the reference.")
    record(materials, "B", f"RMSNorm calibration readback from {ref(a)}.")
    materials.search("RMSNorm calibration")
    path = materials.root / "tasks" / a["task_id"] / "blocks" / (a["blocks"][0]["block_id"] + ".md")
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        materials.search("RMSNorm calibration")


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
    write_settings(settings, enabled=False, roots=[tmp_path])
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
