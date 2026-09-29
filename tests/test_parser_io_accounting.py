"""Separate actual parser I/O from logical consumption.

The production parsers read one fixed tail increment after 8 KiB, 64 KiB,
and 256 KiB of earlier bytes. ``io_bytes`` counts what the parser's own
file reads returned. ``logical_bytes`` is end-start. Host startup and model
time are not measured and are recorded as null, not as zero.
"""

import json
import time

import pytest

from lane_support import (
    PARSER_NAMES,
    REPO,
    SESSIONS,
    MissingParserCheckout,
    encode_header,
    encode_record,
    file_sha256,
    isolation_root,
    load_parser,
    parser_path,
    transcript_path,
)

BUNDLE = REPO / "tests" / "fixtures" / "production-parsers"

TOKEN = "IOCTRL tail increment"


def _measure(parser, path, start, **kwargs):
    counter = {"io": 0}
    previous = parser.__dict__.get("open")

    def wrapped_open(*args, **open_kwargs):
        handle = open(*args, **open_kwargs)
        original_read = handle.read
        original_readline = handle.readline

        def read(*read_args, **read_kwargs):
            data = original_read(*read_args, **read_kwargs)
            if isinstance(data, (bytes, bytearray)):
                counter["io"] += len(data)
            elif isinstance(data, str):
                counter["io"] += len(data.encode())
            return data

        def readline(*line_args, **line_kwargs):
            data = original_readline(*line_args, **line_kwargs)
            if isinstance(data, (bytes, bytearray)):
                counter["io"] += len(data)
            elif isinstance(data, str):
                counter["io"] += len(data.encode())
            return data

        handle.read = read
        handle.readline = readline
        return handle

    parser.open = wrapped_open
    started = time.perf_counter()
    try:
        result = parser.read_material(str(path), start, **kwargs)
    finally:
        if previous is None:
            parser.__dict__.pop("open", None)
        else:
            parser.open = previous
    wall = time.perf_counter() - started
    logical = int(result["end"]) - int(start)
    return result, counter["io"], logical, wall


def _build(parser_name, root, prefix_len, *, fork=False):
    session = SESSIONS[parser_name]
    path = transcript_path(parser_name, root, session)
    when = time.time() + 180
    filler = b'{"type":"llm.request","n":0,"pad":"' + (b"p" * 200) + b'"}\n'
    chunks = []
    if parser_name == "codex":
        parent = "task-parent" if fork else None
        chunks.append(encode_header(parser_name, session, when, fork_parent=parent))
    while sum(len(chunk) for chunk in chunks) < prefix_len:
        chunks.append(filler)
    prefix = b"".join(chunks)
    target = encode_record(parser_name, session, TOKEN, when)
    path.write_bytes(prefix + target)
    if parser_name == "kimi" and fork:
        state = path.resolve().parents[2] / "state.json"
        state.write_text(
            json.dumps(
                {"forkedFrom": "ses_parent", "createdAt": int((when - 60) * 1000)}
            )
        )
    return path, len(prefix), when, session


def _record(report):
    destination = isolation_root() / "parser-io.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a") as handle:
        handle.write(json.dumps(report, sort_keys=True) + "\n")


@pytest.mark.parametrize("parser_name", PARSER_NAMES)
@pytest.mark.parametrize("prefix_len", [8 * 1024, 64 * 1024, 256 * 1024])
def test_fixed_tail_io_does_not_track_history(tmp_path, parser_name, prefix_len):
    parser = load_parser(parser_name)
    path, start, when, session = _build(parser_name, tmp_path, prefix_len)
    result, io_bytes, logical, wall = _measure(
        parser,
        path,
        start,
        session_id=session,
        not_before=when - 30,
    )
    report = {
        "parser": parser_name,
        "parser_file": str(parser_path(parser_name)),
        "history_prefix_bytes": start,
        "file_bytes": path.stat().st_size,
        "io_bytes": io_bytes,
        "logical_bytes": logical,
        "parsed_records": result.get("records"),
        "wall_seconds": round(wall, 6),
        "model_calls": 0,
        "host_startup_seconds": None,
    }
    _record(report)
    assert result["status"] == "ok", result
    assert result["records"] == 1
    assert TOKEN in result["text"]
    assert 0 < logical < 8192
    assert io_bytes > logical, report
    assert io_bytes < 48 * 1024, report
    assert io_bytes < start, report
    assert report["host_startup_seconds"] is None
    assert report["model_calls"] == 0


