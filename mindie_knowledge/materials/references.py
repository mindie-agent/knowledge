"""Typed public references: current navigation, immutable blocks and feedback.

Only complete canonical identities cross the reading interface. Observation
references name a task revision for feedback; they never select a historical
body or silently redirect a read to the current package.
"""
from __future__ import annotations

import re

_DOMAIN = r"[a-z][a-z0-9-]{0,63}"
_ID = r"[0-9a-f]{64}"
_BASE = rf"mindie://(?P<domain>{_DOMAIN})/(?P<task_id>{_ID})"
_TASK = re.compile(_BASE + r"\Z")
_BLOCK = re.compile(_BASE + rf"/blocks/(?P<block_id>{_ID})@(?P<sha256>{_ID})\Z")
_FEEDBACK = re.compile(_BASE + rf"@(?P<revision>{_ID})\Z")


class ReadReferenceError(ValueError):
    """A malformed or unavailable requested object, never substituted bytes."""

    def __init__(self, code, message, *, read_ref=None):
        super().__init__(message)
        self.code = code
        self.read_ref = read_ref


class MaterialReadError(RuntimeError):
    """Required current material is unreadable or inconsistent."""

    code = "material_corrupt"

    def __init__(self, message, *, read_ref=None):
        super().__init__(message)
        self.read_ref = read_ref


def _field(value, expression, name):
    if not isinstance(value, str) or re.fullmatch(expression, value) is None:
        raise ValueError("invalid " + name)
    return value


def task_ref(domain, task_id):
    return "mindie://" + _field(domain, _DOMAIN, "domain") + "/" + _field(task_id, _ID, "task identity")


def block_ref(domain, task_id, block_id, sha256):
    return (task_ref(domain, task_id) + "/blocks/" + _field(block_id, _ID, "block identity")
            + "@" + _field(sha256, _ID, "block file digest"))


def feedback_ref(domain, task_id, revision):
    return task_ref(domain, task_id) + "@" + _field(revision, _ID, "observed task revision")


def _matched(pattern, ref, domain):
    match = pattern.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return None
    value = match.groupdict()
    if domain is not None and value["domain"] != domain:
        raise ReadReferenceError("reference_invalid", "Reference is outside the selected domain.")
    return value


def parse_read_ref(ref, *, domain=None):
    for kind, pattern in (("task", _TASK), ("block", _BLOCK)):
        value = _matched(pattern, ref, domain)
        if value is not None:
            return dict(kind=kind, **value)
    observation = _matched(_FEEDBACK, ref, domain)
    if observation is not None:
        raise ReadReferenceError(
            "reference_invalid", "An observed task revision is for feedback only; use read_ref for current navigation.",
            read_ref=task_ref(observation["domain"], observation["task_id"]),
        )
    raise ReadReferenceError("reference_invalid", "Use a complete task navigation or block reference returned by query/read.")


def parse_feedback_ref(ref, *, domain=None):
    value = _matched(_FEEDBACK, ref, domain)
    if value is None:
        raise ReadReferenceError("reference_invalid", "Feedback requires the exact feedback_ref returned with the observed result.")
    return dict(kind="feedback", **value)
