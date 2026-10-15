"""Public catalog completeness and origin are separate from compatibility."""

from copy import deepcopy
import base64
import hashlib
import io
import json
import urllib.error
import urllib.request

import pytest

from mindie_knowledge import releases_io as reader
from mindie_knowledge.releases import ReleaseValidationError, select_release
from test_releases import REPO, release


def public_release(ident=1, version="0.0.1"):
    record = release(version, ident)
    body = json.dumps(record.pop("metadata")).encode()
    tag = record["tag_name"]
    record.update(
        url=f"https://api.github.com/repos/{REPO}/releases/{ident}",
        assets=[dict(
            name=reader.ASSET_NAME, id=ident, size=len(body),
            url=f"https://api.github.com/repos/{REPO}/releases/assets/{ident}",
            browser_download_url=(
                f"https://github.com/{REPO}/releases/download/{tag}/{reader.ASSET_NAME}"
            ),
            digest="sha256:" + hashlib.sha256(body).hexdigest(),
        )],
    )
    return record, body


def serve(monkeypatch, pages, assets):
    seen = []
    def get(url, **_kwargs):
        seen.append(url)
        if url.startswith("https://api.github.com/"):
            number = int(url.rsplit("=", 1)[1])
            rows, headers = pages[number - 1]
            return json.dumps(rows).encode(), headers
        return assets[url], {}
    monkeypatch.setattr(reader, "_get", get)
    return seen


def assert_error(code, call):
    with pytest.raises(reader.ReleaseFetchError) as caught:
        call()
    assert str(caught.value) == code
    return caught.value


def test_complete_pagination_selects_highest_not_first_page(monkeypatch):
    first, first_body = public_release()
    second, second_body = public_release(2, "0.0.2")
    next_link = f'<https://api.github.com/repos/{REPO}/releases?per_page=100&page=2>; rel="next"'
    seen = serve(monkeypatch, [([first], {"Link": next_link}), ([second], {})], {
        first["assets"][0]["browser_download_url"]: first_body,
        second["assets"][0]["browser_download_url"]: second_body,
    })
    records = reader.fetch_releases(REPO)
    result = select_release(records, repository=REPO, harness_name="codex",
                            harness_version="0.153.4")
    assert result["candidate"]["metadata"]["version"] == "0.0.2"
    assert len(seen) == 4


def test_full_page_without_link_requires_next_read(monkeypatch):
    rows = [dict(id=i, draft=True, prerelease=False, tag_name="draft",
                 url=f"https://api.github.com/repos/{REPO}/releases/{i}")
            for i in range(1, 101)]
    seen = serve(monkeypatch, [(rows, {}), ([], {})], {})
    assert len(reader.fetch_releases(REPO)) == 100
    assert len(seen) == 2


def test_partial_read_does_not_return_earlier_candidates(monkeypatch):
    row, body = public_release()
    calls = []
    def get(url, **_kwargs):
        calls.append(url)
        if "page=2" in url:
            raise reader.ReleaseFetchError("release_http_error", status=429)
        if "/download/" in url:
            return body, {}
        return json.dumps([row]).encode(), {
            "Link": f'<https://api.github.com/repos/{REPO}/releases?per_page=100&page=2>; rel="next"'}
    monkeypatch.setattr(reader, "_get", get)
    error = assert_error("release_http_error", lambda: reader.fetch_releases(REPO))
    assert error.status == 429
    assert len(calls) == 3


def test_draft_and_prerelease_do_not_fetch_missing_assets(monkeypatch):
    row, _ = public_release()
    row.pop("assets")
    row["prerelease"] = True
    seen = serve(monkeypatch, [([row], {})], {})
    assert reader.fetch_releases(REPO)[0]["prerelease"]
    assert len(seen) == 1


@pytest.mark.parametrize("change,code", [
    ({"url": "https://api.github.com/repos/other/fork/releases/1"}, "release_identity_mismatch"),
    ({"draft": "false"}, "invalid_release_record"),
])
def test_untrusted_or_malformed_release_fails_before_asset(monkeypatch, change, code):
    row, _ = public_release()
    row.update(change)
    seen = serve(monkeypatch, [([row], {})], {})
    assert_error(code, lambda: reader.fetch_releases(REPO))
    assert len(seen) == 1


