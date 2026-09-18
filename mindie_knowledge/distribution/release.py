"""Assemble, publish and receive complete, Git-bound OVPack releases."""

from __future__ import annotations

import shutil
import json
import os
import tempfile
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mindie_knowledge.distribution.errors import ReleaseError, SourceUnavailable
from mindie_knowledge.distribution.manifest import (
    ExpectedContract,
    ReleaseManifest,
    atomic_write_json,
    read_json,
    sha256_file,
    validate_release_manifest,
)

RELEASE_MANIFEST_NAME = "release.json"


@dataclass
class ReleaseSnapshot:
    """A fetched release: validated manifest plus a local pack file to stage."""

    manifest: ReleaseManifest
    pack_path: Path
    label: str

    @property
    def references_path(self) -> Path | None:
        spec = self.manifest.data.get("references")
        return self.pack_path.parent / spec["file"] if spec else None


class ReleaseSource(Protocol):
    def fetch(self) -> ReleaseSnapshot: ...


class LocalReleaseSource:
    """Reads a release directory produced by :func:`make_release`."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def fetch(self) -> ReleaseSnapshot:
        directory = self.directory
        manifest_path = directory / RELEASE_MANIFEST_NAME
        if not directory.is_dir():
            raise SourceUnavailable(
                f"release directory {directory} is not reachable; when pointing at a "
                "mirror, check the local path, or retry when back online"
            )
        data = read_json(manifest_path)
        if data is None:
            raise SourceUnavailable(
                f"{manifest_path} is missing or not valid JSON; the release is incomplete"
            )
        manifest = validate_release_manifest(data, expected=ExpectedContract())
        pack_path = directory / manifest.pack["file"]
        if not pack_path.is_file():
            raise SourceUnavailable(
                f"release asset {pack_path.name} is missing from {directory}"
            )
        if manifest.data.get("references") and not (directory / manifest.data["references"]["file"]).is_file():
            raise SourceUnavailable("release reference metadata asset is missing")
        return ReleaseSnapshot(manifest=manifest, pack_path=pack_path, label=str(directory))


def source_from_location(location: Any, *, cache_dir: Path | None = None) -> ReleaseSource:
    """Accept a local directory or github://owner/repository."""

    if isinstance(location, LocalReleaseSource):
        return location
    text = str(location)
    if text.startswith("github://"):
        return GitHubReleaseSource(text.removeprefix("github://"), cache_dir=cache_dir)
    if text.startswith(("http://", "https://")):
        raise SourceUnavailable(
            "arbitrary HTTP release sources are disabled; use github://owner/repository"
        )
    return LocalReleaseSource(Path(text))


