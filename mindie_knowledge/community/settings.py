"""Community settings: validated view of the shared local JSON config.

The file carries schema ``mindie-community-config/1``. Tokens never live in
settings; only the *name* of an environment variable may be configured. A
missing/disabled/malformed sharing config fails closed for publication: the
caller receives ``disabled`` and no Git mutation or outbound request happens.
"""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .common import (
    DEFAULT_TRANSACTION_SECONDS,
    CommunityError,
    check_account,
    check_repository,
)

MAX_CONFIG_BYTES = 64 * 1024

DEFAULTS = {
    "branch": "main",
    "idle_seconds": 300,
    "transaction_seconds": DEFAULT_TRANSACTION_SECONDS,
    "operation_limit": 60,
    "transport": "gh",
}


class SharingDisabled(CommunityError):
    def __init__(self, detail: str = "community sharing is not enabled"):
        super().__init__(detail, status="disabled")


def load_settings_file(path: Path) -> dict[str, Any]:
    """Read and minimally validate the shared config file (fail closed)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CommunityError(f"cannot read community config: {exc.strerror or exc}")
    if len(raw) > MAX_CONFIG_BYTES:
        raise CommunityError("community config exceeds the size limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommunityError(f"community config is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise CommunityError("community config must be a JSON object")
    validated = validate_settings(data)
    # The actual absolute path of the file just read is authoritative; a path
    # embedded inside the JSON is never trusted (no redirection).
    validated["config_path"] = str(Path(path).resolve())
    return validated


def validate_settings(data: Mapping[str, Any]) -> dict[str, Any]:
    """Community-side view of the shared config. The shared keys (schema,
    enabled, generation, enabled_at, repository, branch, project_roots,
    idle_seconds) are validated by the ONE core normalizer — no duplicated
    ranges; only the community-private extension keys are checked here."""
    from mindie_knowledge.loop import settings as shared

    try:
        shared_normalized = shared.normalize(data)
    except ValueError as exc:
        raise CommunityError(str(exc)) from None
    out: dict[str, Any] = dict(DEFAULTS)
    for key in ("enabled", "generation", "enabled_at", "repository", "branch",
                "project_roots", "idle_seconds"):
        out[key] = shared_normalized[key]

    config_path = data.get("config_path")
    if config_path is not None:
        if not isinstance(config_path, str) or not Path(config_path).is_absolute():
            raise CommunityError("config_path must be an absolute path string")
        out["config_path"] = config_path

    fork = data.get("fork")
    out["fork"] = check_repository(fork, "fork") if fork else None
    account = data.get("account")
    out["account"] = check_account(account) if account else None

    for key, limit in (("transaction_seconds", (5, 3600)), ("operation_limit", (1, 500))):
        value = data.get(key, DEFAULTS[key])
        if type(value) is not int or not limit[0] <= value <= limit[1]:
            raise CommunityError(f"{key} must be {limit[0]}..{limit[1]}")
        out[key] = value

    transport = data.get("transport", "gh")
    if transport not in ("gh", "file"):
        raise CommunityError("transport must be 'gh' (GitHub CLI) or 'file' (local dev)")
    out["transport"] = transport
    dev_remotes = data.get("dev_remotes") or {}
    if not isinstance(dev_remotes, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in dev_remotes.items()
    ):
        raise CommunityError("dev_remotes must map owner/repo to a Git URL")
    for repo in dev_remotes:
        check_repository(repo)
    out["dev_remotes"] = dict(dev_remotes)
    token_env = data.get("token_env", "GH_TOKEN")
    if not isinstance(token_env, str) or not token_env or not token_env.replace("_", "").isalnum():
        raise CommunityError("token_env must name an environment variable, never a token")
    out["token_env"] = token_env

    return out



def _bounded_int(data: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise CommunityError(f"bot.{key} must be {low}..{high}")
    return value


def sharing_enabled(settings: Mapping[str, Any]) -> bool:
    """The single closed-by-default gate consulted before every effect."""
    return bool(settings.get("enabled")) and bool(settings.get("repository"))


def require_sharing(settings: Mapping[str, Any]) -> None:
    if not settings.get("enabled"):
        raise SharingDisabled()
    if not settings.get("repository"):
        raise SharingDisabled("community sharing has no target repository configured")


def live_gate(settings: Mapping[str, Any]) -> None:
    """Re-read the live shared config BEFORE every outbound write.

    ``settings['config_path']`` (private, never exported into receipts or logs)
    must be the actual absolute path of the shared community JSON. The file is
    re-validated fresh and must still match this run's admitted settings:
    enabled, nonempty string generation, repository, branch, fork, account and
    project_roots. Any mismatch, absence or malformation stops the write.
    """
    require_sharing(settings)
    path = settings.get("config_path")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise SharingDisabled("publication requires the actual absolute shared config path")
    try:
        live = load_settings_file(Path(path))
    except CommunityError as exc:
        raise SharingDisabled(f"live community config unreadable: {str(exc)[:120]}")
    if live.get("enabled") is not True:
        raise SharingDisabled("community sharing was disabled")
    live_generation = live.get("generation")
    if not (isinstance(live_generation, str) and live_generation):
        raise SharingDisabled("live config carries no admitted nonempty generation")
    for key in ("repository", "branch", "fork", "account"):
        if live.get(key) != settings.get(key):
            raise SharingDisabled(f"community {key} changed since this run started")
    if live_generation != settings.get("generation"):
        raise SharingDisabled("community settings generation changed; restart with fresh settings")
    if list(live.get("project_roots") or []) != list(settings.get("project_roots") or []):
        raise SharingDisabled("community project scope changed since this run started")