@pytest.mark.parametrize("parser_name", ("kimi", "codex"))
def test_fork_identity_read_keeps_the_tail_increment_bounded(tmp_path, parser_name):
    parser = load_parser(parser_name)
    path, start, when, session = _build(
        parser_name, tmp_path, 256 * 1024, fork=True
    )
    result, io_bytes, logical, wall = _measure(
        parser,
        path,
        start,
        session_id=session,
        not_before=when - 30,
    )
    report = {
        "parser": parser_name,
        "case": "fork-tail",
        "history_prefix_bytes": start,
        "io_bytes": io_bytes,
        "logical_bytes": logical,
        "parsed_records": result.get("records"),
        "wall_seconds": round(wall, 6),
        "model_calls": 0,
        "host_startup_seconds": None,
    }
    _record(report)
    assert result["status"] == "ok", result
    assert TOKEN in result["text"]
    assert io_bytes > logical, report
    assert io_bytes < 48 * 1024, report
    assert report["model_calls"] == 0


_DECLARED_PARSERS = {
    "kimi": (
        "299ad86dea2a85cc7cb6fc4b93a62e813cd59647",
        "scripts/transcript.py",
        "4086914f6aa5452ef1a59606b0be62126404c1248133247a0db9d0844cca7aae",
    ),
    "cc": (
        "71cfccaa8f4dd1b7dcdfdd51fae9ec1b20a569e1",
        "scripts/transcript.py",
        "c719bea6957999d08a937e502c764d1b84aa2577ce4f84e6ce78d2a686ec8a9b",
    ),
    "codex": (
        "ebebe7d4536909568535a53f1e0557c93c145835",
        "plugins/mindie-agent/scripts/codex_transcript.py",
        "48d4663512f472d69114797ab7372f34421541122ff161735bad6ed421fddcb6",
    ),
}


def test_committed_parser_fixtures_match_provenance():
    """The bundle is the git-show bytes named in provenance, not a double."""
    provenance = json.loads((BUNDLE / "provenance.json").read_text())
    assert set(provenance["parsers"]) == set(_DECLARED_PARSERS)
    for name, (commit, source_path, digest) in _DECLARED_PARSERS.items():
        recorded = provenance["parsers"][name]
        fixture = BUNDLE / recorded["fixture"]
        assert recorded["commit"] == commit
        assert recorded["source_path"] == source_path
        assert recorded["sha256"] == digest
        assert fixture.is_file(), name
        assert file_sha256(fixture) == digest


def test_explicit_parser_override_beats_framework_source(tmp_path, monkeypatch):
    provenance = json.loads((BUNDLE / "provenance.json").read_text())
    kimi = (BUNDLE / provenance["parsers"]["kimi"]["fixture"]).resolve()
    monkeypatch.setenv("MINDIE_FRAMEWORK_SOURCE", str(tmp_path))
    monkeypatch.setenv("MINDIE_PARSER_KIMI", str(kimi))
    assert parser_path("kimi").resolve() == kimi
    with pytest.raises(MissingParserCheckout, match="is not a file"):
        parser_path("cc")


def test_missing_or_invalid_parser_fixture_is_an_error(tmp_path, monkeypatch):
    for name in ("MINDIE_FRAMEWORK_SOURCE", "MINDIE_PARSER_KIMI", "MINDIE_PARSER_CC", "MINDIE_PARSER_CODEX"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingParserCheckout, match="MINDIE_PARSER_KIMI"):
        parser_path("kimi")
    missing = tmp_path / "missing-transcript.py"
    monkeypatch.setenv("MINDIE_PARSER_KIMI", str(missing))
    with pytest.raises(MissingParserCheckout, match="is not a file"):
        parser_path("kimi")
    empty = tmp_path / "empty-source"
    empty.mkdir()
    monkeypatch.delenv("MINDIE_PARSER_KIMI")
    monkeypatch.setenv("MINDIE_FRAMEWORK_SOURCE", str(empty))
    with pytest.raises(MissingParserCheckout, match="is not a file"):
        parser_path("kimi")
    bad = tmp_path / "not-a-parser.py"
    bad.write_text("x = 1\n")
    monkeypatch.setenv("MINDIE_PARSER_KIMI", str(bad))
    with pytest.raises(RuntimeError, match="does not export"):
        load_parser("kimi")


def test_kimi_fork_boundary_drops_parent_material(tmp_path):
    parser = load_parser("kimi")
    session = SESSIONS["kimi"]
    path = transcript_path("kimi", tmp_path, session)
    when = time.time() + 180
    path.write_bytes(
        encode_record("kimi", session, "FORKOLD parent material", when - 3600)
        + encode_record("kimi", session, "FORKNEW child material", when)
    )
    state = path.resolve().parents[2] / "state.json"
    state.write_text(
        json.dumps({"forkedFrom": "ses_parent", "createdAt": int((when - 60) * 1000)})
    )
    result = parser.read_material(str(path), 0, session_id=session)
    assert result["status"] == "ok", result
    assert "FORKNEW" in result["text"]
    assert "FORKOLD" not in result["text"]