def make_release(
    *,
    pack_path: Path,
    build_manifest: Path | dict[str, Any],
    out_dir: Path,
    expected: ExpectedContract | None = None,
) -> Path:
    """Assemble a local release directory from a build result.

    The build manifest is re-validated against the pinned contract before
    anything is published, and the pack copy is re-hashed, so a release
    directory is always self-consistent.
    """

    pack_path = Path(pack_path)
    out_dir = Path(out_dir)
    if isinstance(build_manifest, Path):
        data = read_json(build_manifest)
        if data is None:
            raise ReleaseError(f"build manifest {build_manifest} is missing or invalid JSON")
    else:
        data = dict(build_manifest)
    manifest = validate_release_manifest(data, expected=expected or ExpectedContract())
    if not pack_path.is_file():
        raise ReleaseError(f"pack {pack_path} does not exist")
    if sha256_file(pack_path) != manifest.pack["sha256"]:
        raise ReleaseError(
            f"pack {pack_path} sha256 does not match the build manifest; "
            "rebuild instead of mixing artifacts"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / manifest.pack["file"]
    if pack_path.resolve() != target.resolve():
        shutil.copyfile(pack_path, target)
    if sha256_file(target) != manifest.pack["sha256"]:
        raise ReleaseError(f"copied pack {target} failed its checksum; remove and retry")
    if manifest.data.get("references"):
        from mindie_knowledge.distribution.references import verify_references
        source = pack_path.parent / manifest.data["references"]["file"]
        verify_references(source, manifest, pack_path=pack_path)
        reference_target = out_dir / source.name
        if source.resolve() != reference_target.resolve():
            shutil.copyfile(source, reference_target)
        verify_references(reference_target, manifest, pack_path=target)
    atomic_write_json(out_dir / RELEASE_MANIFEST_NAME, manifest.data)
    return out_dir


def publish_release(directory: Path, *, repository: str) -> dict[str, Any]:
    """Upload both assets as a draft, then publish. Never overwrite a release."""
    from mindie_knowledge.github_transport import api, gh, repository_name
    from mindie_knowledge.distribution.pack import verify_pack

    repository = repository_name(repository)
    snapshot = LocalReleaseSource(directory).fetch()
    verify_pack(snapshot.pack_path, snapshot.manifest, expected=ExpectedContract())
    if snapshot.references_path is not None:
        from mindie_knowledge.distribution.references import verify_references
        verify_references(snapshot.references_path, snapshot.manifest, pack_path=snapshot.pack_path)
    sha = snapshot.manifest.source_git_sha
    tag = f"knowledge-{snapshot.manifest.version_id}"
    tag_ref = gh(["api", f"repos/{repository}/git/ref/tags/{tag}"], check=False)
    if tag_ref.returncode:
        gh(["api", "--method", "POST", f"repos/{repository}/git/refs",
            "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}"])
    elif json.loads(tag_ref.stdout).get("object", {}).get("sha") != sha:
        raise ReleaseError("release tag does not identify the corpus Git commit")
    existing = gh(["release", "view", tag, "--repo", repository,
                   "--json", "isDraft,url,tagName"], check=False)
    if existing.returncode == 0:
        release = json.loads(existing.stdout)
        actual = api(f"repos/{repository}/git/ref/tags/{tag}")
        if actual.get("object", {}).get("sha") != sha:
            raise ReleaseError("release tag does not identify the corpus Git commit")
        if not release["isDraft"]:
            # A retry after publication verifies the remote assets, not just a tag.
            with tempfile.TemporaryDirectory(prefix="knowledge-release-") as temporary:
                remote = GitHubReleaseSource(repository, cache_dir=Path(temporary), tag=tag).fetch()
                if remote.manifest.data != snapshot.manifest.data:
                    raise ReleaseError("published release differs; published assets are immutable")
            return {"status": "unchanged", "url": release["url"], "source_git_sha": sha}
    else:
        gh(["release", "create", tag, "--repo", repository, "--target", sha,
            "--title", f"Knowledge {sha[:12]}", "--notes", f"Reviewed corpus commit: {sha}", "--draft"])
    assets = [str(Path(directory) / RELEASE_MANIFEST_NAME), str(snapshot.pack_path)]
    if snapshot.references_path is not None:
        assets.append(str(snapshot.references_path))
    gh(["release", "upload", tag, *assets, "--repo", repository, "--clobber"], timeout=600)
    # Recheck the tag before making the complete draft visible to clients.
    actual = api(f"repos/{repository}/git/ref/tags/{tag}")
    if actual.get("object", {}).get("sha") != sha:
        raise ReleaseError("release tag changed before publication")
    gh(["release", "edit", tag, "--repo", repository, "--draft=false", "--latest"])
    return {"status": "published", "url": f"https://github.com/{repository}/releases/tag/{tag}",
            "source_git_sha": sha}


class GitHubReleaseSource:
    """Download a public release; cache verified assets and preserve offline state."""

    def __init__(self, repository: str, *, cache_dir: Path | None = None, tag: str | None = None):
        from mindie_knowledge.github_transport import repository_name

        self.repository = repository_name(repository)
        self.tag = tag
        base = Path(os.environ.get("MINDIE_KNOWLEDGE_STATE") or Path.home() / ".cache" / "mindie-knowledge")
        self.cache_dir = Path(cache_dir) if cache_dir else base / "release-downloads"

    def _json(self, suffix: str) -> Any:
        url = f"https://api.github.com/repos/{self.repository}/{suffix}"
        request = urllib.request.Request(url, headers={"User-Agent": "mindie-knowledge", "Accept": "application/vnd.github+json"})
        # Public consumption needs no login. Existing environment credentials may
        # raise the API rate limit, and are sent only to api.github.com.
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise SourceUnavailable("GitHub release metadata is too large")
        return json.loads(raw)

    def cached(self, source_git_sha: str) -> ReleaseSnapshot | None:
        """Find an already downloaded release for an activation we own.

        This never chooses a new version while offline. The caller supplies
        the exact previously activated Git identity and still verifies the
        cached asset before using it.
        """
        directory = self.cache_dir / self.repository.replace("/", "--")
        if not directory.is_dir():
            return None
        for release in sorted(directory.iterdir(), key=lambda path: path.name, reverse=True):
            if not release.is_dir() or not release.name.isdigit():
                continue
            try:
                manifest = validate_release_manifest(read_json(release / RELEASE_MANIFEST_NAME), expected=ExpectedContract())
            except Exception:
                continue
            pack = release / manifest.pack["file"]
            if manifest.source_git_sha == source_git_sha and pack.is_file():
                return ReleaseSnapshot(manifest, pack, "cached GitHub release")
        return None

    def _download(self, asset: dict[str, Any], destination: Path, *, maximum: int) -> None:
        url = str(asset.get("browser_download_url") or "")
        prefix = f"https://github.com/{self.repository}/releases/download/"
        if not url.startswith(prefix):
            raise SourceUnavailable("release asset URL does not belong to the configured repository")
        request = urllib.request.Request(url, headers={"User-Agent": "mindie-knowledge"})
        # No credential header is attached to asset URLs or their CDN redirects.
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > maximum:
                        raise SourceUnavailable("release asset exceeds its declared size")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def fetch(self) -> ReleaseSnapshot:
        try:
            suffix = f"releases/tags/{urllib.parse.quote(self.tag, safe='')}" if self.tag else "releases/latest"
            release = self._json(suffix)
            if release.get("draft") or release.get("prerelease"):
                raise SourceUnavailable("release is not published")
            release_id = int(release["id"])
            directory = self.cache_dir / self.repository.replace("/", "--") / str(release_id)
            directory.mkdir(parents=True, exist_ok=True)
            assets = {item["name"]: item for item in release["assets"] if item.get("state") == "uploaded"}
            self._download(assets[RELEASE_MANIFEST_NAME], directory / RELEASE_MANIFEST_NAME, maximum=4 * 1024 * 1024)
            manifest = validate_release_manifest(read_json(directory / RELEASE_MANIFEST_NAME), expected=ExpectedContract())
            tag = str(release["tag_name"])
            if tag != f"knowledge-{manifest.version_id}":
                raise SourceUnavailable("release tag and manifest version differ")
            ref = self._json(f"git/ref/tags/{urllib.parse.quote(tag, safe='')}")
            if ref.get("object", {}).get("sha") != manifest.source_git_sha:
                raise SourceUnavailable("release tag and corpus Git identity differ")
            pack = directory / manifest.pack["file"]
            size = int(manifest.pack["size"])
            if not 0 < size <= 2 * 1024**3:
                raise SourceUnavailable("release pack is outside the supported size limit")
            if not pack.is_file() or sha256_file(pack) != manifest.pack["sha256"]:
                self._download(assets[pack.name], pack, maximum=size)
            if pack.stat().st_size != size or sha256_file(pack) != manifest.pack["sha256"]:
                raise SourceUnavailable("downloaded release pack failed its integrity check")
            if manifest.data.get("references"):
                spec = manifest.data["references"]
                reference = directory / spec["file"]
                if not reference.is_file() or sha256_file(reference) != spec["sha256"]:
                    self._download(assets[reference.name], reference, maximum=spec["size"])
                from mindie_knowledge.distribution.references import verify_references
                verify_references(reference, manifest, pack_path=pack)
            return ReleaseSnapshot(manifest, pack, str(release.get("html_url") or self.repository))
        except SourceUnavailable:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceUnavailable(f"GitHub release unavailable: {type(exc).__name__}: {exc}") from exc
