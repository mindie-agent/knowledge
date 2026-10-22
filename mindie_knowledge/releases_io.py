"""Public, bounded GitHub Release reads for the pure eligibility selector.

[Role] Bounded public GitHub Release reads for the pure eligibility selector.
[Invariants] Responses are size-limited and validated before use; authentication tokens are never persisted in release metadata.
[Timeouts & Locks] Every HTTP read has an explicit timeout and byte cap controlled by the caller.
[Side-Effect Policy] Read-only network access to the fixed release repository; no installs, writes or token export.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .releases import (
    REPOSITORIES, ReleaseValidationError, validate_compatibility, validate_release_metadata,
)


ASSET_NAME = "mindie-plugin-release.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100
_COMPATIBILITY_PATHS = {
    "codex": "plugins/mindie-agent/harness-compatibility.json",
    "claude-code": "harness-compatibility.json",
    "kimi-code": "harness-compatibility.json",
}


class ReleaseFetchError(RuntimeError):
    """An incomplete/unavailable catalog is not an empty successful result."""

    def __init__(self, code, *, status=None):
        self.code = code
        self.status = status
        super().__init__(code)


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = urllib.parse.urlsplit(newurl)
            trusted = (
                target.scheme == "https" and not target.username and not target.password
                and target.port in (None, 443)
                and target.hostname in {
                    "github.com", "api.github.com", "release-assets.githubusercontent.com",
                }
            )
        except ValueError:
            trusted = False
        if not trusted:
            raise ReleaseFetchError("untrusted_release_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url, *, maximum, timeout, accept):
    request = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": "mindie-release-reader",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.build_opener(_OfficialRedirects()).open(
                request, timeout=timeout) as response:
            if response.status != 200:
                raise ReleaseFetchError("release_http_error", status=response.status)
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise ReleaseFetchError("release_response_too_large")
            return body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raise ReleaseFetchError("release_http_error", status=exc.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise ReleaseFetchError("release_transport_unavailable") from None


def _json(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseFetchError("duplicate_release_json_key")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ReleaseFetchError("invalid_release_json")
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ReleaseFetchError("invalid_release_json") from None


def _next_page(headers, *, repository, page):
    link = next((value for key, value in headers.items() if key.lower() == "link"), "")
    found = []
    for item in link.split(","):
        if re.search(r';\s*rel="?next"?\s*(?:;|$)', item):
            match = re.match(r"\s*<([^>]+)>", item)
            if match is None:
                raise ReleaseFetchError("invalid_release_pagination")
            try:
                target = urllib.parse.urlsplit(match[1])
            except ValueError:
                raise ReleaseFetchError("invalid_release_pagination") from None
            if (target.scheme != "https" or target.netloc != "api.github.com"
                    or target.path != f"/repos/{repository}/releases"
                    or target.fragment
                    or urllib.parse.parse_qs(target.query) != {
                        "page": [str(page + 1)], "per_page": ["100"]}):
                raise ReleaseFetchError("invalid_release_pagination")
            found.append(True)
    if len(found) > 1:
        raise ReleaseFetchError("invalid_release_pagination")
    return bool(found)


def _metadata(row, *, repository, timeout):
    assets = row.get("assets")
    if (not isinstance(assets, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   for item in assets)):
        raise ReleaseFetchError("invalid_release_assets")
    matches = [item for item in assets
               if isinstance(item, dict) and item.get("name") == ASSET_NAME]
    if not matches:
        return None
    if len(matches) != 1:
        raise ReleaseFetchError("duplicate_release_metadata")
    asset = matches[0]
    ident, size, tag = asset.get("id"), asset.get("size"), row.get("tag_name")
    if (type(ident) is not int or ident <= 0 or type(size) is not int
            or not 1 <= size <= MAX_METADATA_BYTES
            or not isinstance(tag, str) or not 1 <= len(tag) <= 129):
        raise ReleaseFetchError("invalid_release_asset")
    api_url = f"https://api.github.com/repos/{repository}/releases/assets/{ident}"
    download = (f"https://github.com/{repository}/releases/download/"
                f"{urllib.parse.quote(tag, safe='')}/{ASSET_NAME}")
    if asset.get("url") != api_url or asset.get("browser_download_url") != download:
        raise ReleaseFetchError("untrusted_release_asset")
    body, _ = _get(download, maximum=MAX_METADATA_BYTES, timeout=timeout,
                   accept="application/octet-stream")
    if len(body) != size:
        raise ReleaseFetchError("release_asset_size_mismatch")
    digest = asset.get("digest")
    if digest is not None:
        if (not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or digest != "sha256:" + hashlib.sha256(body).hexdigest()):
            raise ReleaseFetchError("release_asset_digest_mismatch")
    return _json(body)


def fetch_releases(repository, *, timeout=10.0):
    """Read every public Release page and its official metadata asset.

    No authentication files, local index, state write or installation is used.
    Any partial read fails the whole call. A digest is checked when supplied by
    GitHub; otherwise HTTPS origin and advertised byte length remain checked.
    The adapter must still verify the selected Git SHA and its declaration.
    """
    if repository not in REPOSITORIES.values():
        raise ReleaseValidationError("untrusted_repository")
    if (type(timeout) not in (int, float) or not 0 < timeout <= 60):
        raise ReleaseFetchError("invalid_release_read_timeout")
    result, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = (f"https://api.github.com/repos/{repository}/releases"
               f"?per_page=100&page={page}")
        body, headers = _get(url, maximum=MAX_PAGE_BYTES, timeout=timeout,
                             accept="application/vnd.github+json")
        rows = _json(body)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ReleaseFetchError("invalid_release_page")
        more = _next_page(headers, repository=repository, page=page)
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) is not int
                    or row["id"] <= 0 or type(row.get("draft")) is not bool
                    or type(row.get("prerelease")) is not bool):
                raise ReleaseFetchError("invalid_release_record")
            ident = row["id"]
            if (ident in seen
                    or row.get("url") != f"https://api.github.com/repos/{repository}/releases/{ident}"):
                raise ReleaseFetchError("release_identity_mismatch")
            seen.add(ident)
            record = {key: row.get(key) for key in ("id", "tag_name", "draft", "prerelease")}
            if not row["draft"] and not row["prerelease"]:
                record["metadata"] = _metadata(row, repository=repository, timeout=timeout)
            result.append(record)
        if not more and len(rows) < 100:
            return result
    raise ReleaseFetchError("release_pagination_limit")


def verify_release_source(metadata, *, repository, harness_name, timeout=10.0):
    """Verify an official immutable source object and its compatibility file.

    This is public source verification, not proof of native installation.
    File paths are fixed by Harness; no asset-provided URL or filename is read.
    """
    validated = validate_release_metadata(metadata, repository=repository,
                                          harness_name=harness_name)
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ReleaseFetchError("invalid_release_read_timeout")
    sha = validated["source"]["sha"]
    api = f"https://api.github.com/repos/{repository}"
    raw, _ = _get(f"{api}/git/commits/{sha}", maximum=MAX_METADATA_BYTES,
                  timeout=timeout, accept="application/vnd.github+json")
    commit = _json(raw)
    if (not isinstance(commit, dict) or commit.get("sha") != sha
            or commit.get("url") != f"{api}/git/commits/{sha}"):
        raise ReleaseFetchError("release_source_commit_mismatch")
    path = _COMPATIBILITY_PATHS[harness_name]
    url = f"{api}/contents/{path}?ref={sha}"
    raw, _ = _get(url, maximum=2 * MAX_METADATA_BYTES, timeout=timeout,
                  accept="application/vnd.github+json")
    item = _json(raw)
    if (not isinstance(item, dict) or item.get("type") != "file"
            or item.get("path") != path or item.get("url") != url
            or item.get("encoding") != "base64"
            or type(item.get("size")) is not int
            or not 1 <= item["size"] <= MAX_METADATA_BYTES
            or not isinstance(item.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", item["sha"]) is None
            or not isinstance(item.get("content"), str)
            or len(item["content"]) > 2 * MAX_METADATA_BYTES):
        raise ReleaseFetchError("invalid_release_source_file")
    try:
        body = base64.b64decode(item["content"].replace("\n", ""), validate=True)
    except (ValueError, binascii.Error):
        raise ReleaseFetchError("invalid_release_source_file") from None
    if len(body) != item["size"]:
        raise ReleaseFetchError("release_source_size_mismatch")
    digest = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
    if digest != item["sha"]:
        raise ReleaseFetchError("release_source_blob_mismatch")
    declaration = validate_compatibility(_json(body), harness_name=harness_name)
    if declaration != validated["compatibility"]:
        raise ReleaseFetchError("release_source_declaration_mismatch")
    return validated
