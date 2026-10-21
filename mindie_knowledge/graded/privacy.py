"""[Role] Scan structured C5 metadata without mistaking Git locators for SSH.

[Invariants] Only recognized source_locator values are split into separately
scanned repository, commit and path fields. Arbitrary strings keep every rule.
[Timeouts & Locks] Pure in-memory deterministic scanning.
[Side-Effect Policy] Reports findings only; never modifies source bytes.
"""
from ..redact import scan_tree
from .units import is_graded_unit_path, locator_parts


def _scan_locator(value):
    parts = locator_parts(value)
    if parts is None:
        return value
    if "path" in parts:
        return {**parts, "path": parts["path"].split("/")}
    return parts


def _scan_unit_path(value):
    if isinstance(value, str) and is_graded_unit_path(value if value.endswith(".md") else f"{value}.md"):
        return value.split("/")
    return value


def metadata_findings(value, path):
    def visit(item):
        if isinstance(item, dict):
            out = {}
            for key, member in item.items():
                if key == "source_locator" and isinstance(member, str):
                    out[key] = _scan_locator(member)
                elif key in ("evidence_refs", "validated_revisions") and isinstance(member, list):
                    out[key] = [_scan_locator(ref) if isinstance(ref, str) else visit(ref) for ref in member]
                elif key == "path" and isinstance(member, str):
                    out[key] = _scan_unit_path(member)
                elif key == "id_path" and isinstance(member, dict):
                    out[key] = {k: _scan_unit_path(v) for k, v in member.items()}
                else:
                    out[key] = visit(member)
            return out
        if isinstance(item, list):
            return [visit(member) for member in item]
        return item
    return scan_tree(visit(value), path=(path,))
