"""Build path: fixed Git content -> dense OVPack + manifest, via a fake native client."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from distribution.helpers import FakeClient, make_pack

from mindie_knowledge.distribution.build import build_pack, check_markdown_contract
from mindie_knowledge.distribution.errors import BuildError
from mindie_knowledge.distribution.manifest import (
    EMBEDDING_MODEL,
    ExpectedContract,
    validate_release_manifest,
    version_id_from_sha,
)


class BuildFakeClient(FakeClient):
    """Adds a native-shaped export that packs everything written under the URI."""

    def __init__(self, *, wrong_root: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.wrong_root = wrong_root

    def export_ovpack(self, uri: str, to: str, include_vectors: bool = False) -> str:
        self.calls.append(("export_ovpack", {"uri": uri, "include_vectors": include_vectors}))
        docs = {}
        prefix = uri + "/"
        for tree, entries in self.trees.items():
            if tree == uri or tree.startswith(prefix):
                for doc_uri, content in entries.items():
                    if doc_uri.startswith(prefix):
                        docs[doc_uri[len(prefix):]] = content
        root = uri.rsplit("/", 1)[-1] + ("-wrong" if self.wrong_root else "")
        entries = [
            {
                "path": path,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "size": len(content.encode()),
                "text": content,
            }
            for path, content in sorted(docs.items())
        ]
        make_pack(Path(to), entries, root_name=root)
        return to


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "corpus-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    for relpath, text in files.items():
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "corpus")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_build_from_fixed_commit(tmp_path):
    repo, sha = _repo(tmp_path, {"alpha.md": "# Alpha\n\nBody alpha.\n", "notes/beta.md": "# Beta\n\nBody beta.\n"})
    client = BuildFakeClient()
    result = build_pack(repo=repo, out_dir=tmp_path / "out", client=client, expected_sha=sha)
    assert result.documents == 2
    assert result.pack_path.name == f"corpus-{version_id_from_sha(sha)}.ovpack"
    manifest = validate_release_manifest(result.manifest, expected=ExpectedContract())
    assert manifest.source_git_sha == sha
    assert manifest.data["pack"]["vector_mode"] == "require"
    assert manifest.data["embedding"]["model"] == EMBEDDING_MODEL
    assert manifest.data["pack"]["index"]["dense"]["count"] > 0
    write_options = [call[1]["options"] for call in client.calls if call[0] == "write"]
    assert write_options and all(o == {"processing_mode": "vectors_only"} for o in write_options)
    # The pack content is exactly the committed Git content (0.4.19 layout: files/ prefix).
    with zipfile.ZipFile(result.pack_path) as archive:
        root = version_id_from_sha(sha)
        assert archive.read(f"{root}/files/alpha.md").decode() == "# Alpha\n\nBody alpha.\n"
        assert archive.read(f"{root}/files/notes/beta.md").decode() == "# Beta\n\nBody beta.\n"


def test_build_requires_exact_commit(tmp_path):
    repo, sha = _repo(tmp_path, {"a.md": "# A\n\nBody.\n"})
    with pytest.raises(BuildError, match="check out"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient(), expected_sha="b" * 40)


def test_build_requires_clean_tree(tmp_path):
    repo, _sha = _repo(tmp_path, {"a.md": "# A\n\nBody.\n"})
    (repo / "a.md").write_text("# A\n\nEdited.\n", encoding="utf-8")
    with pytest.raises(BuildError, match="uncommitted"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient())


def test_build_requires_git_content(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(BuildError, match="Git worktree"):
        build_pack(repo=plain, out_dir=tmp_path / "out", client=BuildFakeClient())


def test_build_rejects_empty_corpus(tmp_path):
    repo, _sha = _repo(tmp_path, {"notes.txt": "not markdown"})
    with pytest.raises(BuildError, match="no markdown"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient())


def test_build_rejects_bodyless_markdown(tmp_path):
    repo, _sha = _repo(tmp_path, {"a.md": "# Title only\n"})
    with pytest.raises(BuildError, match="non-empty body"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient())


def test_build_surfaces_native_processing_errors(tmp_path):
    repo, _sha = _repo(tmp_path, {"a.md": "# A\n\nBody.\n"})
    client = BuildFakeClient()
    client.wait_processed = lambda timeout=None: {  # type: ignore[assignment]
        "Embedding": {"processed": 1, "requeue_count": 0, "error_count": 2, "errors": ["boom"]}
    }
    with pytest.raises(BuildError, match="Embedding"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=client)


def test_build_checks_native_export_root(tmp_path):
    repo, _sha = _repo(tmp_path, {"a.md": "# A\n\nBody.\n"})
    with pytest.raises(BuildError, match="rooted"):
        build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient(wrong_root=True))


def test_build_pins_model_files(tmp_path):
    repo, _sha = _repo(tmp_path, {"a.md": "# A\n\nBody.\n"})
    cache = tmp_path / "model-cache"
    cache.mkdir()
    (cache / "model.onnx").write_bytes(b"weights")
    (cache / "tokenizer.json").write_bytes(b"{}")
    result = build_pack(repo=repo, out_dir=tmp_path / "out", client=BuildFakeClient(), model_cache=cache)
    model_files = {entry["path"] for entry in result.manifest["embedding"]["model_files"]}
    assert model_files == {"model.onnx", "tokenizer.json"}


def test_model_pins_ignore_download_metadata_and_historical_snapshots(tmp_path):
    from mindie_knowledge.distribution.manifest import hash_model_tree

    caches = [tmp_path / "linux", tmp_path / "macos"]
    revision = "a" * 40
    for cache in caches:
        repo = cache / "models--example--model"
        snapshot = repo / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (snapshot / "model.onnx").write_bytes(b"same-model")
        (snapshot / "tokenizer.json").write_bytes(b"same-tokenizer")
        (repo / "refs").mkdir()
        (repo / "refs/main").write_text(revision)
        (repo / "files_metadata.json").write_text(cache.name)
    old = caches[1] / "models--example--model/snapshots" / ("b" * 40)
    old.mkdir()
    (old / "model.onnx").write_bytes(b"old-unused-model")
    assert hash_model_tree(caches[0]) == hash_model_tree(caches[1])
    assert len(hash_model_tree(caches[0])) == 2
    active = caches[1] / "models--example--model/snapshots" / revision / "model.onnx"
    active.write_bytes(b"changed-model")
    assert hash_model_tree(caches[0]) != hash_model_tree(caches[1])

    # Keeping old files must not let a client serving another revision pass.
    from types import SimpleNamespace
    from mindie_knowledge.distribution.pack import verify_model_files

    manifest = SimpleNamespace(embedding={"model_files": hash_model_tree(caches[0])})
    active.write_bytes(b"same-model")
    assert verify_model_files(caches[1], manifest) == []
    (caches[1] / "models--example--model/refs/main").write_text("b" * 40)
    assert "active embedding model revision" in verify_model_files(caches[1], manifest)[0]


def test_markdown_contract_directly():
    check_markdown_contract("a.md", "# Title\n\nBody.\n")
    check_markdown_contract("a.md", "Title line\n\nBody.\n")
    with pytest.raises(BuildError):
        check_markdown_contract("a.md", "# Title only\n")
    with pytest.raises(BuildError):
        check_markdown_contract("a.md", "")
