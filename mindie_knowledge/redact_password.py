"""[Role] Deterministic password / passphrase assignment detection (r4 rules).

[Invariants]
- Detect any non-empty secret value (``>= 1`` character), including plain,
  quoted and JSON-escaped forms inside MCP ``remote_auth`` arguments/results.
- A double-quoted JSON value may contain escaped quotes (``a\"bc``); the whole
  secret is one span, never a prefix ending at the escape.
- Mask only the secret value: the surrounding key, quotes, ``--flag`` and
  Chinese prose structure remain intact.
- Never report visible placeholders (``<your-password>``, ``$VAR``, ``***``)
  or closed self-redaction markers (``[REDACTED]``, ``<REDACTED>``,
  ``[REDACTED_PASSWORD]``); redacting already-redacted text adds no finding.
- Never report documentation words such as ``密码输入`` / ``密码学`` / ``密码回退``.

[Timeouts & Locks]
- Pure regular-expression parsing; no subprocess, network, lock or wait.

[Side-Effect Policy]
- Returns source spans only; never rewrites or persists input.
"""
from __future__ import annotations

import re

# A value may be double-quoted, single-quoted (both optionally JSON-escaped),
# or bare. Exactly one named group participates per match; ``match.lastindex``
# identifies it so the span covers the secret bytes only. Doubly escaped
# (JSON-in-JSON) delimiters use the non-recursive escaped forms; a plain
# opening quote may contain escaped inner quotes (``a\"bc``).
_PASSWORD_VALUE = (
    r"(?:"
    r"\\\"(?P<dqesc>(?:\\[^\"\r\n]|[^\\\"\r\n])+)\\\""
    r"|\"(?P<dq>(?:\\.|[^\"\\\r\n])+)\""
    r"|\\'(?P<sqesc>(?:\\[^'\r\n]|[^\\'\r\n])+)\\'"
    r"|'(?P<sq>(?:\\.|[^'\\\r\n])+)'"
    r"|(?P<bare>[^\s,，。；;)\]}\"'\\]+)"
    r")"
)

_PASSWORD_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"[A-Za-z0-9_.-]*(?:passw(?:or)?d|passwd|passphrase|pwd)"
    r"\b\\?\s*[\"']?\s*[:=]\s*" + _PASSWORD_VALUE,
    re.IGNORECASE,
)
_PASSWORD_FLAG = re.compile(
    r"(?<![\w-])"
    r"(?:--(?:password|passphrase|ssh[_-]?password)(?:=|[ \t]+)"
    r"|sshpass[ \t]+-p[ \t]*)"
    + _PASSWORD_VALUE,
    re.IGNORECASE,
)
_PASSWORD_CJK = re.compile(
    r"(?:SSH\s*)?(?:密码|口令|访问令牌|密钥)"
    r"(?:\s*(?:都是|均为|是|为|[:：=])\s*|\s+)" + _PASSWORD_VALUE,
    re.IGNORECASE,
)

# Visible placeholders are not secrets; masking them would only damage
# documentation examples such as ``password: <your-password>``.
_PASSWORD_PLACEHOLDER = re.compile(
    r"^(?:"
    r"<[^<>]+>"
    r"|\{[^{}]+\}"
    r"|\[[^\[\]]+\]"
    r"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\*{3,}"
    r")$"
)
# A closed self-redaction marker is never a new secret. The bare value rule
# stops at ``]``, so ``--password [REDACTED]`` would otherwise rescan as
# ``[REDACTED`` and quarantine an already-clean contribution.
_REDACTED_MARKER = re.compile(
    r"(?:\[REDACTED(?:_[A-Z0-9]+)*\]|<REDACTED(?:_[A-Z0-9]+)*>|\*{3,})",
    re.IGNORECASE,
)
PASSWORD_NEEDLES = ("password", "passwd", "passphrase", "pwd", "--pass",
                    "--ssh", "ssh_password", "ssh-password", "sshpass",
                    "密码", "口令", "访问令牌", "密钥")


def _value_spans(pattern, text):
    for match in pattern.finditer(text):
        index = match.lastindex
        if not index:
            continue
        value = match.group(index)
        if not value or _PASSWORD_PLACEHOLDER.fullmatch(value.strip()):
            continue
        marker = value.strip()
        if _REDACTED_MARKER.fullmatch(marker):
            continue
        if text[match.end(index):match.end(index) + 1] == "]":
            if _REDACTED_MARKER.fullmatch(marker + "]"):
                continue
        yield match.span(index)


def password_assignment_spans(text):
    """Spans of password/passphrase values in key/value and ``--flag`` forms."""
    seen = set()
    for span in (*_value_spans(_PASSWORD_ASSIGNMENT, text),
                 *_value_spans(_PASSWORD_FLAG, text)):
        if span not in seen:
            seen.add(span)
            yield span


def password_cjk_spans(text):
    """Spans of values after Chinese password/token labels."""
    yield from _value_spans(_PASSWORD_CJK, text)
