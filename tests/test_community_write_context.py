"""Profile write boundary for CommunityWriteContext.

One canonical lock covers enable/disable, extension stamps, and a legacy
adoption. A stale previous= snapshot, a nested one-shot, and a stamp of a
corrupt or managed field are rejected. Generation changes only on write().
"""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from lane_support import REPO, reap

REPOSITORY = "mindie-agent/knowledge-vllm-ascend"


@pytest.fixture
def settings_mod():
    from mindie_knowledge.loop import settings as mod

    if not hasattr(mod, "CommunityWriteContext"):
        pytest.fail("candidate does not publish CommunityWriteContext")
    return mod


def _roots(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _write(settings_mod, path, root, *, enabled=True, **extensions):
    return settings_mod.write(
        path,
        enabled=enabled,
        repository=REPOSITORY,
        project_roots=[root],
        **extensions,
    )


def _pair(tmp_path, scripts):
    procs = []
    try:
        for name, source, args in scripts:
            script = tmp_path / name
            script.write_text(source)
            env = os.environ.copy()
            env["GROK_CORE_REPO"] = str(REPO)
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(script), *args],
                    cwd=tmp_path,
                    env=env,
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=20)
            results.append((proc.returncode, out, err))
        return results
    finally:
        for proc in procs:
            reap(proc, group=True)