@pytest.mark.parametrize("field,value,code", [
    ("browser_download_url", "https://attacker.test/payload", "untrusted_release_asset"),
    ("url", "https://api.github.com/repos/other/fork/releases/assets/1", "untrusted_release_asset"),
    ("size", 0, "invalid_release_asset"),
    ("size", reader.MAX_METADATA_BYTES + 1, "invalid_release_asset"),
    ("digest", "sha256:" + "0" * 64, "release_asset_digest_mismatch"),
])
def test_asset_identity_size_and_digest_are_checked(monkeypatch, field, value, code):
    row, body = public_release()
    original = row["assets"][0]["browser_download_url"]
    row["assets"][0][field] = value
    serve(monkeypatch, [([row], {})], {original: body})
    assert_error(code, lambda: reader.fetch_releases(REPO))


def test_duplicate_metadata_and_truncated_asset_fail(monkeypatch):
    row, body = public_release()
    row["assets"].append(deepcopy(row["assets"][0]))
    serve(monkeypatch, [([row], {})], {})
    assert_error("duplicate_release_metadata", lambda: reader.fetch_releases(REPO))
    row["assets"].pop()
    serve(monkeypatch, [([row], {})], {row["assets"][0]["browser_download_url"]: body[:-1]})
    assert_error("release_asset_size_mismatch", lambda: reader.fetch_releases(REPO))


def test_missing_digest_keeps_https_identity_and_size_checks(monkeypatch):
    row, body = public_release()
    row["assets"][0].pop("digest")
    serve(monkeypatch, [([row], {})], {row["assets"][0]["browser_download_url"]: body})
    assert reader.fetch_releases(REPO)[0]["metadata"]["version"] == "0.0.1"


def test_old_release_without_declaration_is_explicitly_undeclared(monkeypatch):
    row, _ = public_release()
    row["assets"] = []
    seen = serve(monkeypatch, [([row], {})], {})
    assert reader.fetch_releases(REPO)[0]["metadata"] is None
    assert len(seen) == 1


@pytest.mark.parametrize("target", [
    "https://api.github.com/repos/other/fork/releases?per_page=100&page=2",
    f"https://api.github.com/repos/{REPO}/releases?per_page=100&page=1",
    f"http://api.github.com/repos/{REPO}/releases?per_page=100&page=2",
    f"https://api.github.com/repos/{REPO}/releases?per_page=100&page=2&extra=1",
])
def test_pagination_cannot_change_origin_or_repeat(monkeypatch, target):
    serve(monkeypatch, [([], {"Link": f'<{target}>; rel="next"'})], {})
    assert_error("invalid_release_pagination", lambda: reader.fetch_releases(REPO))


def test_pagination_limit_is_not_a_partial_success(monkeypatch):
    monkeypatch.setattr(reader, "MAX_PAGES", 1)
    serve(monkeypatch, [([], {"Link": (
        f'<https://api.github.com/repos/{REPO}/releases?per_page=100&page=2>; rel="next"'
    )})], {})
    assert_error("release_pagination_limit", lambda: reader.fetch_releases(REPO))


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'{'])
def test_ambiguous_or_invalid_json_rejected(body):
    with pytest.raises(reader.ReleaseFetchError):
        reader._json(body)


@pytest.mark.parametrize("target", [
    "http://github.com/a", "https://github.com:invalid/a",
    "https://github.com.attacker.test/a", "https://user:secret@github.com/a",
    "https://attacker.test/a",
])
def test_redirect_rejects_untrusted_destination(target):
    request = urllib.request.Request("https://github.com/official")
    assert_error("untrusted_release_redirect", lambda: reader._OfficialRedirects().redirect_request(
        request, None, 302, "Found", {}, target))


def test_asset_cdn_redirect_is_supported():
    request = urllib.request.Request("https://github.com/official")
    target = "https://release-assets.githubusercontent.com/asset?signed=fixture"
    redirected = reader._OfficialRedirects().redirect_request(
        request, None, 302, "Found", {}, target)
    assert redirected.full_url == target


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_http_failure_is_static_and_does_not_retry(monkeypatch, status):
    calls = []
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, "private response", {}, None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    error = assert_error("release_http_error", lambda: reader._get(
        "https://api.github.com/fixture", maximum=8, timeout=1, accept="application/json"))
    assert error.status == status
    assert len(calls) == 1


