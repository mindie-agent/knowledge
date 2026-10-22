"""[Role] Deterministic redaction ruleset and built-in public allowlist values.

[Invariants]
- `REDACTION_PROFILE` changes whenever a rule tightens, so downstream entries
  cleared under an older profile are re-scanned rather than silently trusted.
- Rule matches preserve non-address version tokens and never let a complete
  address/private-key token pass; built-in allow entries are public by construction.
- Function rules (IPv4/IPv6, high entropy, private-key blocks) are pure and
  return disjoint or explicitly overlapping spans without rewriting input.

[Timeouts & Locks]
- Pure regular-expression and string parsing; no subprocess, network, file
  lock or wait is performed by any rule.

[Side-Effect Policy]
- No filesystem, environment or network writes; only immutable constants and
  span evidence are produced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator


#: Version of the ruleset declared in this file. Bump when a rule tightens.
#:
#: r2: adds ``internal-machine-identifier`` (a machine word followed by a
#: slot number, "remote 131" / "remote_131", and run identifiers that embed a
#: machine slot beside a device index, "microbenchmark_131_npu4_...") and adds
#: ``remote`` to the numbered-host prefixes. Entries cleared under r1 must be
#: re-scanned (sync/rescan.py --profile r2).
#: r2 (correction): ``hostname-numbered`` no longer allows unbounded words
#: between the prefix and the digits, so prose such as ``worker-pool-size-32``
#: and candidate ids such as ``<owner>-<slug>-<hex>`` are not reported. The
#: trailing boundary is ``(?![a-z0-9])`` so a slot token may continue with
#: ``_`` or ``-`` (``remote_000_cann_...`` still hits ``remote_000``). This
#: is a relaxation, not a tightening; entries already cleared under r2 do
#: not need a re-scan.
#: r2 (correction): ``username-at-host`` skips a finding only when its whole
#: span sits inside a complete standalone ``mindie://`` reference token
#: (domain grammar matching documents.DOMAIN_RE; 16- or 64-char lowercase
#: hex id and revision; strict token boundaries). Other rules still apply
#: to the same text. Hex@hex without this scheme is unchanged.
#: r2 (correction): ``ipv6-address`` skips a finding only when the match is
#: inside an unambiguous Python numeric subscript/slice tightly attached to
#: an identifier or ``)`` / ``]`` (for example ``x[:, ::2]``,
#: ``torch.randn(...)[::2]``). Bare ``::2``, standalone ``[::2]``, quoted
#: address literals, and host/URL forms stay reported. Other rules are
#: unchanged. Both corrections are relaxations; keep profile r2.
#: r2 (correction): ``ipv4-address`` skips a finding only when the whole match
#: is exactly a four-part version token in unambiguous package grammar: a
#: pip-check "PACKAGE VERSION requires ..." / "... has requirement ..." line
#: with a plausible distribution name. CIDR, range, and port forms, endpoint
#: words as the name, and bare version-looking tokens stay reported. A
#: relaxation; keep profile r2.
#: r3 (correction): complete generated ``<redacted:rule:12-lowercase-hex>``
#: placeholders cannot contain private identifiers. Ignore only findings wholly
#: contained in those closed tokens; malformed tokens and findings extending
#: beyond a token still go through the same privacy rules. A relaxation; keep r3.
#: r3: mask complete private-key blocks. Missing or mismatched END markers
#: cover the supplied remainder, including blank lines and long fragments.
#: r3 (correction): an IPv4 token cannot end inside a longer dotted number,
#: such as the observed firmware version 7.7.0.10.220. Sentence-final periods
#: remain valid address boundaries. A relaxation; keep r3.
REDACTION_PROFILE = "r3"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    hint: str
    pattern: "re.Pattern[str] | None" = None
    finder: "Callable[[str], Iterable[tuple[int, int]]] | None" = None

    def spans(self, text: str) -> Iterator[tuple[int, int]]:
        if self.pattern is not None:
            for m in self.pattern.finditer(text):
                # Prefer the first capture group when a rule anchors on
                # context (e.g. "--name X" reports only X).
                if m.lastindex:
                    yield m.span(m.lastindex)
                else:
                    yield m.span()
        if self.finder is not None:
            yield from self.finder(text)


_HEX = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_PINNED_SOURCE_URL = re.compile(
    r"(?<![\w:/])https://(?:"
    r"(?:github\.com|gitcode\.com)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[0-9a-f]{40}/[A-Za-z0-9_./-]+"
    r"|gitcode\.com/cann/[A-Za-z0-9_.-]+/blob/master/[A-Za-z0-9_./-]+"
    r"|www\.hiascend\.com/document/detail/[A-Za-z0-9_./-]+"
    r")(?:#L[1-9][0-9]*(?:-L[1-9][0-9]*)?)?"
    r"(?=$|[\s\"'`<>)\],;])"
)


def _mixed_opaque_token(token: str) -> bool:
    return (len(token) >= 32 and not _HEX.fullmatch(token) and not _UUID.fullmatch(token)
            and any(c.isupper() for c in token) and any(c.islower() for c in token)
            and any(c.isdigit() for c in token))


def _high_entropy_tokens(text: str) -> Iterator[tuple[int, int]]:
    """Long mixed-case alphanumeric blobs that are not hashes or uuids.

    Hex digests (commit shas, content_hash) and uuids are structurally
    identifiable and are not credentials, so they are excluded; everything
    else that is >= 32 chars and mixes upper, lower and digits is reported.
    """
    source_urls = [match.span() for match in _PINNED_SOURCE_URL.finditer(text)]
    source_index = 0
    for m in _TOKEN_CANDIDATE.finditer(text):
        tok = m.group()
        while source_index < len(source_urls) and source_urls[source_index][1] <= m.start():
            source_index += 1
        if (source_index < len(source_urls) and source_urls[source_index][0] <= m.start()
                and m.end() <= source_urls[source_index][1]):
            # A commit and an uppercase filename separated by real URL path
            # boundaries are not one opaque token. This is tokenization, not a
            # host allowlist: every segment and every other rule still runs.
            for segment in re.finditer(r"[^/]+", tok):
                if _mixed_opaque_token(segment.group()):
                    yield m.start() + segment.start(), m.start() + segment.end()
        elif _mixed_opaque_token(tok):
            yield m.span()


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_PLACEHOLDER_START = r"(?![<$\{\*%])"  # <host>, $USER, {name}, *, %USERNAME%

_HOST_PREFIXES = (
    "node|host|server|srv|worker|master|machine|ecs|vm|pod|box|k8s|rack|blade|"
    "compute|bastion|jump|login|cluster|remote"
)
# Machine words that, followed by a bare number *with a space*, name a machine
# in prose ("on remote 131", "machine 7"). Deliberately narrower than
# _HOST_PREFIXES: "node 4" and "host 2" are also how people write rank and
# node *indices*, which are load-bearing method detail and must survive.
_MACHINE_WORDS = "remote|machine|box|server|bastion|workstation"
_INTERNAL_TLDS = "local|localdomain|internal|intranet|lan|corp|home|priv|private|localnet"

# Standalone MindIE reference: mindie://<domain>/<id>@<revision>. Domain is the
# same grammar as documents.DOMAIN_RE (duplicated here so the redactor does
# not import loop/storage). Legacy IDs use hex; C5 uses units/unit-* and a
# complete 64-character semantic content digest. Neither accepts suffixes.
# Only a complete token with a strict opening delimiter-or-start and closing
# delimiter-or-end is recognized. Permitted surroundings: whitespace, quotes,
# backticks, Markdown-link parentheses, angle brackets. URI prefixes and any
# joined suffix (path, query, fragment, port, alnum, underscore, dash) fail.
_MINDIE_REF_OPEN = r"(?:^|(?<=[\s\"'`<(]))"
_MINDIE_REF_CLOSE = r"(?:$|(?=[\s\"'`>)]|[.!?,;](?:$|[\s\"'`>)])))"
_MINDIE_REF_TOKEN = re.compile(
    _MINDIE_REF_OPEN
    + r"mindie://"
    r"[a-z][a-z0-9-]{0,63}/"
    r"(?:(?:[a-f0-9]{16}|[a-f0-9]{64})@(?:[a-f0-9]{16}|[a-f0-9]{64})"
    r"|units/unit-[a-z0-9]+(?:-[a-z0-9]+)*@[1-9][0-9]*)"
    + _MINDIE_REF_CLOSE
)

_IPV6_PATTERN = re.compile(
    r"(?<![0-9a-z:])(?:"
    r"(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,7}:"
    r"|(?:[0-9a-f]{1,4}:){1,6}:[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,5}(?::[0-9a-f]{1,4}){1,2}"
    r"|(?:[0-9a-f]{1,4}:){1,4}(?::[0-9a-f]{1,4}){1,3}"
    r"|(?:[0-9a-f]{1,4}:){1,3}(?::[0-9a-f]{1,4}){1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,2}(?::[0-9a-f]{1,4}){1,5}"
    r"|[0-9a-f]{1,4}:(?::[0-9a-f]{1,4}){1,6}"
    r"|:(?:(?::[0-9a-f]{1,4}){1,7}|:)"
    r"|::(?:ffff(?::0{1,4})?:)?(?:" + _OCTET + r"\.){3}" + _OCTET +
    r")(?:%[0-9a-z]+)?(?![0-9a-z:])",
    re.IGNORECASE,
)

_USERNAME_AT_HOST_PATTERN = re.compile(
    r"(?<![\w.+-])[a-z_][\w-]{0,31}@[a-z0-9][\w-]*(?:\.[\w-]+)*(?![\w.-])",
    re.IGNORECASE,
)

_SLICE_ITEM = re.compile(
    r"^(?:\.\.\.|(?:-?\d+)?(?::(?:-?\d+)?(?::(?:-?\d+)?)?)?)$"
)


def _span_in_complete_mindie_ref(text: str, start: int, end: int) -> bool:
    for m in _MINDIE_REF_TOKEN.finditer(text):
        if m.start() <= start and end <= m.end():
            return True
    return False


def _is_numeric_python_slice(inner: str) -> bool:
    """True when ``inner`` is only numeric index/slice items (and ellipsis)."""
    if any(c in inner for c in "'\"/@\\"):
        return False
    if re.search(r"[A-Za-z]", inner):
        return False
    parts = re.split(r"\s*,\s*", inner.strip())
    if not parts or any(p == "" for p in parts):
        return False
    return all(_SLICE_ITEM.fullmatch(p.strip()) for p in parts)


def _in_attached_numeric_subscript(text: str, start: int, end: int) -> bool:
    """IPv6 match sits in ``operand[numeric slice]`` with no intervening space."""
    i = start - 1
    open_idx = None
    while i >= 0:
        c = text[i]
        if c == "]":
            return False
        if c == "[":
            open_idx = i
            break
        i -= 1
    if open_idx is None or open_idx == 0:
        return False
    j = open_idx + 1
    close_idx = None
    while j < len(text):
        if text[j] == "[":
            return False
        if text[j] == "]":
            close_idx = j
            break
        j += 1
    if close_idx is None or not (open_idx < start and end <= close_idx):
        return False
    if not _is_numeric_python_slice(text[open_idx + 1 : close_idx]):
        return False
    prev = text[open_idx - 1]
    # ASCII identifier / expression terminator only. CJK prose such as
    # ``取[::2]`` is not an operand; narrative bare slices stay protected.
    return (prev.isascii() and prev.isalnum()) or prev in ")]_"


def _ipv6_spans(text: str) -> Iterator[tuple[int, int]]:
    for m in _IPV6_PATTERN.finditer(text):
        if _in_attached_numeric_subscript(text, m.start(), m.end()):
            continue
        yield m.span()


def _username_at_host_spans(text: str) -> Iterator[tuple[int, int]]:
    for m in _USERNAME_AT_HOST_PATTERN.finditer(text):
        if _span_in_complete_mindie_ref(text, m.start(), m.end()):
            continue
        yield m.span()


_IPV4_PATTERN = re.compile(
    r"(?<![\w.])(?:" + _OCTET + r"\.){3}" + _OCTET +
    r"(?:\s*-\s*(?:(?:" + _OCTET + r"\.){0,3}" + _OCTET + r"))?"
    r"(?:/\d{1,2})?(?![\w]|\.\d)"
)

# Strong software-version grammar in which a four-part dotted token is a
# public distribution version, not an address: a pip-check line
# ("<dist> <version> requires <dist>..." / "<dist> <version> has requirement
# ..."). The token must be bounded exactly: a CIDR, range, or port
# continuation disqualifies it.
_PACKAGE_NAME = r"[A-Za-z][A-Za-z0-9._-]*"
_PACKAGE_VERSION_CONTEXT = re.compile(
    r"(?<![\w.-])(" + _PACKAGE_NAME + r")[ \t]+(\d+(?:\.\d+){3})"
    r"(?=[ \t]+(?:requires|has[ \t]+requirement)[ \t]+[A-Za-z0-9])"
)

# Endpoint/network words that must not pass as a distribution name.
_NOT_A_PACKAGE = frozenset({
    "host", "hostname", "server", "node", "machine", "endpoint", "target",
    "addr", "address", "ip", "ipv4", "ipv6", "localhost", "gateway", "peer",
    "remote", "client", "url", "uri", "subnet",
})


def _package_version_spans(text: str) -> set[tuple[int, int]]:
    spans = set()
    for m in _PACKAGE_VERSION_CONTEXT.finditer(text):
        if m.group(1).lower() in _NOT_A_PACKAGE:
            continue
        spans.add(m.span(2))
    return spans


def _ipv4_spans(text: str) -> Iterator[tuple[int, int]]:
    versions = _package_version_spans(text)
    for m in _IPV4_PATTERN.finditer(text):
        if m.span() in versions:
            continue
        yield m.span()


_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY)-----")


def _private_key_blocks(text):
    cursor = 0
    for match in _PRIVATE_KEY_BEGIN.finditer(text):
        if match.start() < cursor:
            continue
        end_marker = '-----END ' + match.group(1) + '-----'
        end = text.find(end_marker, match.end())
        cursor = len(text) if end < 0 else end + len(end_marker)
        yield match.start(), cursor


RULES: tuple[Rule, ...] = (
    # --- credentials ------------------------------------------------------
    Rule(
        id="credential-private-key",
        description="private-key block, including incomplete fragments",
        hint="never submit private keys or fragments",
        finder=_private_key_blocks,
    ),
    Rule(
        id="credential-known-format",
        description="token in a well-known provider format",
        hint="never submit credentials, not even expired or partial ones",
        pattern=re.compile(
            r"(?<![A-Za-z0-9_])(?:"
            r"gh[pousr]_[A-Za-z0-9]{20,}"
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_-]{20,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|sk-[A-Za-z0-9_-]{20,}"
            r"|xox[abprs]-[A-Za-z0-9-]{10,}"
            r"|hf_[A-Za-z0-9]{30,}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r")"
        ),
    ),
    Rule(
        id="credential-url-userinfo",
        description="user:password embedded in a URL",
        hint="strip the userinfo part of the URL",
        pattern=re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    ),
    Rule(
        id="credential-bearer",
        description="bearer token",
        hint="never submit credentials",
        pattern=re.compile(r"\bbearer\s+([A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE),
    ),
    Rule(
        id="credential-assignment",
        description="credential-shaped key/value assignment",
        hint="replace the value with a placeholder such as <token>",
        pattern=re.compile(
            r"\b(?:api[_-]?key|apikey|secret(?:[_-]?key)?|access[_-]?key|"
            r"private[_-]?key|token|passw(?:or)?d|passwd|pwd|auth(?:orization)?|"
            r"credentials?)\b\s*[:=]\s*[\"']?" + _PLACEHOLDER_START + r"([^\s,;)\"']{6,})",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="credential-cjk-assignment",
        description="credential provided with a Chinese password or token label",
        hint="never submit credentials from natural-language setup instructions",
        pattern=re.compile(
            r"(?:密码|口令|访问令牌|密钥)\s*(?:都是|均为|是|为|[:：=])\s*"
            r"[\"'`]?([A-Za-z0-9!@#$%^&*()_+={}\[\]:;.,/?~-]{6,})"
        ),
    ),
    # --- network identifiers ------------------------------------------------
    Rule(
        id="mac-address",
        description="MAC address",
        hint="hardware addresses identify a specific machine; remove them",
        pattern=re.compile(
            r"(?<![0-9a-f:.-])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f:.-])",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="ipv6-address",
        description="IPv6 address",
        hint="describe the network role instead of the address",
        finder=_ipv6_spans,
    ),
    Rule(
        id="ipv4-address",
        description="IPv4 address (optionally a range or CIDR)",
        hint="describe the network role instead of the address; a benign "
        "4-part version outside package-version grammar still needs an "
        "explicit allowlist entry",
        finder=_ipv4_spans,
    ),
    # --- people -------------------------------------------------------------
    Rule(
        id="email-address",
        description="e-mail address",
        hint="refer to people by GitHub handle in verified_by, nowhere else",
        pattern=re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])"),
    ),
    Rule(
        id="username-at-host",
        description="user@host login target",
        hint="remove login targets; they name both a user and a machine",
        finder=_username_at_host_spans,
    ),
    Rule(
        id="user-path",
        description="absolute path revealing a user account",
        hint="use a placeholder such as /home/<user>/... or a relative path",
        pattern=re.compile(
            r"(?<![\w/])(?:file://)?(?:/home|/Users|/export/home|/data/home|/root|/mnt/[a-z]/Users)/"
            + _PLACEHOLDER_START + r"[^\s\"'`,;:()\[\]]+"
        ),
    ),
    Rule(
        id="user-path-windows",
        description="Windows profile path revealing a user account",
        hint="use a placeholder such as C:\\Users\\<user>\\...",
        pattern=re.compile(
            r"\b[A-Za-z]:[\\/]Users[\\/]" + _PLACEHOLDER_START + r"([^\\/\s\"']+)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="user-tilde-path",
        description="~user home directory reference",
        hint="use ~ or $HOME without a user name",
        pattern=re.compile(r"(?<![\w/])~(?![/\s<$\{])([a-z_][\w.-]*)", re.IGNORECASE),
    ),
    Rule(
        id="username-assignment",
        description="user name in a key/value assignment",
        hint="remove the account name or replace it with <user>",
        pattern=re.compile(
            r"\b(?:user(?:name)?|login|account|owner|uid)\s*[:=]\s*[\"']?"
            + _PLACEHOLDER_START + r"([a-z_][\w.-]{1,31})",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="username-flag",
        description="user name passed via --user",
        hint="remove the account name or replace it with <user>",
        pattern=re.compile(
            r"(?<![\w-])--(?:user|login)[= ]" + _PLACEHOLDER_START + r"([a-z_][\w-]{1,31})",
            re.IGNORECASE,
        ),
    ),
    # --- machines -----------------------------------------------------------
    Rule(
        id="container-command",
        description="container name in a docker/podman command",
        hint="replace the container name with <container>",
        pattern=re.compile(
            r"\b(?:docker|podman|nerdctl|ctr)\s+(?:container\s+)?"
            r"(?:exec|attach|logs|start|stop|restart|rm|kill|inspect|cp|top|"
            r"stats|port|rename|update|pause|unpause|commit)\s+"
            r"(?:-{1,2}[\w-]+(?:=\S+)?\s+)*" + _PLACEHOLDER_START + r"([A-Za-z0-9][\w.-]+)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="container-name-flag",
        description="container or host name passed via --name/--hostname",
        hint="replace the name with a placeholder",
        pattern=re.compile(
            r"(?<![\w-])--(?:name|hostname|container)[= ]"
            + _PLACEHOLDER_START + r"([A-Za-z0-9][\w.-]+)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="container-name-assignment",
        description="container name in a key/value assignment",
        hint="replace the container name with <container>",
        pattern=re.compile(
            r"\bcontainer(?:[_ -]?(?:name|id))?\s*[:=]\s*[\"']?"
            + _PLACEHOLDER_START + r"(?=[\w.-]*[\d_-])([A-Za-z0-9][\w.-]{2,})",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="hostname-internal-domain",
        description="host name under an internal-looking domain",
        hint="describe the machine role instead of naming it",
        pattern=re.compile(
            r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:"
            + _INTERNAL_TLDS + r")(?![\w-])",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="hostname-numbered",
        description="numbered host name (node-01, worker3, ...)",
        hint="describe the machine role instead of naming it",
        pattern=re.compile(
            r"(?<![\w.-])(?:" + _HOST_PREFIXES + r")[-_]?\d{1,4}"
            r"(?:[a-z][a-z0-9]*)?(?![a-z0-9])",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="internal-machine-identifier",
        description="internal machine slot identifier (remote 131, remote_131, "
        "<run>_131_npu4_...)",
        hint="drop the machine identity and keep the method: device model, device "
        "index, software versions, workload shape",
        pattern=re.compile(
            # "remote 131" / "machine 7": a machine word, one space, a slot number.
            r"(?:(?<![\w.-])(?:" + _MACHINE_WORDS + r") \d{1,4}(?![\w])"
            # "microbenchmark_131_npu4_2026-06-03": a run identifier in which a
            # bare number sits directly beside a device index. The number is
            # the machine slot; "npu4" is the device and is *not* reported.
            r"|(?<=_)\d{1,4}(?=_npu\d))",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="hostname-assignment",
        description="host name in a key/value assignment",
        hint="replace the host name with <host>",
        pattern=re.compile(
            r"\b(?:host(?:name)?|server|node|machine)\s*[:=]\s*[\"']?"
            + _PLACEHOLDER_START + r"(?=[\w.-]*[\d.-])([A-Za-z0-9][\w.-]+)",
            re.IGNORECASE,
        ),
    ),
    # --- identifiers --------------------------------------------------------
    Rule(
        id="employee-id",
        description="employee-style identifier (letter(s) + 8 digits)",
        hint="remove personal identifiers",
        pattern=re.compile(r"(?<![\w-])[a-z]{1,2}\d{8}(?![\w-])", re.IGNORECASE),
    ),
    Rule(
        id="ticket-id",
        description="ticket / tracker identifier (ABC-1234, DTS...)",
        hint="internal tickets are not followable evidence; reference a public "
        "issue, PR or commit instead",
        pattern=re.compile(r"(?<![\w-])(?:[A-Z][A-Z0-9]{1,9}-\d{2,7}|DTS\d{10,16})(?![\w-])"),
    ),
    Rule(
        id="credential-high-entropy",
        description="long mixed-case alphanumeric blob (possible secret)",
        hint="if this is not a secret, allowlist it explicitly",
        finder=_high_entropy_tokens,
    ),
)

RULE_IDS = tuple(r.id for r in RULES)


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #

#: Exact (case-insensitive) values that are public by construction.
BUILTIN_ALLOWLIST: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "0.0.0.0",
        "127.0.0.1",
        "255.255.255.0",
        "255.255.255.255",
        "::",
        "::1",
        "localhost",
        "root",
        "SHA-224",
        "SHA-256",
        "SHA-384",
        "SHA-512",
        "UTF-16",
        "UTF-32",
        "ISO-8859",
        "git@github.com",
        # Prose words that follow "password:" / "token:" in a sentence and are
        # not values: "the token: expired" is a symptom, not a credential.
        "expired", "required", "missing", "invalid", "rejected", "redacted",
        "omitted", "unset", "unknown", "changed", "rotated", "present", "absent",
        "[REDACTED_SECRET]",
        "correct", "incorrect", "mismatch", "needed", "ignored", "accepted",
    )
)

#: Regular expressions (full match, case-insensitive) for public value classes.
BUILTIN_ALLOW_PATTERNS: tuple[str, ...] = (
    r"192\.0\.2\.\d{1,3}(?:\s*-\s*[\d.]+)?(?:/\d{1,2})?",  # RFC 5737 TEST-NET-1
    r"198\.51\.100\.\d{1,3}(?:\s*-\s*[\d.]+)?(?:/\d{1,2})?",  # RFC 5737 TEST-NET-2
    r"203\.0\.113\.\d{1,3}(?:\s*-\s*[\d.]+)?(?:/\d{1,2})?",  # RFC 5737 TEST-NET-3
    r"2001:0?db8:[0-9a-f:]*",  # RFC 3849 documentation prefix
    r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}",  # loopback
    r"[\w.+-]+@(?:[\w-]+\.)*example\.(?:com|org|net)",  # RFC 2606 e-mail
    r"(?:[\w-]+\.)*example\.(?:com|org|net|local|internal|lan|home|corp|priv|private|localdomain|localnet|intranet)",
    r"[\w.+-]+@(?:users\.)?noreply\.github\.com",
    r"RFC-\d+",
    r"aclnn[A-Za-z0-9_/+.-]+",
    r"E[A-Z]\d{4}-[A-Za-z0-9_.-]+",
    r"G[1-4]_[a-z0-9_]+",
    r"<[^<>]+>",  # <placeholder>
)
