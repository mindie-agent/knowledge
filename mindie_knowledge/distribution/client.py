"""Native client seam for build and sync.

The distribution module never owns the OpenViking/embedding processes; the
local knowledge lifecycle does. Callers either pass an already-connected
client or an ``openviking_url`` (plus optional ``api_key``) and the default
factory connects lazily. Only the following native surface is used, against
the real OpenViking 0.4.19 SDK (``SyncHTTPClient``)::

    mkdir(uri) / write(uri, content, wait=..., options={"processing_mode": "vectors_only"})
    wait_processed(timeout) / check_consistency(uri) / stat(uri)
    export_ovpack(uri, to, include_vectors=True)
    import_ovpack(file_path, parent, on_conflict="fail", vector_mode="require")
    rm(uri, recursive=True, wait=True) / find(query, target_uri=..., limit=..., options=...)
    read(uri) / close()

Any test double only needs the same methods.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from mindie_knowledge.distribution.errors import DistributionError
from mindie_knowledge.distribution.manifest import EMBEDDING_PROVIDER

MetricsReader = Callable[[], Mapping[str, Any]]


def connect_client(url: str, *, api_key: str | None = None) -> Any:
    """Connect a native ``SyncHTTPClient`` to a live loopback instance."""

    try:
        from openviking_sdk import SyncHTTPClient
    except ImportError:
        raise DistributionError(
            "openviking_sdk is not installed; install mindie-knowledge with the pinned "
            "OpenViking 0.4.19 dependency"
        ) from None
    kwargs: dict[str, Any] = {"url": url}
    if api_key:
        kwargs["api_key"] = api_key
    from mindie_knowledge.local.instance import without_proxies

    # The SDK captures proxy settings when creating its HTTP client. This
    # connection is loopback-only; GitHub transport keeps the user's proxy.
    with without_proxies():
        client = SyncHTTPClient(**kwargs)
        client.initialize()
    return client


def provision_tenant_key(
    openviking_url: str,
    *,
    root_key: str,
    account_id: str = "default",
    user_id: str = "knowledge-data",
    connect: Callable[..., Any] = connect_client,
) -> str:
    """Create (or reuse) the tenant data user and return its key.

    The root key only administers accounts; content operations use the tenant
    key — the verified research flow for OVPack import/export. ``connect`` is
    injectable for tests.
    """

    try:
        from openviking_sdk.errors import AlreadyExistsError

        already: tuple[type[BaseException], ...] = (AlreadyExistsError,)
    except ImportError:
        already = ()
    admin = connect(openviking_url, api_key=root_key)
    try:
        try:
            account = admin.admin_create_account(account_id, user_id)
        except already:
            account = admin.admin_register_user(account_id, user_id, role="admin")
    finally:
        admin.close()
    key = account.get("user_key") if isinstance(account, dict) else getattr(account, "user_key", None)
    if not key:
        raise DistributionError(
            f"tenant provisioning on {openviking_url} returned no user_key; "
            "check the server auth_mode is api_key"
        )
    return str(key)


def embedding_info_from_health(
    health_url: str, *, timeout: float = 5.0
) -> tuple[dict[str, Any], MetricsReader]:
    """Read the loopback embedding endpoint identity and return a metrics reader.

    Both the lifecycle embedding server and the distribution test fixture
    answer ``GET /health`` with ``model``/``dimension`` plus cumulative call
    counters. The returned reader snapshots those counters so import-phase and
    query-phase embedding calls can be told apart.
    """

    def snapshot() -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(health_url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise DistributionError(
                f"embedding endpoint {health_url} is not reachable: {exc}; "
                "start the local knowledge service before syncing"
            ) from None
        return payload if isinstance(payload, dict) else {}

    info_payload = snapshot()
    info = {
        "model": info_payload.get("model"),
        "dimension": info_payload.get("dimension"),
        "provider": info_payload.get("provider") or EMBEDDING_PROVIDER,
    }
    return info, snapshot


def metrics_delta(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, int]:
    """Counter delta for the fields the embedding endpoint exposes."""

    if not before or not after:
        return {}
    out: dict[str, int] = {}
    for key in ("requests", "texts", "characters", "generation_rejected"):
        try:
            delta = int(after.get(key, 0)) - int(before.get(key, 0))
        except (TypeError, ValueError):
            continue
        if delta:
            out[key] = delta
    return out
