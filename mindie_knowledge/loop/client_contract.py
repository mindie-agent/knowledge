"""Explicit client compatibility across an exact service handoff.

Runtime locations are not a wire protocol. Adapters declare compatible
transcript and summary protocols; all business bindings still participate in
the identity. A retirement receipt can name one exact successor, never latest.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

SCHEMA = "mindie-service-client/1"
_PROTOCOL = re.compile(r"[a-z][a-z0-9-]{0,63}/[1-9][0-9]*\Z")
_FIELDS = {
    "schema", "transcript_protocol", "summary_protocol",
    "adapter_profile", "adapter_config_env",
}


class ClientCompatibilityError(RuntimeError):
    pass


def fingerprint(config):
    """Return a business/protocol identity, or None for undeclared clients."""
    declaration = config.get("client_contract")
    if declaration is None:
        return None
    if (not isinstance(declaration, dict) or set(declaration) != _FIELDS
            or declaration.get("schema") != SCHEMA
            or not isinstance(declaration.get("adapter_config_env"), str)
            or any(not isinstance(declaration.get(key), str)
                   or not _PROTOCOL.fullmatch(declaration[key])
                   for key in ("transcript_protocol", "summary_protocol"))
            or not isinstance(declaration.get("adapter_profile"), str)
            or not Path(declaration["adapter_profile"]).is_absolute()):
        raise ValueError("service client compatibility declaration is invalid")
    value = copy.deepcopy(config)
    from .service_context import validate
    context = validate(value)
    value["service_launch_context"] = context
    paths = context["paths"]
    key = declaration["adapter_config_env"]
    if not isinstance(paths.get(key), str) or not Path(paths[key]).is_absolute():
        raise ValueError("service client adapter binding is missing")
    paths[key] = declaration["adapter_profile"]
    for key in ("transcript_adapter", "redactor_executable"):
        if not isinstance(value.get(key), str) or not Path(value[key]).is_absolute():
            raise ValueError("service client runtime binding is invalid")
        value.pop(key)
    command = value.get("summary_command")
    if (not isinstance(command, list) or len(command) < 2
            or not all(isinstance(part, str) and part for part in command)
            or not Path(command[0]).is_absolute()):
        raise ValueError("service client summary command is invalid")
    if command[1] == "-m" and len(command) >= 3:
        arguments = command[3:]
    elif Path(command[1]).is_absolute():
        arguments = command[2:]
    else:
        raise ValueError("service client summary entry must be explicit")
    value["summary_command"] = [declaration["summary_protocol"], *arguments]
    # Candidate receipts describe installed bytes. Publication policy remains
    # part of the client identity; a different build is not a different policy.
    if "product_validation" in value:
        receipt = value["product_validation"]
        if not isinstance(receipt, dict) or not isinstance(receipt.get("publication"), dict):
            raise ValueError("service client product declaration is invalid")
        value["product_validation"] = {
            "schema": receipt.get("schema"), "publication": receipt["publication"],
        }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def require_compatible(first, second):
    expected = fingerprint(first)
    if expected is None or fingerprint(second) != expected:
        raise ClientCompatibilityError("service client configuration is incompatible")
    return expected


def successor_binding(config_path, config, successor_config_path):
    from .cli import config_at
    from .lifecycle import config_fingerprint

    target = Path(successor_config_path).resolve()
    if target == Path(config_path).resolve():
        raise ClientCompatibilityError("service successor cannot be itself")
    successor = config_at(target)
    require_compatible(config, successor)
    return dict(config_path=str(target),
                config_fingerprint=config_fingerprint(target, successor))


def resolve_config(config_path):
    """Follow completed, exact handoffs without starting or selecting a release."""
    from .cli import config_at
    from .lifecycle import ServiceRetired, config_fingerprint, inspect_retirement

    current = Path(config_path).resolve()
    original = config_at(current)
    config = original
    seen = set()
    while True:
        if current in seen:
            raise ClientCompatibilityError("service successor cycle")
        seen.add(current)
        receipt = inspect_retirement(current, config)
        if receipt is None:
            return current, config
        if receipt["status"] != "retired":
            raise ClientCompatibilityError("service handoff outcome is uncertain")
        successor = receipt.get("successor")
        if successor is None:
            raise ServiceRetired("this service configuration is retired without a successor")
        current = Path(successor["config_path"])
        config = config_at(current)
        if config_fingerprint(current, config) != successor["config_fingerprint"]:
            raise ClientCompatibilityError("service successor configuration changed")
        require_compatible(original, config)


def connect_ready(config_path, config):
    """Authenticate a compatible live owner; do not authorize a second owner."""
    from .cli import connect, connection_path
    from .lifecycle import config_fingerprint, inspect_retirement
    from .locks import lock_held
    from .transport import rpc

    receipt = inspect_retirement(config_path, config)
    if receipt is not None and receipt["status"] != "retired":
        raise ClientCompatibilityError("service handoff outcome is uncertain")
    if receipt is not None and "successor" not in receipt:
        from .lifecycle import ServiceRetired
        raise ServiceRetired("this service configuration is retired without a successor")
    identity = fingerprint(config)
    if identity is None:
        return None
    consumer = connection_path(config).with_name("consumer.lock")
    try:
        consumer.stat()
    except FileNotFoundError:
        return None
    owned = lock_held(consumer)
    if owned is False:
        return None
    if owned is None:
        raise ClientCompatibilityError("service consumer ownership is unknown")
    try:
        connection = connect(config)
    except FileNotFoundError:
        return None
    expected = connection.get("client_fingerprint")
    if expected != identity:
        raise ClientCompatibilityError("active service client configuration is incompatible")
    status = rpc(connection, "status")
    verify_service_identity(config_path, config, connection, status)
    if (status.get("worker_alive") is not True or status.get("worker_failed") is True
            or status.get("admission_frozen") is True):
        raise ClientCompatibilityError("compatible service is not ready")
    return connection


def verify_service_identity(config_path, config, connection, status):
    """Share one authenticated status check between MCP and short Stop."""
    from .lifecycle import config_fingerprint, inspect_retirement, ServiceRetired
    receipt = inspect_retirement(config_path, config)
    if receipt is not None:
        if receipt["status"] != "retired":
            raise ClientCompatibilityError("service handoff outcome is uncertain")
        if "successor" not in receipt:
            raise ServiceRetired("this service configuration is retired without a successor")
        if connection.get("config_fingerprint") == config_fingerprint(config_path, config):
            raise ClientCompatibilityError("retired service still owns the connection")
    identity = fingerprint(config)
    attested = status.get("service_identity") if isinstance(status, dict) else None
    if (identity is None or connection.get("client_fingerprint") != identity
            or not isinstance(attested, dict)
            or attested.get("client_fingerprint") != identity
            or attested.get("config_fingerprint") != connection.get("config_fingerprint")
            or attested.get("config_path") != connection.get("config_path")):
        raise ClientCompatibilityError("active service identity is unverified")


def ensure_service(config_path):
    from .cli import config_at, ensure_service as ensure_owner
    config = config_at(config_path)
    ready = connect_ready(config_path, config)
    if ready is not None:
        return ready
    target, _ = resolve_config(config_path)
    return ensure_owner(target)


def prepare_service(config_path):
    from .cli import config_at, prepare_service as prepare_owner
    config = config_at(config_path)
    if connect_ready(config_path, config) is not None:
        return "ready"
    target, _ = resolve_config(config_path)
    return prepare_owner(target)
