"""Canonical ``mindie-entry/2`` public entry documents.

One Markdown file per entry: a small YAML frontmatter header (schema,
entry_id, domain, kind, title, summary, optional conditions) and the detailed
body as the Markdown content. There is no separate summary artifact and no
public revision/producers/sources/status/retirement metadata: the content
``revision`` is an internal fingerprint computed during parse/write as the
SHA256 of the canonical sorted compact UTF-8 JSON of the public semantic
fields (including the detailed body and the normalized — possibly empty —
conditions), excluding local ownership and state. Draft ownership lives in a
private entry-owner relation, never in the Markdown. Withdrawal is deletion
from the published tree, not a tombstone field. ``conditions`` carries only
known relevant software versions or source commits; hardware, configuration,
input parameters and documentation/code/issue citations stay in the body.
Files are canonical LF bytes; hashing and Git commits see exactly what was
rendered. Duplicate YAML keys and unknown fields are rejected so canonical
identity is never ambiguous, and an unknown schema fails loudly.

Only safe YAML is used: ``safe_load``/``safe_dump`` never execute constructors.
"""

from __future__ import annotations

import hashlib
import json
import re

import yaml

SCHEMA = "mindie-entry/2"
KINDS = ("knowledge", "experience")

MAX_FILE_BYTES = 128 * 1024
MAX_TITLE = 240
MAX_SUMMARY_BYTES = 2048
MAX_BODY_BYTES = 64 * 1024
MAX_CONDITION_KEY = 128
MAX_CONDITION_VALUE = 512

DOMAIN_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
HEX_RE = re.compile(r"[0-9a-f]{64}")

# Internal normalized document fields. ``revision`` is computed, never parsed
# from or rendered into the public file.
FIELDS = (
    "schema",
    "entry_id",
    "revision",
    "domain",
    "kind",
    "title",
    "summary",
    "conditions",
    "content",
)

PUBLIC_HEADER_FIELDS = (
    "schema",
    "entry_id",
    "domain",
    "kind",
    "title",
    "summary",
    "conditions",
)

_OBSERVATION_HEADING = "## Later observations"