def test_stale_previous_is_rejected(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    _write(settings_mod, path, root, fork="lane/fork")
    before = path.read_bytes()
    with pytest.raises(TypeError, match="previous"):
        settings_mod.write(
            path,
            enabled=False,
            repository=REPOSITORY,
            project_roots=[root],
            previous={"enabled": True, "fork": "stale"},
        )
    assert path.read_bytes() == before


def test_unparseable_settings_are_preserved(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    raw = b"{not-json\n"
    path.write_bytes(raw)
    root = _roots(tmp_path)
    with pytest.raises(ValueError):
        _write(settings_mod, path, root, enabled=False)
    assert path.read_bytes() == raw
    with pytest.raises(ValueError):
        settings_mod.update_extensions(path, consent_config=str(tmp_path / "consent.json"))
    assert path.read_bytes() == raw
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        settings_mod.update_extensions(missing, consent_config=str(tmp_path / "consent.json"))
    assert not missing.exists()


def test_stamp_rejects_managed_keys_and_does_not_repair(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    consent = tmp_path / "consent.json"
    _write(settings_mod, path, root, fork="lane/fork")
    original_gen = json.loads(path.read_text())["generation"]
    settings_mod.update_extensions(path, consent_config=str(consent.resolve()))
    stamped = json.loads(path.read_text())
    assert stamped["generation"] == original_gen
    assert stamped["enabled"] is True
    assert stamped["fork"] == "lane/fork"
    assert stamped["consent_config"] == str(consent.resolve())
    kept = path.read_bytes()
    for fields in (
        {"enabled": False},
        {"generation": "replaced"},
        {"schema": "other"},
        {"project_roots": []},
    ):
        with pytest.raises(ValueError):
            settings_mod.update_extensions(path, **fields)
        assert path.read_bytes() == kept
    data = json.loads(kept)
    data["enabled"] = "false"
    path.write_text(json.dumps(data, indent=2) + "\n")
    damaged = path.read_bytes()
    loaded = settings_mod.load(path)
    assert loaded.state == "corrupt"
    assert loaded.capture_block_kind() == "fault"
    with pytest.raises(ValueError):
        settings_mod.update_extensions(path, consent_config=str(consent.resolve()))
    assert path.read_bytes() == damaged
    repaired = _write(settings_mod, path, root, enabled=False)
    document = json.loads(path.read_text())
    assert type(document["enabled"]) is bool and document["enabled"] is False
    assert document["generation"] != original_gen
    assert document["consent_config"] == str(consent.resolve())
    assert document["fork"] == "lane/fork"
    assert repaired.capture_block_kind() == "revoked"


def test_context_methods_require_the_held_context(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    _write(settings_mod, path, root)
    ctx = settings_mod.CommunityWriteContext(path)
    with pytest.raises(RuntimeError):
        ctx.read(path)
    with pytest.raises(RuntimeError):
        ctx.write(path, enabled=False, repository=REPOSITORY, project_roots=[root])
    with pytest.raises(RuntimeError):
        ctx.update_extensions(path, consent_config=str(tmp_path / "consent.json"))
    lock = path.with_name(path.name + ".lock")
    with settings_mod.CommunityWriteContext(path):
        assert lock.is_file()
    assert lock.is_file()


def test_nested_one_shot_is_rejected_while_the_context_is_held(tmp_path, settings_mod):
    """A second acquisition on the same canonical key must fail closed.

    The one-shot helper takes the lock itself, so calling it inside a held
    context is the nested write the contract rejects. Bytes stay put.
    """
    from mindie_knowledge.consent_store import ConsentError

    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    _write(settings_mod, path, root, fork="lane/fork")
    before = path.read_bytes()
    started = time.monotonic()
    with settings_mod.CommunityWriteContext(path):
        with pytest.raises(ConsentError) as caught:
            settings_mod.write(
                path,
                enabled=False,
                repository=REPOSITORY,
                project_roots=[root],
            )
    assert caught.value.state == "locked"
    assert path.read_bytes() == before
    assert time.monotonic() - started < 8


def test_disable_and_stamp_survive_either_order(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    consent = str((tmp_path / "consent.json").resolve())
    _write(settings_mod, path, root, fork="lane/fork")
    generation = json.loads(path.read_text())["generation"]
    _write(settings_mod, path, root, enabled=False)
    disabled_generation = json.loads(path.read_text())["generation"]
    assert disabled_generation != generation
    settings_mod.update_extensions(path, consent_config=consent)
    document = json.loads(path.read_text())
    assert document["enabled"] is False
    assert document["consent_config"] == consent
    assert document["generation"] == disabled_generation
    assert document["fork"] == "lane/fork"
    settings_mod.update_extensions(path, consent_config=consent)
    assert json.loads(path.read_text())["generation"] == disabled_generation
    _write(settings_mod, path, root, enabled=True)
    settings_mod.update_extensions(path, consent_config=consent)
    _write(settings_mod, path, root, enabled=False)
    document = json.loads(path.read_text())
    assert document["enabled"] is False
    assert type(document["enabled"]) is bool
    assert document["consent_config"] == consent
    assert document["generation"] != disabled_generation
    assert document["fork"] == "lane/fork"


def test_cross_process_toggle_cannot_drop_a_stamp(tmp_path, settings_mod):
    path = tmp_path / "community.json"
    root = _roots(tmp_path)
    consent = str((tmp_path / "consent.json").resolve())
    _write(settings_mod, path, root, fork="lane/fork")
    header = textwrap.dedent(
        """\
        import json, sys
        sys.path.insert(0, __import__("os").environ["GROK_CORE_REPO"])
        from mindie_knowledge.loop import settings as settings_mod
        path, root, consent = sys.argv[1:]
        """
    )
    toggle = header + textwrap.dedent(
        """\
        seen = False
        for i in range(20):
            settings_mod.write(
                path, enabled=(i % 2 == 0), repository=REPOSITORY_LITERAL,
                project_roots=[root],
            )
            document = json.loads(open(path).read())
            seen = seen or "consent_config" in document
            if seen and document.get("consent_config") != consent:
                print("STAMP LOST", i)
                sys.exit(3)
            if document.get("fork") != "lane/fork":
                print("FORK LOST", i)
                sys.exit(5)
        """
    ).replace("REPOSITORY_LITERAL", repr(REPOSITORY))
    stamp = header + textwrap.dedent(
        """\
        for i in range(20):
            settings_mod.update_extensions(path, consent_config=consent)
            document = json.loads(open(path).read())
            if document.get("consent_config") != consent:
                print("STAMP NOT DURABLE", i)
                sys.exit(4)
            if not isinstance(document.get("enabled"), bool):
                print("ENABLED TYPE", i)
                sys.exit(6)
        """
    )
    results = _pair(
        tmp_path,
        (
            ("toggle.py", toggle, [str(path), str(root), consent]),
            ("stamp.py", stamp, [str(path), str(root), consent]),
        ),
    )
    for code, out, err in results:
        assert code == 0, (code, out, err)
    document = json.loads(path.read_text())
    assert document["consent_config"] == consent
    assert type(document["enabled"]) is bool
    assert document["fork"] == "lane/fork"
    assert document["schema"] == "mindie-community-config/1"
    assert isinstance(document["generation"], str) and document["generation"]


def test_canonical_lock_migration_keeps_a_racing_disable(tmp_path, settings_mod):
    legacy = tmp_path / "legacy.json"
    canonical = tmp_path / "community.json"
    root = _roots(tmp_path)
    consent = str((tmp_path / "consent.json").resolve())
    _write(settings_mod, legacy, root, fork="lane/fork")
    with settings_mod.CommunityWriteContext(canonical) as ctx:
        current = ctx.read(legacy)
        assert current.raw["enabled"] is True
        ctx.write(
            canonical,
            enabled=False,
            repository=REPOSITORY,
            project_roots=[root],
            fork=current.raw["fork"],
        )
        generation = json.loads(canonical.read_text())["generation"]
        ctx.update_extensions(canonical, consent_config=consent)
        document = json.loads(canonical.read_text())
    assert document["enabled"] is False
    assert document["generation"] == generation
    assert document["consent_config"] == consent
    assert document["fork"] == "lane/fork"
    # Put the authority back on the legacy file and race adoption.
    canonical.unlink()
    _write(settings_mod, legacy, root, enabled=True, fork="lane/fork")
    header = textwrap.dedent(
        """\
        import json, sys
        sys.path.insert(0, __import__("os").environ["GROK_CORE_REPO"])
        from pathlib import Path
        from mindie_knowledge.loop.settings import CommunityWriteContext
        canonical, legacy, root, consent = sys.argv[1:]
        canonical_path, legacy_path = Path(canonical), Path(legacy)
        """
    )
    migrate = header + textwrap.dedent(
        """\
        seen_disable = False
        for _ in range(16):
            with CommunityWriteContext(canonical_path) as ctx:
                source = canonical_path if canonical_path.is_file() else legacy_path
                current = ctx.read(source)
                enabled = current.raw.get("enabled") is True
                ctx.write(
                    canonical_path, enabled=enabled, repository=REPOSITORY_LITERAL,
                    project_roots=[root], fork="lane/fork",
                )
                ctx.update_extensions(canonical_path, consent_config=consent)
                document = json.loads(canonical_path.read_text())
            if document.get("enabled") is False:
                seen_disable = True
            elif seen_disable:
                print("DISABLE LOST")
                sys.exit(3)
            if document.get("consent_config") != consent:
                print("STAMP MISSING")
                sys.exit(4)
        print("saw-disable" if seen_disable else "no-disable")
        """
    ).replace("REPOSITORY_LITERAL", repr(REPOSITORY))
    toggle = header + textwrap.dedent(
        """\
        for _ in range(16):
            with CommunityWriteContext(canonical_path) as ctx:
                target = canonical_path if canonical_path.is_file() else legacy_path
                ctx.write(
                    target, enabled=False, repository=REPOSITORY_LITERAL,
                    project_roots=[root],
                )
                if target.is_file():
                    document = json.loads(target.read_text())
                    if document.get("enabled") is not False:
                        print("TOGGLE DROPPED")
                        sys.exit(5)
        """
    ).replace("REPOSITORY_LITERAL", repr(REPOSITORY))
    results = _pair(
        tmp_path,
        (
            ("migrate.py", migrate, [str(canonical), str(legacy), str(root), consent]),
            ("toggle.py", toggle, [str(canonical), str(legacy), str(root), consent]),
        ),
    )
    for code, out, err in results:
        assert code == 0, (code, out, err)
    document = json.loads(canonical.read_text())
    assert document["enabled"] is False
    assert type(document["enabled"]) is bool
    assert document["consent_config"] == consent
    assert document["fork"] == "lane/fork"
    assert document["schema"] == "mindie-community-config/1"
