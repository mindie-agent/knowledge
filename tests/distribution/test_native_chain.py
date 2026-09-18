"""Real native small-sample chain on this machine (skipped without fixtures).

Exercises the actual OpenViking 0.4.19 servers + the pinned FastEmbed/ONNX
model over loopback: build a dense OVPack from a fixed Git commit, adapt it
into a local release, sync it into an isolated instance, switch versions,
verify modify/delete visibility, private-layer preservation and restart.

Fixtures (never committed paths): set ``MINDIE_DIST_TEST_MODEL_CACHE`` to a
pre-existing FastEmbed cache for the pinned MiniLM model. The OpenViking
server binary defaults to ``.venv/bin/openviking-server`` of this worktree;
override with ``MINDIE_DIST_TEST_OPENVIKING_SERVER``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindie_knowledge.distribution import (  # noqa: E402
    LocalReleaseSource,
    build_pack,
    check_and_sync,
    connect_client,
    current_shared,
    embedding_info_from_health,
    make_release,
)
from mindie_knowledge.distribution.manifest import EMBEDDING_MODEL  # noqa: E402
from mindie_knowledge.distribution.references import prepared_shared_documents  # noqa: E402
from mindie_knowledge.markdown import normalized_sha256  # noqa: E402

MODEL_CACHE = os.environ.get("MINDIE_DIST_TEST_MODEL_CACHE", "")
SERVER_BIN = os.environ.get("MINDIE_DIST_TEST_OPENVIKING_SERVER", "")

WORKTREE = Path(__file__).resolve().parent.parent.parent
DEFAULT_SERVER = WORKTREE / ".venv" / "bin" / "openviking-server"


def _server_binary() -> str | None:
    if SERVER_BIN and Path(SERVER_BIN).is_file():
        return SERVER_BIN
    if DEFAULT_SERVER.is_file():
        return str(DEFAULT_SERVER)
    return shutil.which("openviking-server")


pytestmark = [
    pytest.mark.skipif(
        not (MODEL_CACHE and Path(MODEL_CACHE).is_dir()),
        reason="MINDIE_DIST_TEST_MODEL_CACHE is not set to a local FastEmbed cache",
    ),
    pytest.mark.skipif(_server_binary() is None, reason="openviking-server binary not found"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    raise RuntimeError(f"{url} did not become healthy")


def _clean_proxy_env(env: dict) -> dict:
    """Loopback servers/clients must not go through a SOCKS/HTTP proxy.

    httpx builds its proxy transport at client construction when proxy env is
    present, so merely adding NO_PROXY is not enough — drop the variables for
    this loopback-only chain.
    """

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"
    return env


def _launch(cmd: list[str], log_path: Path, owned: list[subprocess.Popen]) -> subprocess.Popen:
    env = _clean_proxy_env(os.environ.copy())
    env.update(
        {
            "OMP_NUM_THREADS": "4",
            "OPENBLAS_NUM_THREADS": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
    owned.append(proc)
    return proc


def _stop_all(owned: list[subprocess.Popen]) -> None:
    for proc in reversed(owned):
        if proc.poll() is not None:
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            continue
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=10)


def _server_config(state_dir: Path, port: int, embed_port: int, root_key: str) -> dict:
    return {
        "storage": {
            "workspace": str(state_dir),
            "vectordb": {"name": "context", "backend": "local"},
            "agfs": {"backend": "local"},
        },
        "embedding": {
            "dense": {
                "api_base": f"http://127.0.0.1:{embed_port}/v1",
                "api_key": "local-loopback",
                "provider": "openai",
                "dimension": 384,
                "model": EMBEDDING_MODEL,
            }
        },
        "vlm": {
            "api_base": f"http://127.0.0.1:{embed_port}/v1",
            "api_key": "generation-disabled",
            "provider": "openai",
            "model": "generation-disabled",
        },
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "auth_mode": "api_key",
            "root_api_key": root_key,
        },
    }


def _data_client(port: int, root_key: str):
    """Tenant data key via the production provisioning helper (root key administers only)."""

    from mindie_knowledge.distribution import provision_tenant_key

    user_key = provision_tenant_key(
        f"http://127.0.0.1:{port}", root_key=root_key, user_id="distchain"
    )
    client = connect_client(f"http://127.0.0.1:{port}", api_key=user_key)
    return client, user_key


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit_corpus(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "dist-test@example.invalid")
        _git(repo, "config", "user.name", "dist-test")
    for relpath, text in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for missing in set(_git(repo, "ls-files").split()) - set(files):
        _git(repo, "rm", "-q", missing)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "corpus")
    return _git(repo, "rev-parse", "HEAD")


CORPUS_V1 = {
    "zh/graph-launch.md": "# 图模式启动失败排查\n\n紫金色调试探针：图模式下显存预留不足会导致启动卡死，调低预留后恢复。\n",
    "zh/cache-move.md": "# 缓存目录迁移\n\n移动模型缓存后要同时更新环境变量，否则静默回退到默认路径。\n",
    "en/benchmark-warmup.md": "# Benchmark warmup\n\nThe first benchmark run compiles graphs; discard its numbers.\n",
    "en/profiling-gap.md": "# Profiling gap\n\nA missing kernel interval usually means the capture stopped early.\n",
    "ops/restart-note.md": "# Restart note\n\nAfter a restart the service needs the same workspace path to reuse state.\n",
}

CORPUS_V2 = {
    "zh/graph-launch.md": "# 图模式启动失败排查\n\n紫金色调试探针更新：除了显存预留，还要检查编译缓存目录的剩余空间。\n",
    "zh/cache-move.md": "# 缓存目录迁移\n\n移动模型缓存后要同时更新环境变量，否则静默回退到默认路径。\n",
    "en/profiling-gap.md": "# Profiling gap\n\nA missing kernel interval usually means the capture stopped early.\n",
    "ops/restart-note.md": "# Restart note\n\nAfter a restart the service needs the same workspace path to reuse state.\n",
    "ops/disk-budget.md": "# Disk budget\n\n预留空间至少为知识包体积的三倍，给新旧版本切换留余量。\n",
}


def test_native_build_release_sync_chain(tmp_path):
    _clean_proxy_env(os.environ)  # in-process SDK clients are loopback-only too
    owned: list[subprocess.Popen] = []
    embed_port = _free_port()
    source_port = _free_port()
    target_port = _free_port()
    root_key = secrets.token_urlsafe(24)
    logs = tmp_path / "logs"
    try:
        _launch(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "embedding_fixture.py"),
                "--port",
                str(embed_port),
                "--cache-dir",
                MODEL_CACHE,
                "--metrics",
                str(tmp_path / "embedding-metrics.jsonl"),
            ],
            logs / "embedding.log",
            owned,
        )
        _wait_health(f"http://127.0.0.1:{embed_port}/health", timeout=180)
        embedding_info, metrics_reader = embedding_info_from_health(
            f"http://127.0.0.1:{embed_port}/health"
        )
        assert embedding_info["model"] == EMBEDDING_MODEL

        configs = {}
        for name, port, state_dir in (
            ("source", source_port, tmp_path / "source-state"),
            ("target", target_port, tmp_path / "target-state"),
        ):
            config = _server_config(state_dir, port, embed_port, root_key)
            config_path = tmp_path / f"{name}.conf.json"
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            config_path.chmod(0o600)
            configs[name] = config_path
            _launch([_server_binary(), "--config", str(config_path)], logs / f"{name}.log", owned)  # type: ignore[list-item]
            _wait_health(f"http://127.0.0.1:{port}/health")

        source, _source_key = _data_client(source_port, root_key)
        target, target_key = _data_client(target_port, root_key)
        source.wait_processed(timeout=300)
        target.wait_processed(timeout=300)

        # Private layers on the target must survive every shared update.
        target.mkdir("viking://resources/candidate")
        target.write(
            "viking://resources/candidate/local-note.md",
            "# 本地候选\n\n未经审核的本地经验，共享更新不得覆盖。\n",
            wait=True,
            options={"processing_mode": "vectors_only"},
        )
        target.wait_processed(timeout=300)

        # v1: fixed Git content -> dense OVPack -> local release.
        repo = tmp_path / "corpus-repo"
        sha1 = _commit_corpus(repo, {**CORPUS_V1, "zh/graph-launch.meta.json": json.dumps({"retrieval": {
            "source_sha256": normalized_sha256(CORPUS_V1["zh/graph-launch.md"]),
            "aliases": ["ACLGraph reservation incident"], "topics": ["ascend"]}})})
        build1 = build_pack(
            repo=repo,
            out_dir=tmp_path / "build1",
            client=source,
            expected_sha=sha1,
            model_cache=Path(MODEL_CACHE),
            metrics_reader=metrics_reader,
        )
        assert build1.manifest["source"]["git_sha"] == sha1
        assert build1.manifest["embedding"]["model_files"], "model files must be pinned"
        build_texts = build1.details["embedding_calls"].get("texts", 0)
        assert build_texts >= len(CORPUS_V1), "build must embed the documents once"
        release1 = make_release(
            pack_path=build1.pack_path, build_manifest=build1.manifest, out_dir=tmp_path / "release1"
        )

        # Sync into the target: prebuilt vectors, no re-embedding, then switch.
        state_root = tmp_path / "knowledge-state"
        result1 = check_and_sync(
            state_root,
            LocalReleaseSource(release1),
            embedding_info=embedding_info,
            client=target,
            metrics_reader=metrics_reader,
            model_cache=Path(MODEL_CACHE),
            smoke_query="紫金色调试探针",
        )
        assert result1.status == "switched", result1.reason
        assert result1.details["import_embedding_calls"].get("texts", 0) == 0
        assert result1.details["import_embedding_calls"].get("generation_rejected", 0) == 0
        assert result1.details["smoke_query_hits"] > 0
        current1 = current_shared(state_root)
        assert current1 and current1["source_git_sha"] == sha1
        root1 = current1["root_uri"]
        prepared1 = list(prepared_shared_documents(state_root, current=current1))
        graph1 = next(doc for doc in prepared1 if doc.uri.endswith("graph-launch.md"))
        assert graph1.retrieval["aliases"] == ["ACLGraph reservation incident"]
        assert graph1.retrieval["topics"] == ["ascend"]
        from mindie_knowledge.catalog import refresh_catalog, search_catalog
        from mindie_knowledge.server.layers import load_config
        bootstrap = tmp_path / "bootstrap"
        bootstrap.mkdir()
        config = load_config({"state_root": str(state_root), "backend": "memory", "layers": {
            "shared": str(bootstrap), "project": {"enabled": False}, "candidate": {"enabled": False}}}, env={})
        assert refresh_catalog(config, extra_documents=prepared1)["status"] == "ready"
        alias_hits = search_catalog(config, "ACLGraph reservation incident", layers=["shared"],
                                    selection={"topics": ["ascend"], "mode": "only"})
        assert alias_hits.hits and alias_hits.hits[0].uri == graph1.uri

        hits = target.find(
            "图模式启动", target_uri=root1, limit=3, options={"level": 2, "read_content": False}
        )["resources"]
        assert hits and hits[0]["uri"].endswith("graph-launch.md")
        assert "紫金色调试探针" in target.read(hits[0]["uri"])
        assert "未经审核的本地经验" in target.read("viking://resources/candidate/local-note.md")

        # Same release again: quiet no-change.
        assert (
            check_and_sync(
                state_root,
                LocalReleaseSource(release1),
                embedding_info=embedding_info,
                client=target,
            ).status
            == "unchanged"
        )

        # v2: one doc modified, one deleted, one added upstream.
        sha2 = _commit_corpus(repo, {**CORPUS_V2, "zh/graph-launch.meta.json": json.dumps({"retrieval": {
            "source_sha256": normalized_sha256(CORPUS_V2["zh/graph-launch.md"]),
            "aliases": ["compile cache headroom"], "topics": ["ascend"]}})})
        assert sha2 != sha1
        build2 = build_pack(
            repo=repo,
            out_dir=tmp_path / "build2",
            client=source,
            expected_sha=sha2,
            model_cache=Path(MODEL_CACHE),
        )
        release2 = make_release(
            pack_path=build2.pack_path, build_manifest=build2.manifest, out_dir=tmp_path / "release2"
        )
        result2 = check_and_sync(
            state_root,
            LocalReleaseSource(release2),
            embedding_info=embedding_info,
            client=target,
            metrics_reader=metrics_reader,
            model_cache=Path(MODEL_CACHE),
        )
        assert result2.status == "switched", result2.reason
        assert result2.details["import_embedding_calls"].get("texts", 0) == 0
        current2 = current_shared(state_root)
        assert current2 and current2["source_git_sha"] == sha2
        root2 = current2["root_uri"]
        assert root2 != root1
        # A pointer switch never exposes old-version enrichment under the new
        # active source, including the interval before catalog maintenance.
        assert not search_catalog(config, "ACLGraph reservation incident", layers=["shared"]).hits
        prepared2 = list(prepared_shared_documents(state_root, current=current2))
        assert refresh_catalog(config, extra_documents=prepared2)["status"] == "ready"
        alias_hits = search_catalog(config, "compile cache headroom", layers=["shared"],
                                    selection={"topics": ["ascend"], "mode": "only"})
        assert alias_hits.hits and alias_hits.hits[0].uri == root2 + "/zh/graph-launch.md"
        assert all(not doc.uri.endswith("benchmark-warmup.md") for doc in prepared2)

        updated = target.read(f"{root2}/zh/graph-launch.md")
        assert "编译缓存目录" in updated
        with pytest.raises(Exception):
            target.read(f"{root2}/en/benchmark-warmup.md")  # deleted upstream
        assert "三倍" in target.read(f"{root2}/ops/disk-budget.md")
        assert "未经审核的本地经验" in target.read("viking://resources/candidate/local-note.md")

        # Restart the target server: the switched version keeps working.
        target.close()
        source.close()
        _stop_all([proc for proc in owned if proc.args and "target" in str(proc.args[-1])])
        owned[:] = [proc for proc in owned if proc.poll() is None]
        _launch(
            [_server_binary(), "--config", str(configs["target"])],  # type: ignore[list-item]
            logs / "target-restart.log",
            owned,
        )
        _wait_health(f"http://127.0.0.1:{target_port}/health")
        restarted = connect_client(f"http://127.0.0.1:{target_port}", api_key=target_key)
        try:
            restarted.wait_processed(timeout=300)
            hits = restarted.find(
                "编译缓存目录", target_uri=root2, limit=3, options={"level": 2, "read_content": False}
            )["resources"]
            # Ranking across near-duplicate notes is model-dependent; what must
            # survive a restart is retrieval from the active root plus exact reads.
            assert hits and all(hit["uri"].startswith(root2 + "/") for hit in hits)
            assert "编译缓存目录" in restarted.read(f"{root2}/zh/graph-launch.md")
            assert "未经审核的本地经验" in restarted.read(
                "viking://resources/candidate/local-note.md"
            )
        finally:
            restarted.close()
    finally:
        _stop_all(owned)
        assert all(proc.poll() is not None for proc in owned)