class DraftFull(ValueError):
    """The merged draft reached the 64 KiB storage envelope; stop expanding."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def revision_of(doc: dict) -> str:
    """Content revision: SHA256 of the canonical JSON of the public fields."""
    return digest({key: doc[key] for key in FIELDS if key != "revision"})


def _text(value, name, limit, *, nonempty=True):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip() if nonempty else value
    if nonempty and not value:
        raise ValueError(f"{name} must be nonempty")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _bytes(value, name, limit):
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{name} exceeds {limit} UTF-8 bytes")
    return value


def validate(doc: dict) -> dict:
    """Structural validation of one entry document; returns it unchanged.

    Text fields must already be canonical (stripped): a document whose body
    carries leading/trailing whitespace is rejected rather than silently
    rewritten, so ``render_entry(parse_entry(x))`` and the revision digest can
    never disagree about what the bytes are."""
    if not isinstance(doc, dict) or set(doc) != set(FIELDS):
        raise ValueError("entry must contain exactly the mindie-entry/2 fields")
    if doc["schema"] != SCHEMA:
        raise ValueError("unsupported entry schema")
    if not isinstance(doc["entry_id"], str) or not HEX_RE.fullmatch(doc["entry_id"]):
        raise ValueError("entry_id must be a 64-character hex identity")
    if not isinstance(doc["revision"], str) or not HEX_RE.fullmatch(doc["revision"]):
        raise ValueError("revision must be a 64-character hex digest")
    if not isinstance(doc["domain"], str) or not DOMAIN_RE.fullmatch(doc["domain"]):
        raise ValueError("invalid domain")
    if doc["kind"] not in KINDS:
        raise ValueError("kind must be knowledge or experience")
    _text(doc["title"], "title", MAX_TITLE)
    if doc["title"] != doc["title"].strip():
        raise ValueError("title must be canonical (no surrounding whitespace)")
    if not isinstance(doc["summary"], str):
        raise ValueError("summary must be text")
    if doc["summary"] != doc["summary"].strip():
        raise ValueError("summary must be canonical (no surrounding whitespace)")
    _bytes(doc["summary"], "summary", MAX_SUMMARY_BYTES)
    if not doc["summary"]:
        raise ValueError("summary must be nonempty")
    conditions = doc["conditions"]
    if not isinstance(conditions, dict):
        raise ValueError("conditions must be an object")
    for key, value in conditions.items():
        _text(key, "condition key", MAX_CONDITION_KEY)
        _text(value, f"condition {key!r}", MAX_CONDITION_VALUE)
        if key != key.strip() or value != value.strip():
            raise ValueError("conditions must be canonical text")
    if not isinstance(doc["content"], str) or not doc["content"].strip():
        raise ValueError("content must be nonempty text")
    if doc["content"] != doc["content"].strip():
        raise ValueError("content must be canonical (no surrounding whitespace)")
    _bytes(doc["content"], "content", MAX_BODY_BYTES)
    if revision_of(doc) != doc["revision"]:
        raise ValueError("revision does not match the canonical fields")
    return doc


def make_entry(*, entry_id, domain, kind, title, summary, content,
               conditions=None) -> dict:
    """Build a validated entry, computing its revision from the fields."""
    doc = dict(
        schema=SCHEMA,
        entry_id=entry_id,
        revision="0" * 64,
        domain=domain,
        kind=kind,
        title=title.strip(),
        summary=summary.strip(),
        conditions=dict(conditions or {}),
        content=content.strip(),
    )
    doc["revision"] = revision_of(doc)
    return validate(doc)


def render_entry(doc: dict) -> str:
    """Canonical Markdown: YAML header without the private revision, then the
    body. Empty conditions are omitted rather than rendered as a placeholder."""
    validate(doc)
    header = {key: doc[key] for key in PUBLIC_HEADER_FIELDS if key != "conditions"}
    if doc["conditions"]:
        header["conditions"] = dict(doc["conditions"])
    frontmatter = yaml.safe_dump(
        header, sort_keys=True, allow_unicode=True, width=10**6
    )
    return f"---\n{frontmatter}---\n\n{doc['content'].strip()}\n"


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys outright."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in mapping:
                raise ValueError(f"duplicate YAML key {key!r}")
            mapping[key] = self.construct_object(value_node, deep=True)
        return mapping


def parse_entry(markdown) -> dict:
    """Parse and validate one canonical entry document.

    Accepts ``str`` or UTF-8 ``bytes``. Byte and malformed-file limits apply;
    CRLF input is rejected so hashing always sees canonical LF bytes. The
    internal revision is computed from the parsed public fields; any revision,
    ownership or status field in the file is an unknown field and fails loudly.
    """
    if isinstance(markdown, bytes):
        if len(markdown) > MAX_FILE_BYTES:
            raise ValueError("entry file exceeds the byte limit")
        try:
            text = markdown.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("entry file is not valid UTF-8") from None
    elif isinstance(markdown, str):
        if len(markdown.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("entry file exceeds the byte limit")
        text = markdown
    else:
        raise ValueError("entry document must be text or bytes")
    if "\r" in text:
        raise ValueError("entry files must be canonical LF bytes")
    if not text.startswith("---\n"):
        raise ValueError("entry must start with a YAML frontmatter block")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("unterminated YAML frontmatter block")
    try:
        header = yaml.load(text[4:end], Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, TypeError):
        raise ValueError("malformed YAML frontmatter") from None
    if not isinstance(header, dict):
        raise ValueError("frontmatter must be a mapping")
    if any(not isinstance(key, str) for key in header):
        raise ValueError("frontmatter field names must be text")
    unknown = set(header) - set(PUBLIC_HEADER_FIELDS)
    if unknown:
        raise ValueError(f"unknown entry fields: {sorted(unknown)}")
    if header.get("schema") != SCHEMA:
        raise ValueError("unsupported entry schema")
    if set(PUBLIC_HEADER_FIELDS) - {"conditions"} - set(header):
        raise ValueError("missing required entry fields")
    doc = dict(header)
    if "conditions" in doc and not isinstance(doc["conditions"], dict):
        raise ValueError("conditions must be a mapping when present")
    doc.setdefault("conditions", {})
    doc["content"] = text[end + len("\n---\n") :].strip()
    if not doc["content"]:
        raise ValueError("entry body must be nonempty")
    doc["revision"] = revision_of(doc)
    return validate(doc)


def append_observation(doc: dict, addition: str, *, marker: str) -> tuple[dict, bool]:
    """Append one self-contained observation/correction under the bounded
    ``## Later observations`` section. Idempotent on ``marker``: an already
    appended increment returns ``(doc, False)``. Never rewrites or deletes
    earlier body. Raises :class:`DraftFull` at the 64 KiB envelope."""
    if not re.fullmatch(r"[0-9a-f]{16,64}", marker or ""):
        raise ValueError("observation marker must be a hex increment identity")
    addition = (addition or "").strip()
    if not addition:
        raise ValueError("observation must be nonempty")
    if f"<!-- observation:{marker} -->" in doc["content"]:
        return doc, False
    block = f"<!-- observation:{marker} -->\n\n{addition}"
    if _OBSERVATION_HEADING in doc["content"]:
        content = doc["content"].rstrip() + "\n\n" + block
    else:
        content = doc["content"].rstrip() + f"\n\n{_OBSERVATION_HEADING}\n\n" + block
    if len(content.encode("utf-8")) > MAX_BODY_BYTES:
        raise DraftFull("draft full: the 64 KiB body envelope is reached")
    updated = dict(doc, content=content)
    updated["revision"] = revision_of(updated)
    return validate(updated), True
