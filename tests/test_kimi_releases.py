"""Exercise the published Kimi declaration through the shared Release reader."""

import base64
import hashlib
import json
from pathlib import Path

from mindie_knowledge import releases_io
from mindie_knowledge.releases import select_release


REPO = "mindie-agent/mindie-agent-kimi"
ASSET = Path(__file__).parent / "fixtures/kimi-release-v0.0.1.json"


def test_published_kimi_release_preserves_exact_host_support_and_no_downgrade(monkeypatch):
    # Exact bytes of asset 651787106 on the official v0.0.1 Release.
    body = ASSET.read_bytes()
    download = f"https://github.com/{REPO}/releases/download/v0.0.1/mindie-plugin-release.json"
    row = dict(
        id=416109950, tag_name="v0.0.1", draft=False, prerelease=False,
        url=f"https://api.github.com/repos/{REPO}/releases/416109950",
        assets=[dict(
            name="mindie-plugin-release.json", id=651787106, size=len(body),
            url=f"https://api.github.com/repos/{REPO}/releases/assets/651787106",
            browser_download_url=download,
            digest="sha256:" + hashlib.sha256(body).hexdigest(),
        )],
    )
    monkeypatch.setattr(
        releases_io, "_get",
        lambda url, **kwargs: (body if url == download else json.dumps([row]).encode(), {}),
    )
    catalog = releases_io.fetch_releases(REPO)
    for host in ("2.1.1", "2.2.0", "2.1.2"):
        selected = select_release(
            catalog, repository=REPO, harness_name="kimi-code", harness_version=host,
        )
        assert selected["status"] == (
            "no_compatible_release" if host == "2.1.2" else "candidate"
        )
    selected = select_release(
        catalog, repository=REPO, harness_name="kimi-code", harness_version="2.2.0",
        installed_version="0.0.2",
    )
    assert selected["status"] == "up_to_date"
    assert selected["candidate"] is None


def test_kimi_source_verification_uses_root_declaration(monkeypatch):
    # Source verification is independent of the historical asset (whose source
    # did not yet include a declaration). Simulate a new release's Git objects.
    metadata = json.loads(ASSET.read_text())
    metadata["source"]["sha"] = "b" * 40
    sha = metadata["source"]["sha"]
    body = json.dumps(metadata["compatibility"]).encode()
    api = f"https://api.github.com/repos/{REPO}"
    url = f"{api}/contents/harness-compatibility.json?ref={sha}"
    objects = {
        f"{api}/git/commits/{sha}": dict(sha=sha, url=f"{api}/git/commits/{sha}"),
        url: dict(
            type="file", path="harness-compatibility.json", url=url,
            encoding="base64", size=len(body),
            content=base64.b64encode(body).decode(),
            sha=hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
        ),
    }
    monkeypatch.setattr(
        releases_io, "_get", lambda url, **kwargs: (json.dumps(objects[url]).encode(), {}),
    )
    assert releases_io.verify_release_source(
        metadata, repository=REPO, harness_name="kimi-code",
    ) == metadata
