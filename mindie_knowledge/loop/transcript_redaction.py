"""Local transcript redaction. No model, network or ambient scanner config.

The installer calls install_scanner once; captures only use its pinned binary.
Gitleaks findings stay in memory and errors never include its output. Existing
privacy rules cover identities and paths; Gitleaks supplies maintained secret
detectors. Neither scanner establishes that proprietary prose is public.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from mindie_knowledge.redact import scan_text

VERSION = "8.30.1"
ARCHIVES = {
    ("Windows", "x64"): ("zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
    ("Windows", "arm64"): ("zip", "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f"),
    ("Linux", "x64"): ("tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    ("Linux", "arm64"): ("tar.gz", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    ("Darwin", "x64"): ("tar.gz", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    ("Darwin", "arm64"): ("tar.gz", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
}


def install_scanner(cache=None):
    """Verified release archive; extract only the executable and license.

    Invoked by setup/update, never by a Stop or the background worker.
    A cached archive is verified again; an altered binary is repaired from it.
    """
    system, machine = platform.system(), platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if (system, arch) not in ARCHIVES:
        raise ValueError("no pinned Gitleaks build for this platform")
    ext, expected = ARCHIVES[system, arch]
    name = f"gitleaks_{VERSION}_{system.lower()}_{arch}.{ext}"
    base = Path(cache) if cache else Path.home() / ".cache" / "mindie-agent" / "gitleaks"
    base = base / VERSION / f"{system.lower()}-{arch}"
    base.mkdir(parents=True, exist_ok=True)
    archive = base / name
    raw = archive.read_bytes() if archive.exists() else None
    if raw is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("cached Gitleaks archive checksum mismatch")
    if raw is None:
        url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{name}"
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(32 * 1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("downloaded Gitleaks archive checksum mismatch")
        _replace(archive, raw)
    binary = "gitleaks.exe" if system == "Windows" else "gitleaks"
    for member in (binary, "LICENSE"):
        if ext == "zip":
            with zipfile.ZipFile(io.BytesIO(raw)) as source:
                data = source.read(member)
        else:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
                data = source.extractfile(member).read()
        target = base / member
        if not target.exists() or target.read_bytes() != data:
            _replace(target, data)
        if member == binary:
            target.chmod(0o700)
    return str((base / binary).absolute())


def _replace(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ScannerUnavailable(OSError):
    """Retry local deterministic work after the scanner becomes available."""


def redact(text, *, executable, key, private_paths=()):
    if not executable or not Path(executable).is_absolute():
        raise ValueError("transcript redaction requires an installed scanner")
    # Never honor transcript comments, repository config or environment
    # allowlists that could silently disable credential detection.
    with tempfile.TemporaryDirectory(prefix="mindie-redaction-") as temp:
        config = Path(temp) / "config.toml"
        config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [executable, "stdin", "--no-banner", "--no-color", "--log-level", "error",
                 "--config", str(config), "--ignore-gitleaks-allow", "--report-format", "json",
                 "--report-path", "-", "--exit-code", "0"],
                input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=temp, timeout=30, check=False,
                env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ScannerUnavailable("transcript secret scanner unavailable") from None
    if result.returncode != 0:
        raise ValueError("transcript secret scanner failed")
    try:
        findings = json.loads(result.stdout or b"[]")
        if not isinstance(findings, list):
            raise ValueError
        spans = []
        offsets = [0]
        offsets.extend(m.end() for m in re.finditer('\n', text))
        offsets.append(len(text))
        for item in findings:
            # Gitleaks columns count bytes and are unreliable following a
            # multi-byte line. Resolve its exact match inside the reported
            # lines, then the secret within that match, in Python offsets.
            first, last = item["StartLine"], item["EndLine"]
            secret, match = item["Secret"], item["Match"]
            if not (isinstance(secret, str) and secret and secret in match
                    and 1 <= first <= last < len(offsets)):
                raise ValueError
            lo, hi = offsets[first - 1], offsets[last]
            found = text.find(match, lo, hi)
            if found < 0:
                raise ValueError
            while found >= 0:
                start = found + match.index(secret)
                spans.append((start, start + len(secret), "secret-" + item["RuleID"]))
                found = text.find(match, found + len(match), hi)
    except (ValueError, KeyError, TypeError, IndexError):
        raise ValueError("transcript secret scanner returned invalid spans") from None
    spans.extend((f.start, f.end, f.rule) for f in scan_text(text))
    for path in private_paths:
        if not isinstance(path, str) or len(path) < 3:
            continue
        normalized = path.replace('\\', '/').rstrip('/')
        spellings = {path.rstrip('/\\'), normalized}
        if re.match(r'^[A-Za-z]:/', normalized):
            spellings.update((normalized.replace('/', '\\'), '/mnt/' + normalized[0].lower() + normalized[2:]))
        elif re.match(r'^/mnt/[A-Za-z]/', normalized):
            windows = normalized[5].upper() + ':' + normalized[6:]
            spellings.update((windows, windows.replace('/', '\\')))
        for spelling in spellings:
            pattern = re.escape(spelling) + r'(?=$|[/\\\s`"\'<>),;:\]?#]|\.(?=$|\s))'
            flags = re.IGNORECASE if re.match(r'^[A-Za-z]:', spelling) else 0
            spans.extend((m.start(), m.end(), 'local-path') for m in re.finditer(pattern, text, flags))
    placeholders = [m.span() for m in re.finditer(r'<redacted:[a-z0-9-]+:[0-9a-f]{12}>', text)]
    spans = [(start, end, rule) for start, end, rule in spans
             if not any(left <= start and end <= right for left, right in placeholders)]
    # Merge overlapping claims; never apply offsets to already-replaced text.
    merged = []
    for start, end, rule in sorted(spans):
        if start is None or end is None or not 0 <= start < end <= len(text):
            raise ValueError("transcript redaction returned invalid spans")
        if merged and start < merged[-1][1]:
            old = merged[-1]
            merged[-1] = (old[0], max(old[1], end), old[2])
        else:
            merged.append((start, end, rule))
    rules = sorted({rule for _, _, rule in merged})
    pieces = []
    cursor = 0
    for start, end, rule in merged:
        token = hmac.new(key, text[start:end].encode(), hashlib.sha256).hexdigest()[:12]
        pieces.extend((text[cursor:start], f"<redacted:{rule}:{token}>"))
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces), rules


if __name__ == "__main__":
    sys.stdout.buffer.write((install_scanner() + '\n').encode('utf-8'))
