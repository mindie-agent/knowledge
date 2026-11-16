"""[Role] Share bounded ETag release reads across component and plugin updaters.

[Invariants] Cache entries contain public response bytes and selected public
headers only. Caller validation precedes cache use and publication, including
legacy entries. An expired cache never conceals a failed network read. Credentials
stay in memory and are sent only to api.github.com.
[Timeouts & Locks] HTTP and credential discovery have explicit caller/5s limits.
Atomic replacement permits concurrent readers without an extra update authority.
[Side-Effect Policy] Read-only public HTTP plus a metadata cache capped at 64 MiB
and 128 entries. No native configuration or knowledge state is accessed.
"""
from __future__ import annotations

import base64
from functools import cache
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_HEADERS = ("ETag", "Link", "Content-Length", "Content-Type", "Last-Modified")
CACHE_ENTRY_LIMIT = 128
CACHE_BYTE_LIMIT = 64 * 1024 * 1024
STALE_TEMPORARY_SECONDS = 3600.0
_CACHE_NAME = re.compile(r"(?:http-[0-9a-f]{64}|[0-9a-f]{64})\.json")


def _prune_http_cache(root, *, deadline=None):
    """Cap live cache bytes; also retire legacy v1 entries and stale temp files.

    Legacy ``<64hex>.json`` files predate the ``http-`` prefix but occupy the
    same cache. A crashed writer's ``*.tmp`` file is invisible to readers and
    is removed only after an hour, so an in-flight write is never stolen.
    """
    now = time.time()
    entries, stale = [], []
    try:
        candidates = list(Path(root).iterdir())
    except TimeoutError:
        raise
    except OSError:
        return {"removed": 0, "stale": 0, "remaining": 0}
    for file in candidates:
        try:
            stat = file.stat()
        except FileNotFoundError:
            continue
        except TimeoutError:
            raise
        except OSError:
            continue
        if file.suffix == ".tmp" and now - stat.st_mtime > STALE_TEMPORARY_SECONDS:
            stale.append(file)
        elif _CACHE_NAME.fullmatch(file.name):
            entries.append((stat.st_mtime, stat.st_size, file))
    for file in stale:
        try:
            file.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            pass
    entries.sort()
    total = sum(size for _, size, _ in entries)
    removed = 0
    while entries and (len(entries) > CACHE_ENTRY_LIMIT or total > CACHE_BYTE_LIMIT):
        if deadline is not None and time.monotonic() >= deadline:
            break
        _, size, file = entries.pop(0)
        try:
            file.unlink(missing_ok=True)
        except TimeoutError:
            raise
        except OSError:
            continue
        total -= size
        removed += 1
    return {"removed": removed, "stale": len(stale), "remaining": len(entries)}


class ReleaseResponseTooLarge(ValueError):
    """A bounded release body cannot be treated as a complete catalog."""


class ReleaseResponseInvalid(RuntimeError):
    """The caller rejected response bytes; a derived cache may discard them."""


class ReleaseResponseFramingError(http.client.HTTPException):
    """Remote framing cannot describe a complete bounded response."""


@cache
def token():
    from ._host_exec import which_native
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value:
        return value
    gh = which_native("gh")
    if not gh:
        return None
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    result = subprocess.run([gh, "auth", "token", "--hostname", "github.com"],
                            capture_output=True, timeout=5, check=False, **kwargs)
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None


def _validate_body(body, headers, validate):
    length = headers.get("Content-Length")
    if length is not None:
        if not isinstance(length, str) or not length.strip(" \t").isascii() or not length.strip(" \t").isdecimal():
            raise ReleaseResponseFramingError("invalid release Content-Length")
        try:
            expected = int(length)
        except ValueError:
            raise ReleaseResponseFramingError("invalid release Content-Length") from None
        if len(body) != expected:
            # read(n) may return a short body without raising IncompleteRead.
            raise http.client.IncompleteRead(b"", max(0, expected - len(body)))
    if validate is not None:
        validate(body)


def read(url, *, maximum, timeout, accept, redirects, directory=None, force=False, credential=None,
         validate=None):
    force = force or os.environ.get("MINDIE_RELEASE_FORCE") == "1"
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.hostname not in {"github.com", "api.github.com", "release-assets.githubusercontent.com"}):
        raise ValueError("untrusted release read origin")
    root = Path(directory or os.environ.get("MINDIE_RELEASE_CACHE", Path.home() / ".cache/mindie-agent/releases"))
    path = root / ("http-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        base64.b64decode(saved["body"], validate=True)
        if not isinstance(saved["headers"], dict) or not isinstance(saved["checked_at"], (int, float)):
            raise ValueError("invalid derived release cache")
    except FileNotFoundError:
        saved = None
    except (ValueError, KeyError, TypeError):
        # This is derived public metadata, not an update or authorization
        # receipt. A corrupt entry is replaced by a fresh authoritative read.
        saved = None
    if saved is not None:
        body = base64.b64decode(saved["body"], validate=True)
        if len(body) > maximum:
            raise ReleaseResponseTooLarge("release response exceeds its byte limit")
        try:
            _validate_body(body, saved["headers"], validate)
        except (ReleaseResponseInvalid, ReleaseResponseFramingError, http.client.IncompleteRead):
            # Old versions cached HTTP 200 bytes before parsing JSON. Do
            # not let their ETag revalidate a body we cannot consume.
            saved = None
            path.unlink(missing_ok=True)
    immutable = any(part in parsed.path for part in ("/git/", "/contents/", "/releases/download/"))
    available = token() if credential is None else credential
    ttl = 86400 if immutable else (300 if available else 900)
    if saved is not None and not force and time.time() - saved["checked_at"] < ttl:
        body = base64.b64decode(saved["body"], validate=True)
        return body, saved["headers"]
    headers = {"Accept": accept, "User-Agent": "mindie-release-updater"}
    if parsed.hostname == "api.github.com":
        if available:
            headers["Authorization"] = "Bearer " + available
    if saved is not None and saved["headers"].get("ETag"):
        headers["If-None-Match"] = saved["headers"]["ETag"]
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(redirects).open(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError("release read did not return a successful response")
            body = response.read(maximum + 1)
            public = {name: response.headers[name] for name in PUBLIC_HEADERS if name in response.headers}
    except urllib.error.HTTPError as exc:
        if exc.code != 304 or saved is None:
            raise
        body, public = base64.b64decode(saved["body"], validate=True), saved["headers"]
    if len(body) > maximum:
        raise ReleaseResponseTooLarge("release response exceeds its byte limit")
    _validate_body(body, public, validate)
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(body=base64.b64encode(body).decode("ascii"), headers=public,
                           checked_at=time.time()), stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    _prune_http_cache(root)
    return body, public
