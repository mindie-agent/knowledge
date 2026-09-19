"""Override the package diagnostics autouse fixture.

G1 must not edit original tests/conftest.py or install diagnostics into the
user environment. These checks are local Store/CLI/evidence only.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_diagnostics():
    yield None
