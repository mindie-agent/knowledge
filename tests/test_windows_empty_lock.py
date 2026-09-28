"""A first-use lock must contend even before its diagnostic file has data."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows byte-range locking")


def test_empty_locked_file_waits_without_writing_outside_lock(tmp_path):
    from mindie_knowledge.consent_store import _UpdateLock

    path = tmp_path / "first-use.lock"
    child = r'''
import sys
from mindie_knowledge.consent_store import _UpdateLock, ConsentError
try:
    with _UpdateLock(sys.argv[1], wait=0.1):
        raise AssertionError("acquired a held lock")
except ConsentError as exc:
    assert exc.state == "locked"
    print("contended", flush=True)
'''
    # An OS lock can protect bytes beyond EOF. Hold that exact first-use
    # state; the child must report contention, never fail in an early write.
    import msvcrt
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        result = subprocess.run([sys.executable, "-c", child, str(path)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "contended"
        assert path.stat().st_size == 0
    finally:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
    with _UpdateLock(path, wait=0.1):
        assert path.stat().st_size == 0
