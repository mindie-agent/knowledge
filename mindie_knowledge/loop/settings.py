"""Shared community-contribution settings (``mindie-community-config/1``).

One local JSON file shared by the adapter and the knowledge runtime. It is
re-read from disk at every gate — before transcript reading, before spawning a
model and before any outbound write — so flipping ``enabled`` or bumping
``generation`` takes effect without restarting anything. Missing, malformed or
unconfigured state fails **closed** for capture and contribution, but never
blocks read-only retrieval, plugin updates or knowledge sync.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

SCHEMA = "mindie-community-config/1"
DEFAULT_IDLE_SECONDS = 300
MAX_IDLE_SECONDS = 86400
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class CommunitySettings:
    """One read of the shared settings file. Treat instances as ephemeral."""

    def __init__(self, path, data, error=None):
        self.path = Path(path) if path else None
        self.raw = data if isinstance(data, dict) else {}
        self.error = error
        self.schema_ok = error is None and self.raw.get("schema") == SCHEMA
        enabled = self.schema_ok and self.raw.get("enabled") is True
        self.enabled = bool(enabled)
        generation = self.raw.get("generation")
        # The generation is a nonempty opaque string shared by all components.
        self.generation = generation if isinstance(generation, str) and generation else None
        enabled_at = self.raw.get("enabled_at")
        self.enabled_at = (
            enabled_at
            if type(enabled_at) in (int, float) and enabled_at > 0
            else None
        )
        repository = self.raw.get("repository")
        self.repository = (
            repository
            if isinstance(repository, str) and _REPOSITORY_RE.fullmatch(repository)
            else None
        )
        branch = self.raw.get("branch", "main")
        self.branch = branch if isinstance(branch, str) and branch else "main"
        roots = self.raw.get("project_roots")
        self.project_roots = []
        if isinstance(roots, list):
            for item in roots:
                if isinstance(item, str) and item.strip() and Path(item).is_absolute():
                    try:
                        self.project_roots.append(
                            Path(item).expanduser().resolve(strict=False)
                        )
                    except OSError:
                        continue
        idle = self.raw.get("idle_seconds", DEFAULT_IDLE_SECONDS)
        self.idle_seconds = (
            idle if type(idle) is int and 30 <= idle <= MAX_IDLE_SECONDS
            else DEFAULT_IDLE_SECONDS
        )

    @property
    def configured(self):
        return self.schema_ok

    def allows_capture(self):
        """The whole capture/extraction/sanitization chain needs an explicit,
        well-formed on: schema, enabled, opaque generation, a finite positive
        authorization boundary and at least one absolute canonical root. Any
        malformed enabled config authorizes nothing."""
        return (
            self.schema_ok
            and self.enabled
            and self.generation is not None
            and self.enabled_at is not None
            and bool(self.project_roots)
        )

    def in_scope(self, candidate):
        """Canonical containment check against the configured project roots.

        Scope checking happens before any path is opened. An enabled config
        without project roots authorizes nothing (fail closed).
        """
        if not self.project_roots or not candidate:
            return False
        try:
            target = Path(candidate).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return False
        for root in self.project_roots:
            if target == root or root in target.parents:
                return True
        return False

    def as_dict(self):
        """Private settings handed to the community package per contract §4.

        Every key from the shared file survives the handoff — extension keys
        (fork/account/bot/transaction/operation/transport/…) are private
        validated configuration that other components own; dropping one would
        silently break a sibling. ``config_path`` is added for the live
        revocation reread and must never appear in public batch content."""
        data = dict(self.raw)
        data["schema"] = self.raw.get("schema")
        data["enabled"] = self.enabled
        data["generation"] = self.generation
        data["enabled_at"] = self.enabled_at
        data["repository"] = self.repository
        data["branch"] = self.branch
        data["project_roots"] = [str(root) for root in self.project_roots]
        data["idle_seconds"] = self.idle_seconds
        data["config_path"] = str(self.path) if self.path else None
        return data

    def public_status(self):
        """Secret-free operational status."""
        return dict(
            configured=self.schema_ok,
            enabled=self.enabled,
            generation_set=self.generation is not None,
            repository=self.repository,
            branch=self.branch,
            project_roots=len(self.project_roots),
            idle_seconds=self.idle_seconds,
            enabled_at=self.enabled_at,
            error=self.error,
        )


def load(path) -> CommunitySettings:
    """Read the settings file once. Never raises; failures mean disabled."""
    if not path:
        return CommunitySettings(None, {}, error="community_config is not configured")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return CommunitySettings(path, {}, error="settings file is unreadable")
    if len(raw) > 64 * 1024:
        return CommunitySettings(path, {}, error="settings file exceeds the byte limit")
    try:
        data = json.loads(raw)
    except ValueError:
        return CommunitySettings(path, {}, error="settings file is not valid JSON")
    if not isinstance(data, dict):
        return CommunitySettings(path, {}, error="settings file must hold one object")
    if data.get("schema") != SCHEMA:
        return CommunitySettings(path, data, error="unsupported settings schema")
    return CommunitySettings(path, data)


def from_engine_config(config: dict) -> CommunitySettings:
    """Resolve the shared settings from one engine configuration document."""
    return load((config or {}).get("community_config"))


def write(path, *, enabled, repository, project_roots, branch="main",
          idle_seconds=DEFAULT_IDLE_SECONDS, previous=None, **extensions):
    """First-configuration/settings-mutation helper used by tests and setup.

    Every mutation sets a fresh nonempty opaque string ``generation`` so
    concurrent runtimes observe the change on their next read. ``enabled_at``
    is set only on an off->on edge. Extension keys already present in the file
    (fork/bot/transaction/…) are preserved across toggles; one component must
    not silently delete another component's private configuration."""
    previous = previous if isinstance(previous, dict) else {}
    try:
        on_disk = json.loads(Path(path).read_text())
        if isinstance(on_disk, dict):
            previous = {**on_disk, **previous}
    except (OSError, ValueError):
        pass
    was_enabled = previous.get("enabled") is True
    data = dict(previous)
    data.update({
        "schema": SCHEMA,
        "enabled": bool(enabled),
        "generation": secrets.token_hex(16),
        "enabled_at": (
            time.time() if enabled and not was_enabled
            else previous.get("enabled_at") if enabled else None
        ),
        "repository": repository,
        "branch": branch,
        "project_roots": [str(Path(root).expanduser().resolve(strict=False))
                          for root in project_roots],
        "idle_seconds": idle_seconds,
    })
    for key, value in extensions.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    from mindie_knowledge.markdown import _atomic_write_text

    _atomic_write_text(target, json.dumps(data, indent=2) + "\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return load(target)
