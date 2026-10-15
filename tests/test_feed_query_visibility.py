"""A failed configured source must not become a successful empty search."""
from contextlib import closing
import threading

import pytest

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.feed import Feed
from mindie_knowledge.loop.store import Store
from mindie_knowledge.loop.transport import FeedUnavailableError, Service, rpc


@pytest.fixture
def configured_service(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "data", "test")) as store:
        feed = Feed(store, dict(repository="org/knowledge", domain="test"))
        service = Service(Engine(store), feeds=[feed])
        monkeypatch.setattr(service, "_identify", lambda args, **_kw: (args, "native-task", None))
        try:
            yield store, feed, service
        finally:
            service.http.server_close()


@pytest.mark.parametrize("status", [None, "unavailable", "invalid"])
def test_initial_source_failure_is_not_an_empty_result(configured_service, monkeypatch, status):
    store, feed, service = configured_service
    if status is not None:
        store.feed_set("feed:" + feed.ident, dict(status=status, detail="private path and secret"))
    monkeypatch.setattr(store, "query", lambda *_a, **_kw: pytest.fail("must not report an empty corpus"))
    monkeypatch.setattr(feed, "sync", lambda *_a, **_kw: pytest.fail("query must not retry sync"))
    with pytest.raises(RuntimeError, match="configured feed has no verified installed snapshot") as error:
        service.call("query", {"query": "synthetic"})
    assert "private path" not in str(error.value)
    assert service.engine.operations.snapshot()["activity"] == 0


def test_retained_snapshot_is_explicitly_stale(configured_service, monkeypatch):
    store, feed, service = configured_service
    store.feed_set("feed:" + feed.ident, dict(
        status="unavailable", commit="a" * 40, contract_sha256="b" * 64,
        detail="private error must not enter query results"))
    result = {"results": [{"ref": "retained-ref"}], "note": "Reference material."}
    monkeypatch.setattr(store, "query", lambda *_a, **_kw: result)
    actual = service.call("query", {"query": "synthetic"})
    assert actual["results"] == result["results"]
    assert actual["source_status"]["status"] == "stale"
    assert actual["source_status"]["feeds"][0]["retained_commit"] == "a" * 40
    assert "private error" not in str(actual)
    assert "current source" in actual["note"]
    assert "source_status" not in result


def test_rpc_delivers_source_failure_without_reporting_a_product_defect(configured_service, monkeypatch):
    store, feed, service = configured_service
    store.feed_set("feed:" + feed.ident, dict(status="unavailable"))
    monkeypatch.setattr(store, "query", lambda *_a, **_kw: pytest.fail("unavailable feed must block query"))
    monkeypatch.setattr(feed, "sync", lambda *_a, **_kw: pytest.fail("query must not retry sync"))
    monkeypatch.setattr("mindie_knowledge.loop.transport.failure",
                        lambda *_a, **_kw: pytest.fail("known source failure is not a product defect"))
    worker = threading.Thread(target=service.http.serve_forever)
    worker.start()
    try:
        with pytest.raises(FeedUnavailableError, match="configured feed has no verified installed snapshot"):
            rpc(service.connection, "query", {"query": "synthetic"}, timeout=5)
        assert service.engine.operations.snapshot()["activity"] == 0
    finally:
        service.http.shutdown()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_rejected_candidate_does_not_mark_prior_snapshot_fresh(configured_service, monkeypatch):
    store, feed, service = configured_service
    store.feed_set("feed:" + feed.ident, dict(
        status="synced", commit="a" * 40, contract_sha256="b" * 64))
    store.feed_set("feed-candidate:" + feed.ident, dict(status="invalid", commit="c" * 40))
    monkeypatch.setattr(store, "query", lambda *_a, **_kw: {"results": []})
    actual = service.call("query", {"query": "synthetic"})
    assert actual["source_status"]["feeds"][0]["candidate_status"] == "invalid"


def test_successfully_synced_empty_source_remains_a_normal_no_match(configured_service):
    store, feed, service = configured_service
    store.feed_set("feed:" + feed.ident, dict(
        status="synced", commit="a" * 40, contract_sha256="b" * 64, entries=0))
    store.feed_set("feed-candidate:" + feed.ident, dict(status="ok", commit="a" * 40))
    actual = service.call("query", {"query": "synthetic"})
    assert actual["results"] == []
    assert "source_status" not in actual
