"""Title + body Markdown for public contribution.

This is a join point with local capture, not a second document store.
Capture writes ordinary Markdown; contribution uses the same parser.
Contribution reads that shape and writes the same shape to the public copy.
Path + Git SHA identify published content. The content digest is only for
idempotency, de-duplication, and integrity — it must not be passed off as a
Git identity.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from mindie_knowledge.markdown import parse_markdown, render_markdown
from mindie_knowledge.contribution.errors import DocumentRejected, IdentityError

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
DIGEST_PREFIX = "sha256:"


def require_title_body(text: str) -> tuple[str, str]:
    title, body = parse_markdown(text)
    if not body:
        raise DocumentRejected(["content is required"])
    return title, body


def slugify(text: str) -> str:
    slug = _SLUG_UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:60] or "contribution"


def canonical_bytes(title: str, body: str) -> bytes:
    return render_markdown(title, body).encode("utf-8")


def content_digest(title: str, body: str) -> str:
    digest = hashlib.sha256(canonical_bytes(title, body)).hexdigest()
    return DIGEST_PREFIX + digest


def digest_token(digest: str) -> str:
    text = str(digest or "")
    if text.startswith(DIGEST_PREFIX):
        text = text[len(DIGEST_PREFIX) :]
    if len(text) != 64:
        raise IdentityError("malformed content digest")
    try:
        int(text, 16)
    except ValueError as exc:
        raise IdentityError("malformed content digest") from exc
    return text.lower()


def require_git_sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise IdentityError("expected a Git commit SHA")
    return value.lower()


def branch_for_digest(digest: str) -> str:
    token = digest_token(digest)
    return f"contrib/{token[:12]}"


def public_filename(digest: str, title: str) -> str:
    token = digest_token(digest)
    return f"{token[:12]}-{slugify(title)}.md"


@dataclass(frozen=True)
class MarkdownDocument:
    title: str
    body: str
    digest: str

    @classmethod
    def from_text(cls, text: str) -> "MarkdownDocument":
        title, body = require_title_body(text)
        return cls(title=title, body=body, digest=content_digest(title, body))

    def render(self) -> str:
        return render_markdown(self.title, self.body)
