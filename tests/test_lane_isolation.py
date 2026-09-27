"""Prove this lane does not use the real home or an enabled diagnostics policy."""

import json
import os
from pathlib import Path

from mindie_diagnostics.fallback import current_consent, policy_path, read_policy

from lane_support import file_sha256, isolation_root, parser_paths


def _snapshot(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "is_symlink": path.is_symlink(),
    }


def test_process_home_and_diagnostics_stay_in_the_test_root():
    root = isolation_root()
    home = Path.home()
    assert root == Path(os.environ["MINDIE_KNOWLEDGE_TEST_ROOT"])
    assert root.is_absolute()
    assert root in home.parents or home == root / "home"
    assert Path(os.environ["HOME"]) == root / "home"
    assert Path(os.environ["XDG_CONFIG_HOME"]) == root / "config"
    assert Path(os.environ["XDG_STATE_HOME"]) == root / "state"
    assert Path(os.environ["MINDIE_DIAGNOSTICS_ROOT"]) == root / "diagnostics"
    config = Path(os.environ["MINDIE_DIAGNOSTICS_CONFIG"])
    assert config == root / "diagnostics.json"
    assert config.is_file() and not config.is_symlink()
    assert policy_path() == config
    policy = read_policy()
    assert policy is not None
    assert policy["decision"] == "disabled"
    assert current_consent() is None


def test_real_home_diagnostics_and_frozen_parsers_are_untouched():
    """Snapshots were taken before HOME was redirected. Product tests must
    not create or rewrite the user's diagnostics files or the source parsers."""
    root = isolation_root()
    before = json.loads((root / "watched-before.json").read_text())
    for path, recorded in before.items():
        assert _snapshot(Path(path)) == recorded, path
    for name, path in parser_paths().items():
        assert path.is_file(), name
        assert file_sha256(path)
        assert str(path) in before
