"""Owned subprocess helpers: fd hygiene, stderr backpressure, batch reads."""

import os
import sys
import time

import pytest

from mindie_knowledge.gitread import CatFileBatch, iter_file_records, run_stdout_to_file


def test_stderr_backpressure_cannot_fake_a_timeout(tmp_path):
    """A child writing 256 KiB to stderr then a short stdout and exiting 0
    completes normally: stderr goes to a managed scratch file, never blocks
    the pipe."""
    out = tmp_path / "out.bin"
    child = (
        "import sys\n"
        "sys.stderr.write('x' * (256 * 1024))\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write('hello stdout')\n"
        "sys.stdout.flush()\n"
    )
    run_stdout_to_file([sys.executable, "-c", child], out, timeout=10)
    assert out.read_bytes() == b"hello stdout"


def test_run_stdout_to_file_failure_reports_a_bounded_tail(tmp_path):
    out = tmp_path / "out.bin"
    child = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    with pytest.raises(OSError, match="boom"):
        run_stdout_to_file([sys.executable, "-c", child], out, timeout=10)


def test_no_descriptor_leak_across_calls(tmp_path):
    if not os.path.isdir("/dev/fd"):
        pytest.skip("/dev/fd unavailable")
    before = len(os.listdir("/dev/fd"))
    for _ in range(8):
        run_stdout_to_file([sys.executable, "-c", "print('x')"], tmp_path / "o", timeout=10)
    after = len(os.listdir("/dev/fd"))
    assert after <= before


def test_cat_file_batch_reads_and_missing(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.check_output(["git", "-C", str(repo), *a], timeout=10)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.invalid")
    (repo / "f.txt").write_bytes(b"payload\n")
    git("add", ".")
    git("commit", "-qm", "c")
    commit = git("rev-parse", "HEAD").decode().strip()
    reader = CatFileBatch(repo)
    try:
        assert reader.read(f"{commit}:f.txt", deadline=time.monotonic() + 10,
                           max_bytes=1024) == b"payload\n"
        assert reader.read(f"{commit}:missing.txt", deadline=time.monotonic() + 10,
                           max_bytes=1024) is None
    finally:
        reader.close()


def test_iter_file_records_bounded_chunks(tmp_path):
    path = tmp_path / "records.bin"
    records = [b"a" * 100, b"b" * 100, b""]
    path.write_bytes(b"\0".join(records))
    assert list(iter_file_records(path, chunk_size=64)) == records[:-1]
