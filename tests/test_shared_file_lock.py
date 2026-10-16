"""Real OS lock semantics, independent of service and native model fixtures."""

import os
import errno
from pathlib import Path
import subprocess
import sys

import pytest

from mindie_knowledge.loop.locks import lock_file, try_lock_file, unlock_file


CHILD = """
import os, sys
from mindie_knowledge.loop.locks import try_lock_file
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
if sys.argv[2] == 'bootstrap':
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    acquired = True
else:
    acquired = try_lock_file(fd, exclusive=sys.argv[2] == 'exclusive')
print('held' if acquired else 'busy', flush=True)
if acquired:
    sys.stdin.buffer.read(1)
os.close(fd)
"""


def child(path, mode):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-u", "-c", CHILD, str(path), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def finish(process):
    stdout, stderr = process.communicate(input=b"x", timeout=10)
    assert process.returncode == 0, stderr.decode(errors="replace")
    assert stderr == b""
    return stdout.replace(b"\r\n", b"\n")


def close_child(process):
    if process is not None and process.poll() is None:
        process.kill()
        process.communicate(timeout=10)


def test_two_readers_overlap_and_writer_waits_until_both_release(tmp_path):
    path = tmp_path / "operation.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    other = None
    try:
        assert try_lock_file(fd, exclusive=False)
        other = child(path, "shared")
        assert other.stdout.readline().strip() == b"held"
        writer = child(path, "exclusive")
        assert finish(writer) == b"busy\n"
        unlock_file(fd)
        writer = child(path, "exclusive")
        assert finish(writer) == b"busy\n"
        finish(other)
        assert try_lock_file(fd, exclusive=True)
        unlock_file(fd)
    finally:
        close_child(other)
        os.close(fd)
    assert path.exists()


def test_owner_process_exit_releases_without_unlink(tmp_path):
    path = tmp_path / "operation.lock"
    holder = child(path, "exclusive")
    fd = None
    try:
        assert holder.stdout.readline().strip() == b"held"
        fd = os.open(path, os.O_RDWR)
        assert not try_lock_file(fd, exclusive=True)
        holder.kill()
        holder.communicate(timeout=10)
        assert try_lock_file(fd, exclusive=True)
        unlock_file(fd)
        assert path.exists()
    finally:
        close_child(holder)
        if fd is not None:
            os.close(fd)


def test_invalid_descriptor_is_error_not_busy():
    with pytest.raises((OSError, ValueError)):
        try_lock_file(-1, exclusive=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock contention variants")
def test_posix_access_conflict_is_busy_but_io_error_is_not(monkeypatch):
    import fcntl

    def denied(*_args):
        raise OSError(errno.EACCES, "conflicting lock")
    monkeypatch.setattr(fcntl, "flock", denied)
    assert not try_lock_file(7, exclusive=True)
    def broken(*_args):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(fcntl, "flock", broken)
    with pytest.raises(OSError) as caught:
        try_lock_file(7, exclusive=True)
    assert caught.value.errno == errno.EIO


def test_busy_uses_standard_exception_and_does_not_change_file_offset(tmp_path):
    path = tmp_path / "operation.lock"
    holder = child(path, "exclusive")
    fd = None
    try:
        assert holder.stdout.readline().strip() == b"held"
        fd = os.open(path, os.O_RDWR)
        os.lseek(fd, 9, os.SEEK_SET)
        with pytest.raises(BlockingIOError):
            lock_file(fd, exclusive=False)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 9
    finally:
        close_child(holder)
        if fd is not None:
            os.close(fd)


@pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap byte-range contract")
def test_windows_bootstrap_first_byte_excludes_shared_runtime_lock(tmp_path):
    path = tmp_path / "operation.lock"
    holder = child(path, "bootstrap")
    fd = None
    try:
        assert holder.stdout.readline().strip() == b"held"
        fd = os.open(path, os.O_RDWR)
        assert not try_lock_file(fd, exclusive=False)
        finish(holder)
        assert try_lock_file(fd, exclusive=False)
        unlock_file(fd)
    finally:
        close_child(holder)
        if fd is not None:
            os.close(fd)
