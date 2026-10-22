"""Temporary summary profiles share the process tree's cleanup owner.

[Role] Temporary summary profiles share the process tree cleanup owner.
[Invariants] Only the core deletes its own fresh root; a declared job/parent mismatch raises instead of adopting a foreign directory.
[Timeouts & Locks] Ownership checks use bounded stat/pipe operations; no long-lived lock is retained.
[Side-Effect Policy] Creates and removes a private scratch directory for one summary worker run.

The declaration is an internal parent/worker protocol, never user configuration
or a worker-provided deletion target. Only the core deletes its own fresh root.
"""
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

ENVIRONMENT_KEY = "MINDIE_SUMMARY_SCRATCH"


class ScratchOwnershipError(ValueError):
    """An incomplete owner contract must fail before invoking a model."""

    mindie_category = "configuration"


def create_root():
    root = Path(tempfile.gettempdir()) / ("mindie-summary-owner-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    return root


def job_name(root):
    return "Local\\" + root.name if os.name == "nt" else None


def declaration(root):
    return json.dumps(dict(schema=1, owner=os.getpid(), path=str(root), job=job_name(root)))


def create_profile(*, prefix):
    """Return a private profile and whether the core owns its final deletion."""
    raw = os.environ.get(ENVIRONMENT_KEY)
    grouped = os.environ.get("MINDIE_MAINTENANCE_GROUP")
    owner = os.environ.get("MINDIE_MAINTENANCE_OWNER")
    if raw is None and grouped is None and owner is None:
        return tempfile.mkdtemp(prefix=prefix), False
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"schema", "owner", "path", "job"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or type(value["owner"]) is not int or value["owner"] <= 0
                or grouped != "1" or owner != str(value["owner"])
                or not isinstance(value["path"], str)):
            raise ValueError
        root = Path(value["path"])
        if os.name == "nt":
            from mindie_knowledge.windows_process import in_owned_job
            name = value["job"]
            if (not isinstance(name, str) or name != job_name(root)
                    or not name.startswith("Local\\mindie-summary-owner-")
                    or not in_owned_job(name)):
                raise ValueError
        elif (value["job"] is not None or os.getppid() != value["owner"]
              or os.getpgrp() != os.getpid()):
            raise ValueError
        info = root.lstat()
        if (not root.is_absolute() or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        raise ScratchOwnershipError("summary scratch ownership is unavailable") from None
    return tempfile.mkdtemp(prefix=prefix, dir=root), True
