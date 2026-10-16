"""Bounded launch bindings for a service outside its calling application's Job.

Adapters declare paths and optional environment names in the immutable engine
configuration. Only the current caller's declared values cross the boundary.
Windows DPAPI protects the short-lived transfer; it is not a credential store.
"""
from __future__ import annotations

import ctypes
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
    "MINDIE_SUMMARY_SCRATCH",
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
    # Windows names are case-insensitive. Explicit upper/lower proxy aliases
    # may coexist in the declaration, but cannot shadow a path binding.
    if {key.casefold() for key in names} & {key.casefold() for key in paths}:
        raise ContextError("service_launch_context_invalid")
    return dict(paths=paths.copy(), environment=names.copy())


def _crypt(raw, entropy, *, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return buffer, Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    raw_buffer, source = blob(raw)
    entropy_buffer, extra = blob(entropy)
    output = Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, ctypes.byref(extra),
                    None, None, 1, ctypes.byref(output)):
        raise ContextError("service_context_decryption_failed" if decrypt
                           else "service_context_encryption_failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


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
