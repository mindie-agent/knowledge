"""Error types for the distribution module.

Every error carries an actionable ``reason`` so a periodic sync caller can
surface *why* without a traceback, and existing queries keep working on the
previous version.
"""

from __future__ import annotations


class DistributionError(Exception):
    """Base class. ``reason`` is a short actionable, user-readable message."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SourceUnavailable(DistributionError):
    """The release source cannot be read (offline, missing, disabled)."""


class CorruptPack(DistributionError):
    """The downloaded pack or release manifest failed integrity checks."""


class IncompatiblePack(DistributionError):
    """The pack does not match the local model/version contract."""


class SwitchInProgress(DistributionError):
    """Another live process is currently importing/switching a version."""


class BuildError(DistributionError):
    """The OVPack build from fixed Git content failed."""


class ReleaseError(DistributionError):
    """Release adaptation failed (including disabled real publishing)."""


class ImportFailed(DistributionError):
    """The native import into the local instance failed; old version kept."""
