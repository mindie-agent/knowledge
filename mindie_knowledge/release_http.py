"""[Role] Share bounded ETag release reads across component and plugin updaters.

[Invariants] Cache entries contain public response bytes and selected public
headers only. An expired cache never conceals a failed network read. Credentials
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
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_HEADERS = ("ETag", "Link", "Content-Length", "Content-Type", "Last-Modified")


class ReleaseResponseTooLarge(ValueError):
    """A bounded release body cannot be treated as a complete catalog."""


@cache
def token():
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value or not shutil.which("gh"):
        return value
    result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                            capture_output=True, timeout=5, check=False)
    return result.stdout.decode("utf-8").strip() if result.returncode == 0 else None


def read(url, *, maximum, timeout, accept, redirects, directory=None, force=False, credential=None):
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
    immutable = any(part in parsed.path for part in ("/git/", "/contents/", "/releases/download/"))
    available = token() if credential is None else credential
    ttl = 86400 if immutable else (300 if available else 900)
    if saved is not None and not force and time.time() - saved["checked_at"] < ttl:
        body = base64.b64decode(saved["body"], validate=True)
        if len(body) > maximum:
            raise ReleaseResponseTooLarge("release response exceeds its byte limit")
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
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(body=base64.b64encode(body).decode("ascii"), headers=public,
                           checked_at=time.time()), stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    entries = []
    for file in root.glob("http-*.json"):
        try:
            stat = file.stat()
        except FileNotFoundError:
            continue  # Another updater may have pruned the same cache entry.
        entries.append((stat.st_mtime, stat.st_size, file))
    entries.sort()
    total = sum(size for _, size, _ in entries)
    while entries and (len(entries) > 128 or total > 64 * 1024 * 1024):
        _, size, file = entries.pop(0)
        file.unlink(missing_ok=True)
        total -= size
    return body, public
