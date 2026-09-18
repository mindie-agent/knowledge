"""Build a redacted public Markdown copy without mutating the candidate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mindie_knowledge import redact
from mindie_knowledge.contribution.documents import MarkdownDocument, public_filename, render_markdown
from mindie_knowledge.contribution.errors import DocumentRejected


def _mask_text(text: str, allow: redact.Allowlist | None = None) -> tuple[str, list[redact.Finding]]:
    """Replace leaked spans with placeholders using the published ruleset."""

    allow = allow or redact.Allowlist()
    claimed: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []
    findings: list[redact.Finding] = []
    for rule in redact.RULES:
        for start, end in rule.spans(text):
            if start == end:
                continue
            if any(claimed_start < end and start < claimed_end for claimed_start, claimed_end in claimed):
                continue
            value = text[start:end]
            claimed.append((start, end))
            if allow.is_allowed(value):
                continue
            placeholder = f"[redacted:{rule.id}]"
            replacements.append((start, end, placeholder))
            findings.append(redact.Finding(path="<markdown>", rule=rule.id, value=value, hint=rule.hint))
    out = text
    for start, end, placeholder in sorted(replacements, key=lambda item: item[0], reverse=True):
        out = out[:start] + placeholder + out[end:]
    return out, findings


@dataclass
class PublicCopy:
    document: MarkdownDocument
    text: str
    path: Path | None
    redacted_spans: list[str] = field(default_factory=list)
    profile: str = redact.REDACTION_PROFILE
    blocked: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.document.title if not self.blocked else "",
            "digest": self.document.digest if not self.blocked else "",
            "redaction_profile": self.profile,
            "redacted_spans": list(self.redacted_spans),
            "blocked": self.blocked,
            "path": str(self.path) if self.path is not None else None,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def prepare_public_copy(
    source_text: str,
    *,
    public_root: Path | None = None,
    allow: redact.Allowlist | None = None,
) -> PublicCopy:
    """Return a public Markdown copy. The caller must not write back to the candidate."""

    try:
        original = MarkdownDocument.from_text(source_text)
    except DocumentRejected as exc:
        return PublicCopy(
            document=MarkdownDocument(title="", body="", digest=""),
            text="",
            path=None,
            blocked=True,
            reason=str(exc),
        )
    rendered = original.render()
    masked, findings = _mask_text(rendered, allow)
    leftover = redact.scan_text(masked, allow)
    if leftover:
        return PublicCopy(
            document=original,
            text="",
            path=None,
            redacted_spans=[item.rule for item in findings],
            blocked=True,
            reason="redaction could not produce a clean public copy",
        )
    try:
        public_doc = MarkdownDocument.from_text(masked)
    except DocumentRejected:
        return PublicCopy(
            document=original,
            text="",
            path=None,
            redacted_spans=[item.rule for item in findings],
            blocked=True,
            reason="redaction removed the title or body",
        )
    public_text = render_markdown(public_doc.title, public_doc.body)
    dest: Path | None = None
    if public_root is not None:
        dest = Path(public_root) / public_filename(public_doc.digest, public_doc.title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(public_text, encoding="utf-8", newline="\n")
    return PublicCopy(
        document=public_doc,
        text=public_text,
        path=dest,
        redacted_spans=[item.rule for item in findings],
    )
