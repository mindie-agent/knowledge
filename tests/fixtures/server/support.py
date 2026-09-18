"""Helpers shared by the tests/test_server_*.py suite.

Kept next to the fixtures it points at. The tests import it by adding this
directory to sys.path, so the suite runs from any working directory and does
not depend on the host's environment variables: every config here is built
with an explicit empty ``env``.
"""

from __future__ import annotations

import pathlib
from typing import Any, Mapping

FIXTURES = pathlib.Path(__file__).resolve().parent
REPO = FIXTURES.parent.parent.parent

from mindie_knowledge.server.layers import ServiceConfig, load_config

def _layer_spec(value: Any, fixture_dir: str) -> dict[str, Any]:
    if value is True:
        return {"roots": [fixture_dir]}
    if value is False:
        return {"enabled": False}
    if value == "missing":
        return {"roots": ["no-such-directory"]}
    return {"roots": [str(value)]}


def build_config(
    *,
    shared: Any = True,
    project: Any = True,
    candidate: Any = True,
    identity: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> ServiceConfig:
    """Build a ServiceConfig over the fixture layers.

    Each layer argument accepts ``True`` (use the fixture directory),
    ``False`` (disabled), ``"missing"`` (configured at a path that does not
    exist) or an explicit path.
    """

    mapping: dict[str, Any] = {
        "backend": "memory",
        "layers": {
            "shared": _layer_spec(shared, "shared"),
            "project": _layer_spec(project, "project"),
            "candidate": _layer_spec(candidate, "candidate"),
        }
    }
    if identity:
        mapping["identity"] = dict(identity)
    if isinstance(candidate, (str, pathlib.Path)):
        mapping["state_root"] = str(pathlib.Path(candidate) / "instance")
    return load_config(mapping, env=dict(env or {}), base_dir=FIXTURES)
