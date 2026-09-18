"""Native SDK seams: non-starting readers and exact vector-record checks."""

import hashlib
import sys
from types import SimpleNamespace

import pytest

from mindie_knowledge.local.openviking import MAINTENANCE_TIMEOUT, READ_TIMEOUT, OpenVikingBackend
from mindie_knowledge.markdown import SHARED_BOOTSTRAP_URI


class MissingRecord(RuntimeError):
    code = "NOT_FOUND"


class FakeInstance:
    def __init__(self, root):
        self.state_root = root
        self.live = False
        self.starts = 0

    def describe(self):
        return {"live": self.live, "openviking_url": "http://127.0.0.1:12345"}

    def ensure(self):
        self.starts += 1
        self.live = True
        return self.describe()

    def data_key(self):
        return "test-tenant-key"


@pytest.fixture
def native(tmp_path, monkeypatch):
    instance = FakeInstance(tmp_path)
    records = {}
    vectors = {}
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls = []
            self.results = []
            clients.append(self)

        def initialize(self):
            self.calls.append(("initialize",))

        def close(self):
            self.calls.append(("close",))

        def mkdir(self, uri):
            self.calls.append(("mkdir", uri))

        def read(self, uri):
            self.calls.append(("read", uri))
            if uri not in records:
                raise MissingRecord("content not found")
            return records[uri]

        def stat(self, uri):
            self.calls.append(("stat", uri))
            if len(uri) == 32:
                if uri not in vectors:
                    raise MissingRecord("vector record not found")
                uri = vectors[uri]
            if uri not in records:
                raise MissingRecord("content not found")
            # Native stat(file) synthesizes the id even when the vector row
            # is missing; only stat(id) actually resolves it through VikingDB.
            return {"uri": uri, "id": hashlib.md5(uri.encode()).hexdigest(), "isDir": False}

        def write(self, uri, content, **kwargs):
            self.calls.append(("write", uri))
            records[uri] = content
            vectors[hashlib.md5(uri.encode()).hexdigest()] = uri

        def wait_processed(self, timeout):
            self.calls.append(("wait", timeout))

        def rm(self, uri, **kwargs):
            records.pop(uri, None)
            vectors.pop(hashlib.md5(uri.encode()).hexdigest(), None)

        def find(self, text, **kwargs):
            self.calls.append(("find", text, kwargs))
            return {"resources": self.results}

    monkeypatch.setitem(sys.modules, "openviking_sdk", SimpleNamespace(SyncHTTPClient=Client))
    monkeypatch.setattr("mindie_knowledge.local.openviking.instance_for_config", lambda config: instance)
    backend = OpenVikingBackend(SimpleNamespace(state_root=tmp_path))
    return backend, instance, records, vectors, clients


def test_ready_search_and_read_never_start_a_stopped_engine(native):
    backend, instance, records, vectors, clients = native
    ok, detail = backend.ready()
    assert not ok and "preparation" in detail
    with pytest.raises(RuntimeError, match="not running"):
        backend.search("canary")
    with pytest.raises(RuntimeError, match="not running"):
        backend.read("viking://resources/project/note.md")
    assert instance.starts == 0
    assert clients == []


def test_reader_connects_without_creating_namespaces_or_waiting_for_maintenance(native):
    backend, instance, records, vectors, clients = native
    instance.live = True
    assert backend.ready()[0]
    assert instance.starts == 0
    assert clients[0].kwargs["timeout"] == READ_TIMEOUT
    assert clients[0].calls == [("initialize",)]
    assert backend.available()[0]
    assert len(clients) == 2
    assert clients[1].kwargs["timeout"] == MAINTENANCE_TIMEOUT
    backend.upsert("viking://resources/project/note.md", "# A\n\nbody", layer="project")
    assert backend.read("viking://resources/project/note.md") == "# A\n\nbody"
    assert any(call[0] == "write" for call in clients[1].calls)
    assert not any(call[0] in {"write", "mkdir", "wait"} for call in clients[0].calls)


def test_warm_reader_does_not_repeat_process_ownership_probe(native, monkeypatch):
    backend, instance, records, vectors, clients = native
    instance.live = True
    assert backend.ready()[0]
    def unexpected_probe():
        raise AssertionError("warm query must not inspect process ownership")
    monkeypatch.setattr(instance, "describe", unexpected_probe)
    assert backend.ready()[0]
    assert backend.search("HCCL", layers=["project"]) == []
    assert backend.read("viking://resources/project/missing.md") is None
    assert len(clients) == 1
    assert instance.starts == 0


def test_reader_failure_is_returned_once_and_next_request_reconnects(native):
    backend, instance, records, vectors, clients = native
    instance.live = True
    assert backend.ready()[0]
    failed = clients[0]
    attempts = []
    def offline(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError("engine restarted")
    failed.find = offline
    with pytest.raises(TimeoutError, match="engine restarted"):
        backend.search("ACL Graph", layers=["project"])
    assert len(attempts) == 1
    assert ("close",) in failed.calls
    assert backend.ready()[0]
    assert len(clients) == 2
    assert backend.search("ACL Graph", layers=["project"]) == []
    assert instance.starts == 0


def test_native_content_does_not_prove_its_vector_exists(native):
    backend, instance, records, vectors, clients = native
    uri = "viking://resources/project/note.md"
    content = "# Observation\n\nOnly tested once.\n"
    assert backend.available()[0]
    backend.upsert(uri, content, layer="project")
    assert backend.check_document(uri, content)
    vector_id = hashlib.md5(uri.encode()).hexdigest()
    vectors.pop(vector_id)
    assert records[uri] == content
    assert not backend.check_document(uri, content)
    assert ("stat", vector_id) in clients[0].calls
    backend.upsert(uri, content, layer="project")
    assert backend.check_document(uri, content)
    records[uri] = "Modified indexed body"
    assert not backend.check_document(uri, content)


def test_native_missing_vector_check_never_starts_engine(native):
    backend, instance, records, vectors, clients = native
    with pytest.raises(RuntimeError, match="not running"):
        backend.check_document("viking://resources/project/note.md", "body")
    assert instance.starts == 0


def test_searches_only_bootstrap_active_and_requested_local_roots(native, monkeypatch):
    backend, instance, records, vectors, clients = native
    instance.live = True
    active = "viking://resources/shared/v0123456789ab"
    monkeypatch.setattr("mindie_knowledge.local.shared.current_shared", lambda root: {"root_uri": active})
    assert backend.ready()[0]
    bootstrap_uri = SHARED_BOOTSTRAP_URI + "/bundled.md"
    active_uri = active + "/corpus/new.md"
    stale_uri = "viking://resources/shared/vffffffffffff/corpus/old.md"
    clients[0].results = [
        {"uri": bootstrap_uri, "score": 0.5, "content": "# Bootstrap\n\nKnown context"},
        {"uri": bootstrap_uri, "score": 0.7, "content": "# Bootstrap\n\nKnown context"},
        {"uri": active_uri, "score": 0.6, "content": "# Public\n\nNew context"},
        {"uri": stale_uri, "score": 1.0, "content": "# Stale\n\nOld context"},
        {"uri": "viking://resources/candidate/private.md", "score": 1.0, "content": "private"},
    ]
    hits = backend.search("context", layers=["shared", "project"])
    assert [hit.uri for hit in hits] == [bootstrap_uri, active_uri]
    assert hits[0].score == 0.7
    call = next(call for call in clients[0].calls if call[0] == "find")
    assert call[2]["target_uri"] == [SHARED_BOOTSTRAP_URI, active, "viking://resources/project"]
    assert instance.starts == 0
