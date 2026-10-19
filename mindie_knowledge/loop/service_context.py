"""[Role] Transfer declared caller bindings to a detached service.

[Invariants] Only declared values cross the boundary. Windows DPAPI binds the
short-lived transfer to the current user and launch identity.
[Timeouts & Locks] Local DPAPI calls; no subprocesses or additional locks.
[Side-Effect Policy] Capture encrypts Windows credentials; successful consumption
removes the one-use file before applying its values.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

SCHEMA = "mindie-service-context/1"
LIMIT = 128 * 1024
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_FORBIDDEN = {
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "PSMODULEPATH",
    "CODEX_THREAD_ID", "CODEX_TURN_ID", "CODEX_SESSION_ID",
    "MINDIE_MAINTENANCE_GROUP", "MINDIE_MAINTENANCE_OWNER",
    "MINDIE_SUMMARY_SCRATCH", "MINDIE_SUMMARY_WORKER_DEADLINE",
}
_PREFIXES = ("PYTHON", "LD_", "DYLD_", "GIT_CONFIG", "GIT_SSH", "BASH_ENV",
             "NODE_OPTIONS", "NODE_PATH")


class ContextError(ValueError):
    """Static error codes never include variable values or private paths."""
    def __init__(self, code):
        super().__init__(code)
        self.code, self.stage = code, "context"


def _windows():
    return os.name == "nt"


def _name(key):
    return (isinstance(key, str) and _NAME.fullmatch(key)
            and key.upper() not in _FORBIDDEN
            and not key.upper().startswith(_PREFIXES))


def validate(config):
    value = config.get("service_launch_context", {})
    if not isinstance(value, dict) or set(value) - {"paths", "environment"}:
        raise ContextError("service_launch_context_invalid")
    paths = value.get("paths", {})
    names = value.get("environment", [])
    if (not isinstance(paths, dict) or not isinstance(names, list)
            or len(paths) + len(names) > 64
            or any(not _name(key) for key in [*paths, *names])
            or len(set(names)) != len(names) or set(paths) & set(names)
            or len({key.casefold() for key in paths}) != len(paths)):
        raise ContextError("service_launch_context_invalid")
    if any(not isinstance(path, str) or "\x00" in path or "\n" in path
           or not Path(path).is_absolute() for path in paths.values()):
        raise ContextError("service_launch_path_invalid")
    if {key.casefold() for key in names} & {key.casefold() for key in paths}:
        raise ContextError("service_launch_context_invalid")
    return dict(paths=paths.copy(), environment=names.copy())


def _crypt(raw, entropy, *, decrypt=False):
    try:
        import pywintypes
        import win32crypt
    except ImportError:
        raise ContextError("service_context_crypto_unavailable") from None
    try:
        # Current-user DPAPI and launch-bound entropy protect credentials
        # without changing the user's profile ACLs or allowing UI prompts.
        if decrypt:
            return win32crypt.CryptUnprotectData(raw, entropy, None, None, 1)[1]
        return win32crypt.CryptProtectData(raw, SCHEMA, entropy, None, None, 1)
    except pywintypes.error:
        code = "decryption" if decrypt else "encryption"
        raise ContextError(f"service_context_{code}_failed") from None


def capture(config, binding):
    declaration = validate(config)
    values = {key: os.environ[key] for key in declaration["environment"]
              if key in os.environ}
    value = dict(schema=SCHEMA, binding=binding, paths=declaration["paths"], environment=values)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    if len(raw) > LIMIT:
        raise ContextError("service_launch_context_too_large")
    return (_crypt(raw, json.dumps(binding, sort_keys=True).encode())
            if _windows() else raw)


def consume(path, config, binding):
    path = Path(path)
    try:
        if _windows():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT + 4096:
                raise ContextError("service_context_file_invalid")
            raw = _crypt(path.read_bytes(), json.dumps(binding, sort_keys=True).encode(), decrypt=True)
        else:
            # systemd does not inherit the caller's environment. Its one-use
            # transfer is private to the current UID, never a unit property.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                        or info.st_size > LIMIT):
                    raise ContextError("service_context_file_invalid")
                raw = stream.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise ContextError("service_context_file_invalid")
        value = json.loads(raw)
    except ContextError:
        raise
    except (OSError, ValueError):
        raise ContextError("service_context_unavailable") from None
    declaration = validate(config)
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("binding") != binding or value.get("paths") != declaration["paths"]
            or not isinstance(value.get("environment"), dict)
            or set(value["environment"]) - set(declaration["environment"])
            or any(not isinstance(item, str) or "\x00" in item
                   for item in value["environment"].values())):
        raise ContextError("service_context_binding_invalid")
    # No service is admitted if the one-use secret transfer cannot be removed.
    try:
        path.unlink()
    except OSError:
        raise ContextError("service_context_cleanup_failed") from None
    return {**value["environment"], **value["paths"]}


def apply(config, values):
    """Apply the authenticated caller projection, including declared absence."""
    normalize = str.casefold if _windows() else lambda key: key
    declared = {normalize(key) for key in validate(config)["environment"]}
    # A scheduled task can inherit stale user-level credentials/proxies.
    # Explicitly absent caller values must remain absent in its service.
    for key in list(os.environ):
        if normalize(key) in declared:
            del os.environ[key]
    os.environ.update(values)
