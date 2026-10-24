"""Public ETag caching reduces polls without changing network failure evidence."""
import io
import json
import urllib.error
import urllib.request

import pytest

from mindie_knowledge import release_http


def test_etag_304_reuses_body_without_persisting_auth_or_private_headers(tmp_path, monkeypatch):
    calls = []
    class Response(io.BytesIO):
        status = 200
        headers = {"ETag": '"release-1"', "Set-Cookie": "private-cookie", "Link": "public-next"}
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                return Response(b'{"tag_name":"v0.13.2"}')
            raise urllib.error.HTTPError(request.full_url, 304, "unchanged", {}, None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    def read(**extra):
        return release_http.read("https://api.github.com/repos/mindie-agent/knowledge/releases/latest",
                                 maximum=1000, timeout=1, accept="application/json", redirects=None,
                                 directory=tmp_path, credential="private-token", **extra)
    first = read()
    assert read() == first
    assert len(calls) == 1
    assert read(force=True) == first
    assert calls[1].get_header("If-none-match") == '"release-1"'
    assert calls[0].get_header("Authorization") == "Bearer private-token"
    stored = next(tmp_path.glob("http-*.json")).read_text()
    assert "private-token" not in stored and "private-cookie" not in stored
    assert json.loads(stored)["headers"]["ETag"] == '"release-1"'


def test_cached_success_does_not_mask_a_failed_refresh(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {}
    class Opener:
        failed = False
        def open(self, request, **kwargs):
            if self.failed:
                raise TimeoutError("controlled release deadline")
            self.failed = True
            return Response(b'{}')
    opener = Opener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: opener)
    def read(**extra):
        return release_http.read("https://api.github.com/repos/mindie-agent/knowledge/releases/latest",
                                 maximum=100, timeout=1, accept="application/json", redirects=None,
                                 directory=tmp_path, credential="", **extra)
    assert read()[0] == b'{}'
    with pytest.raises(TimeoutError, match="controlled"):
        read(force=True)