def test_response_body_read_is_bounded(monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {}
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: type("Opener", (), {
        "open": lambda *args, **kwargs: Response(b"x" * 100),
    })())
    assert_error("release_response_too_large", lambda: reader._get(
        "https://api.github.com/fixture", maximum=8, timeout=1, accept="application/json"))


def test_repository_and_timeout_rejected_before_network(monkeypatch):
    monkeypatch.setattr(reader, "_get", lambda *args, **kwargs: pytest.fail("unexpected request"))
    with pytest.raises(ReleaseValidationError):
        reader.fetch_releases("someone/fork")
    for timeout in (0, -1, True, None, float("nan"), float("inf"), 61):
        assert_error("invalid_release_read_timeout",
                     lambda: reader.fetch_releases(REPO, timeout=timeout))


def source_responses(monkeypatch, change_commit=None, change_file=None, declaration=None):
    metadata = release()["metadata"]
    sha = metadata["source"]["sha"]
    api = f"https://api.github.com/repos/{REPO}"
    commit = dict(sha=sha, url=f"{api}/git/commits/{sha}")
    if change_commit:
        commit.update(change_commit)
    body = json.dumps(declaration or metadata["compatibility"]).encode()
    path = "plugins/mindie-agent/harness-compatibility.json"
    item = dict(
        type="file", path=path, url=f"{api}/contents/{path}?ref={sha}",
        encoding="base64", size=len(body), content=base64.encodebytes(body).decode(),
        sha=hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
    )
    if change_file:
        item.update(change_file)
    seen = []
    def get(url, **_kwargs):
        seen.append(url)
        return json.dumps(commit if "/git/commits/" in url else item).encode(), {}
    monkeypatch.setattr(reader, "_get", get)
    return metadata, seen


def test_source_verifies_exact_commit_and_blob_at_fixed_path(monkeypatch):
    metadata, seen = source_responses(monkeypatch)
    assert reader.verify_release_source(metadata, repository=REPO,
                                        harness_name="codex") == metadata
    assert len(seen) == 2
    assert seen[1].endswith("/plugins/mindie-agent/harness-compatibility.json?ref=" + "a" * 40)


def test_source_rejects_resolved_other_commit_before_reading_file(monkeypatch):
    metadata, seen = source_responses(monkeypatch, change_commit={"sha": "b" * 40})
    assert_error("release_source_commit_mismatch", lambda: reader.verify_release_source(
        metadata, repository=REPO, harness_name="codex"))
    assert len(seen) == 1


@pytest.mark.parametrize("change,code", [
    ({"type": "symlink"}, "invalid_release_source_file"),
    ({"path": "other.json"}, "invalid_release_source_file"),
    ({"url": "https://attacker.test/other"}, "invalid_release_source_file"),
    ({"content": "invalid!"}, "invalid_release_source_file"),
    ({"size": 1}, "release_source_size_mismatch"),
    ({"sha": "b" * 40}, "release_source_blob_mismatch"),
])
def test_source_rejects_wrong_file_or_blob(monkeypatch, change, code):
    metadata, _ = source_responses(monkeypatch, change_file=change)
    assert_error(code, lambda: reader.verify_release_source(
        metadata, repository=REPO, harness_name="codex"))


def test_valid_blob_with_different_release_declaration_is_rejected(monkeypatch):
    declaration = release()["metadata"]["compatibility"]
    declaration["supported"][0]["minimum"] = "0.153.3"
    declaration["interface_registry"]["hooks.stop"]["supported"][0]["minimum"] = "0.153.3"
    metadata, _ = source_responses(monkeypatch, declaration=declaration)
    assert_error("release_source_declaration_mismatch", lambda: reader.verify_release_source(
        metadata, repository=REPO, harness_name="codex"))


def test_source_requires_strict_types_not_python_bool_integer_equality(monkeypatch):
    declaration = release()["metadata"]["compatibility"]
    declaration["supported"][0]["maximum_inclusive"] = 1
    metadata, _ = source_responses(monkeypatch, declaration=declaration)
    with pytest.raises(ReleaseValidationError):
        reader.verify_release_source(metadata, repository=REPO, harness_name="codex")
