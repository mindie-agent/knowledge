"""Errors for the contribution path.

None of these is allowed to fail a local capture. Callers that run after
capture must record a pending record and return.
"""

from __future__ import annotations


class ContributionError(Exception):
    """Base error for the contribution package."""


class IdentityError(ContributionError):
    """A content digest or other non-Git token was used as a Git identity."""


class DocumentRejected(ContributionError):
    """The Markdown is missing a title or a non-empty body."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class TransportError(ContributionError):
    """GitHub, git, or network failed. The pending record remains recoverable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)
