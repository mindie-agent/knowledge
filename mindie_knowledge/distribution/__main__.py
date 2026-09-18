"""``python -m mindie_knowledge.distribution`` — own entry for build/release/sync.

The root ``mindie-knowledge`` CLI is wired by the main agent at integration
time; this module entry is self-contained and prints one JSON result per
invocation. Exit codes: 0 ok (including ``unchanged``), 1 failure, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mindie_knowledge.distribution.build import build_pack
from mindie_knowledge.distribution.client import embedding_info_from_health
from mindie_knowledge.distribution.errors import DistributionError
from mindie_knowledge.distribution.manifest import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from mindie_knowledge.distribution.release import make_release, publish_release, source_from_location
from mindie_knowledge.distribution.sync import check_and_sync, current_shared
from mindie_knowledge.distribution.pack import inspect_pack, verify_pack
from mindie_knowledge.distribution.manifest import ExpectedContract


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_current(args: argparse.Namespace) -> int:
    _print({"state_root": args.state_root, "current": current_shared(Path(args.state_root))})
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    metrics_reader = None
    if args.embedding_health_url:
        info, metrics_reader = embedding_info_from_health(args.embedding_health_url)
    else:
        info = {"model": args.embedding_model, "dimension": args.embedding_dimension}
    result = check_and_sync(
        Path(args.state_root),
        source_from_location(args.source, cache_dir=Path(args.state_root) / "release-downloads"),
        embedding_info=info,
        openviking_url=args.openviking_url,
        api_key=args.api_key,
        metrics_reader=metrics_reader,
        model_cache=Path(args.model_cache) if args.model_cache else None,
        smoke_query=args.smoke_query,
        verify=args.verify,
    )
    _print(result.to_dict())
    return 0 if result.ok else 1


def _cmd_build(args: argparse.Namespace) -> int:
    metrics_reader = None
    if args.embedding_health_url:
        _info, metrics_reader = embedding_info_from_health(args.embedding_health_url)
    from mindie_knowledge.distribution.client import connect_client

    client = connect_client(args.openviking_url, api_key=args.api_key)
    try:
        result = build_pack(
            repo=Path(args.repo),
            out_dir=Path(args.out),
            client=client,
            expected_sha=args.sha,
            corpus_subdir=args.corpus_subdir,
            name=args.name,
            model_cache=Path(args.model_cache) if args.model_cache else None,
            metrics_reader=metrics_reader,
        )
    finally:
        client.close()
    _print(
        {
            "pack": str(result.pack_path),
            "manifest": str(result.manifest_path),
            "documents": result.documents,
            "details": result.details,
        }
    )
    return 0


def _cmd_make_release(args: argparse.Namespace) -> int:
    out = make_release(
        pack_path=Path(args.pack),
        build_manifest=Path(args.manifest),
        out_dir=Path(args.out),
    )
    _print({"release_dir": str(out)})
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    _print(publish_release(Path(args.release), repository=args.repository))
    return 0


def _cmd_build_release(args: argparse.Namespace) -> int:
    """CI entry: own one temporary CPU instance, build and validate both assets."""
    import tempfile
    from mindie_knowledge.local.instance import LocalInstance
    from mindie_knowledge.distribution.client import connect_client
    from mindie_knowledge.corpus_check import validate_corpus

    checked = validate_corpus(Path(args.repo))
    if not checked["ok"]:
        _print(checked)
        return 1

    with tempfile.TemporaryDirectory(prefix="vaws-corpus-build-") as temporary:
        instance = LocalInstance(Path(temporary))
        if args.model_cache:
            instance.source_cache = Path(args.model_cache)
        try:
            status = instance.ensure()
            client = connect_client(status["openviking_url"], api_key=instance.data_key())
            try:
                built = build_pack(
                    repo=Path(args.repo), out_dir=Path(temporary) / "build", client=client,
                    expected_sha=args.sha, corpus_subdir="corpus", model_cache=instance.cache_dir,
                )
                directory = make_release(pack_path=built.pack_path, build_manifest=built.manifest_path,
                                         out_dir=Path(args.out))
            finally:
                client.close()
        finally:
            instance.stop()
    _print({"status": "built", "release_dir": str(directory)})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    source = source_from_location(args.release)
    snapshot = source.fetch()
    info = inspect_pack(snapshot.pack_path)
    verified = verify_pack(snapshot.pack_path, snapshot.manifest, expected=ExpectedContract())
    _print(
        {
            "ok": True,
            "version_id": snapshot.manifest.version_id,
            "source_git_sha": snapshot.manifest.source_git_sha,
            "root_name": info.root_name,
            "dense_records": verified.dense.get("count"),
            "documents": len(snapshot.manifest.content_files),
        }
    )
    return 0


def _cmd_pins(_args: argparse.Namespace) -> int:
    from mindie_knowledge.distribution.manifest import (
        OPENVIKING_SDK_VERSION,
        OPENVIKING_VERSION,
        SHARED_PARENT_URI,
    )

    _print(
        {
            "openviking": OPENVIKING_VERSION,
            "openviking_sdk": OPENVIKING_SDK_VERSION,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimension": EMBEDDING_DIMENSION,
            "embedding_provider": "openai",
            "shared_parent_uri": SHARED_PARENT_URI,
            "vector_mode": "require",
        }
    )
    return 0


def _cmd_tenant_key(args: argparse.Namespace) -> int:
    import os

    from mindie_knowledge.distribution.client import provision_tenant_key

    root_key = os.environ.get(args.root_key_env)
    if not root_key:
        raise DistributionError(
            f"environment variable {args.root_key_env} is not set; it must carry the "
            "instance root key (the root key never goes on the command line)"
        )
    key = provision_tenant_key(
        args.openviking_url,
        root_key=root_key,
        account_id=args.account,
        user_id=args.user_id,
    )
    print(key)  # bare value so CI can do OV_DATA_KEY=$(...)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mindie_knowledge.distribution",
        description="OVPack build, release adaptation and local shared-version sync.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("current", help="print the active shared version pointer")
    p.add_argument("--state-root", required=True)
    p.set_defaults(func=_cmd_current)

    p = sub.add_parser("check", help="one periodic sync check against a release source")
    p.add_argument("--state-root", required=True)
    p.add_argument("--source", required=True, help="local release directory or github://owner/repository")
    p.add_argument("--openviking-url", default=None)
    p.add_argument("--api-key", default=None)
    p.add_argument("--embedding-health-url", default=None)
    p.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    p.add_argument("--embedding-dimension", type=int, default=EMBEDDING_DIMENSION)
    p.add_argument("--model-cache", default=None)
    p.add_argument("--smoke-query", default=None)
    p.add_argument("--verify", action="store_true", help="audit and repair the active content and dense vectors even without a new release")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("build", help="build a dense OVPack from a fixed Git commit")
    p.add_argument("--repo", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--sha", default=None)
    p.add_argument("--corpus-subdir", default=".")
    p.add_argument("--name", default="corpus")
    p.add_argument("--openviking-url", required=True)
    p.add_argument("--api-key", default=None)
    p.add_argument("--embedding-health-url", default=None)
    p.add_argument("--model-cache", default=None)
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("make-release", help="assemble a local release directory")
    p.add_argument("--pack", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_make_release)

    p = sub.add_parser("build-release", help="build a complete release with a temporary local CPU instance")
    p.add_argument("--repo", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model-cache")
    p.set_defaults(func=_cmd_build_release)

    p = sub.add_parser("publish", help="publish a validated release directory to GitHub")
    p.add_argument("--release", required=True)
    p.add_argument("--repository", required=True)
    p.set_defaults(func=_cmd_publish)

    p = sub.add_parser("verify", help="offline-verify a release directory")
    p.add_argument("--release", required=True)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("pins", help="print the pinned build/sync contract as JSON")
    p.set_defaults(func=_cmd_pins)

    p = sub.add_parser("tenant-key", help="provision the tenant data key (prints it on stdout)")
    p.add_argument("--openviking-url", required=True)
    p.add_argument("--root-key-env", default="OV_ROOT_KEY")
    p.add_argument("--account", default="default")
    p.add_argument("--user-id", default="knowledge-data")
    p.set_defaults(func=_cmd_tenant_key)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DistributionError as exc:
        print(json.dumps({"ok": False, "reason": exc.reason}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
