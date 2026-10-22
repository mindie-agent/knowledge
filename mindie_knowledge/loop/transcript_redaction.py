"""[Role] Deterministic local transcript secret & privacy redactor (pinned Gitleaks + regex/HMAC rules).

[Invariants]
- `redact_increment` carries open multi-line PEM private-key state (`{'private_key': ...}`)
  across incremental transcript chunks so split keys remain masked until `-----END ...-----`.
- Unions all detector spans (pinned Gitleaks `8.30.1` + `mindie_knowledge.redact.scan_text`
  + caller `private_paths` across POSIX/Windows/WSL spellings) before replacing
  text with HMAC-keyed `<redacted:rule:12-hex>` or `[REDACTED_SECRET]` placeholders.

[Timeouts & Locks]
- Subprocess execution bounded by `REDACTION_TIMEOUT_SECONDS = 30.0` (or
  `MINDIE_REDACTION_TIMEOUT_SECONDS`) and cancellable via `cancel` event.

[Side-Effect Policy]
- `redact()` runs local Gitleaks over stdin with isolated config (`--ignore-gitleaks-allow`);
  `install_scanner()` downloads/verifies the SHA-256 pinned archive only during setup/update.
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
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from mindie_knowledge.redact import scan_text
from mindie_knowledge.community.common import CommunityError, run_argv
from .process import MaintenanceCancelled

VERSION = "8.30.1"
ARCHIVES = {
    ("Windows", "x64"): ("zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
    ("Windows", "arm64"): ("zip", "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f"),
    ("Linux", "x64"): ("tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    ("Linux", "arm64"): ("tar.gz", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    ("Darwin", "x64"): ("tar.gz", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    ("Darwin", "arm64"): ("tar.gz", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
}


_EXTRACTED_MEMBERS: dict[tuple[str, str], bytes] = {}


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
        cache_key = (expected, member)
        data = _EXTRACTED_MEMBERS.get(cache_key)
        if data is None:
            if ext == "zip":
                with zipfile.ZipFile(io.BytesIO(raw)) as source:
                    data = source.read(member)
            else:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
                    data = source.extractfile(member).read()
            _EXTRACTED_MEMBERS[cache_key] = data
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


_KEY_BEGIN = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY)-----")


def _check_cancel(cancel):
    if cancel is not None and callable(getattr(cancel, "is_set", None)) and cancel.is_set():
        raise MaintenanceCancelled("transcript secret scanner cancelled by owner")


def redact_increment(text, *, state=None, executable, key, private_paths=(), cancel=None):
    """Redact a complete-message increment, carrying only the open PEM type.

    State contains no source text. The caller commits it with the source cursor
    and material files; a retry of the same range starts from the same state.
    A missing or mismatched END keeps subsequent increments masked.
    """
    _check_cancel(cancel)
    state = dict(state or {})
    if set(state) - {'private_key'}:
        raise ValueError('unknown redaction state')
    opened = state.get('private_key')
    if opened is not None and not re.fullmatch(r'(?:[A-Z0-9]+ )*PRIVATE KEY', opened):
        raise ValueError('invalid redaction state')
    continued = bool(opened)
    if opened:
        marker = '-----END ' + opened + '-----'
        end = text.find(marker)
        if end < 0:
            return '[REDACTED_SECRET]', ['credential-private-key'], state
        text = '[REDACTED_SECRET]' + text[end + len(marker):]
        opened = None
    cursor = 0
    while match := _KEY_BEGIN.search(text, cursor):
        marker = '-----END ' + match.group(1) + '-----'
        end = text.find(marker, match.end())
        if end < 0:
            opened = match.group(1)
            break
        cursor = end + len(marker)
    clean, rules = redact(text, executable=executable, key=key, private_paths=private_paths, cancel=cancel)
    if continued:
        rules = sorted(set(rules) | {'credential-private-key'})
    return clean, rules, {'private_key': opened} if opened else {}


GITLEAKS_CONFIG = "[extend]\nuseDefault = true\n"
_CONFIG_DIR: tempfile.TemporaryDirectory | None = None


def _scanner_config() -> tuple[Path, Path]:
    global _CONFIG_DIR
    if _CONFIG_DIR is not None:
        temp = Path(_CONFIG_DIR.name)
        config = temp / "config.toml"
        try:
            if temp.is_dir() and config.is_file() and config.read_text(encoding="utf-8") == GITLEAKS_CONFIG:
                return temp, config
        except TimeoutError:
            raise
        except OSError:
            pass
    _CONFIG_DIR = tempfile.TemporaryDirectory(prefix="mindie-redaction-")
    temp = Path(_CONFIG_DIR.name)
    config = temp / "config.toml"
    config.write_text(GITLEAKS_CONFIG, encoding="utf-8")
    return temp, config


REDACTION_TIMEOUT_SECONDS = 30.0


def _redaction_timeout(timeout=None):
    if timeout is not None:
        return float(timeout)
    raw = os.environ.get("MINDIE_REDACTION_TIMEOUT_SECONDS")
    return float(raw) if raw is not None else REDACTION_TIMEOUT_SECONDS


def _redaction_spans(text, *, executable, private_paths=(), cancel=None, timeout=None):
    _check_cancel(cancel)
    if not executable or not Path(executable).is_absolute():
        raise ValueError("transcript redaction requires an installed scanner")
    # Never honor transcript comments, repository config or environment
    # allowlists that could silently disable credential detection.
    temp, config = _scanner_config()
    try:
        result = run_argv(
            [executable, "stdin", "--no-banner", "--no-color", "--log-level", "error",
             "--config", str(config), "--ignore-gitleaks-allow", "--report-format", "json",
             "--report-path", "-", "--exit-code", "0"],
            input_bytes=text.encode("utf-8"), max_output=None,
            cwd=temp, timeout=_redaction_timeout(timeout), cancel=cancel,
            env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")},
            inherit_env=False,
        )
    except TimeoutError:
        raise
    except (OSError, CommunityError):
        _check_cancel(cancel)
        raise ScannerUnavailable("transcript secret scanner unavailable") from None
    _check_cancel(cancel)
    if result.code != 0 or result.timed_out:
        raise ScannerUnavailable("transcript secret scanner failed")
    try:
        findings = json.loads(result.out or b"[]")
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
        raise ScannerUnavailable("transcript secret scanner returned invalid spans") from None
    # Reporting suppresses duplicate overlapping findings. Replacement must
    # instead union every detector's complete claim: masking an inner identity
    # must not leave a formerly overlapping path/credential tail exposed.
    spans.extend((f.start, f.end, f.rule) for f in scan_text(text, include_overlaps=True))
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
    return merged


def _replace_spans(text, spans, key):
    pieces = []
    cursor = 0
    for start, end, rule in spans:
        token = hmac.new(key, text[start:end].encode(), hashlib.sha256).hexdigest()[:12]
        # A fixed closed token cannot itself be mistaken for a credential
        # assignment by the final outbound scan. It contains no user input.
        placeholder = '[REDACTED_SECRET]' if rule.startswith(('secret-', 'credential-')) else f"<redacted:{rule}:{token}>"
        pieces.extend((text[cursor:start], placeholder))
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces)


def redact(text, *, executable, key, private_paths=(), cancel=None, timeout=None):
    spans = _redaction_spans(text, executable=executable, private_paths=private_paths,
                            cancel=cancel, timeout=timeout)
    return _replace_spans(text, spans, key), sorted({rule for _, _, rule in spans})


def redact_tree(value, *, executable, key, private_paths=(), cancel=None, timeout=None):
    """Scan JSON values once without treating serialization escapes as content.

    Keys and unescaped string values retain their JSON assignment context in
    the scanner input. Findings are projected back onto each original string; JSON
    delimiters and typed numeric measurements are never rewritten as text.
    """
    parts, strings, position = [], [], 0

    def append(text, *, leaf=False):
        nonlocal position
        if leaf:
            strings.append((position, position + len(text), text))
        parts.append(text)
        position += len(text)

    def collect(item):
        if isinstance(item, dict):
            append('{\n')
            for name, member in item.items():
                append('"')
                append(name, leaf=True)
                append('\": ')
                collect(member)
                append(',\n')
            append('}')
        elif isinstance(item, list):
            append('[\n')
            for member in item:
                collect(member)
                append(',\n')
            append(']')
        elif isinstance(item, str):
            append('"')
            append(item, leaf=True)
            append('"')
        else:
            append(json.dumps(item))

    collect(value)
    spans = _redaction_spans(''.join(parts), executable=executable,
                            private_paths=private_paths, cancel=cancel, timeout=timeout)
    cleaned, cursor = [], 0
    for start, end, original in strings:
        while cursor < len(spans) and spans[cursor][1] <= start:
            cursor += 1
        selected, index = [], cursor
        while index < len(spans) and spans[index][0] < end:
            left, right, rule = spans[index]
            selected.append((max(left, start) - start, min(right, end) - start, rule))
            index += 1
        cleaned.append(_replace_spans(original, selected, key))
    values = iter(cleaned)

    def rebuild(item):
        if isinstance(item, dict):
            result = {}
            for _, member in item.items():
                name = next(values)
                if name in result:
                    raise ValueError('redaction produced duplicate object keys')
                result[name] = rebuild(member)
            return result
        if isinstance(item, list):
            return [rebuild(member) for member in item]
        return next(values) if isinstance(item, str) else item

    return rebuild(value), sorted({rule for _, _, rule in spans})


if __name__ == "__main__":
    sys.stdout.buffer.write((install_scanner() + '\n').encode('utf-8'))
