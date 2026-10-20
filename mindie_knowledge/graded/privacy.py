"""[Role] Scan structured C5 metadata without mistaking Git locators for SSH.

[Invariants] Only recognized source_locator values are split into separately
scanned repository, commit and path fields. Arbitrary strings keep every rule.
[Timeouts & Locks] Pure in-memory deterministic scanning.
[Side-Effect Policy] Reports findings only; never modifies source bytes.
"""
from ..redact import scan_tree
from .units import locator_parts


def metadata_findings(value, path):
    def visit(item):
        if isinstance(item, dict):
            return {key: (locator_parts(member) or member) if key == "source_locator" and isinstance(member, str)
                    else visit(member) for key, member in item.items()}
        if isinstance(item, list):
            return [visit(member) for member in item]
        return item
    return scan_tree(visit(value), path=(path,))
