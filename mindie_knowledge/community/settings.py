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
    SCHEMA_CONFIG,
    CommunityError,
    check_account,
    check_branch,
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
    if data.get("schema") != SCHEMA_CONFIG:
        raise CommunityError(f"community config must declare schema {SCHEMA_CONFIG}")
    out: dict[str, Any] = dict(DEFAULTS)
    enabled = data.get("enabled", False)
    if type(enabled) is not bool:
        raise CommunityError("community config 'enabled' must be a boolean")
    out["enabled"] = enabled
    generation = data.get("generation")
    if enabled and not (isinstance(generation, str) and generation):
        raise CommunityError("enabled community config requires a nonempty string generation")
    if generation is not None and not isinstance(generation, str):
        generation = str(generation)
    out["generation"] = generation
    enabled_at = data.get("enabled_at")
    if enabled and not (type(enabled_at) in (int, float)
                        and math.isfinite(enabled_at) and enabled_at > 0):
        # An enabled config without a finite positive start authorizes nothing.
        raise CommunityError("enabled community config requires a finite positive 'enabled_at'")
    out["enabled_at"] = enabled_at
    config_path = data.get("config_path")
    if config_path is not None:
        if not isinstance(config_path, str) or not Path(config_path).is_absolute():
            raise CommunityError("config_path must be an absolute path string")
        out["config_path"] = config_path

    repository = data.get("repository")
    if repository is not None:
        out["repository"] = check_repository(repository)
    else:
        out["repository"] = None
    out["branch"] = check_branch(str(data.get("branch") or DEFAULTS["branch"]))
    roots = data.get("project_roots", [])
    if not isinstance(roots, list) or not all(
        isinstance(r, str) and Path(r).is_absolute() for r in roots
    ):
        raise CommunityError("community config 'project_roots' must be absolute paths")
    out["project_roots"] = roots
    idle = data.get("idle_seconds", DEFAULTS["idle_seconds"])
    if type(idle) is not int or not 30 <= idle <= 86400:
        raise CommunityError("idle_seconds must be 30..86400")
    out["idle_seconds"] = idle

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

    bot = data.get("bot") or {}
    if not isinstance(bot, Mapping):
        raise CommunityError("bot settings must be an object")
    out["bot"] = _validate_bot(bot)
    return out


def _validate_bot(bot: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    account = bot.get("account")
    out["account"] = check_account(account) if account else None
    argv = bot.get("grok_argv")
    for key in ("grok_argv", "skill_grok_argv"):
        argv = bot.get(key)
        if argv is not None:
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise CommunityError(f"bot.{key} must be a nonempty list of strings")
            out[key] = list(argv)
        else:
            out[key] = None
    skill_prefix = bot.get("skill_prefix", "plugins/mindie-agent/skills")
    if not isinstance(skill_prefix, str) or not skill_prefix or skill_prefix.startswith("/"):
        raise CommunityError("bot.skill_prefix must be a clean relative path")
    parts = PurePosixPath(skill_prefix).parts
    if ".." in parts or any(p.startswith(".") for p in parts):
        raise CommunityError("bot.skill_prefix must be a clean relative path")
    out["skill_prefix"] = str(PurePosixPath(skill_prefix))
    out["review_timeout_seconds"] = _bounded_int(bot, "review_timeout_seconds", 300, 30, 1800)
    out["review_input_bytes"] = _bounded_int(bot, "review_input_bytes", 64 * 1024, 1024, 1024 * 1024)
    out["review_output_bytes"] = _bounded_int(bot, "review_output_bytes", 128 * 1024, 1024, 1024 * 1024)
    out["max_files_per_pr"] = _bounded_int(bot, "max_files_per_pr", 100, 1, 500)
    out["poll_max_prs"] = _bounded_int(bot, "poll_max_prs", 20, 1, 100)
    plugin_repo = bot.get("plugin_repository")
    out["plugin_repository"] = check_repository(plugin_repo, "plugin_repository") if plugin_repo else None
    plugin_branch = bot.get("plugin_branch")
    out["plugin_branch"] = check_branch(str(plugin_branch)) if plugin_branch else "main"
    plugin_token_env = bot.get("plugin_token_env")
    if plugin_token_env is not None and (
        not isinstance(plugin_token_env, str) or not plugin_token_env.replace("_", "").isalnum()
    ):
        raise CommunityError("bot.plugin_token_env must name an environment variable")
    out["plugin_token_env"] = plugin_token_env
    out["merge_method"] = bot.get("merge_method") if bot.get("merge_method") in ("squash", "merge") else "squash"
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
