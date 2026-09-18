"""OpenViking 0.4.19 adapter: native write/find/read/rm on loopback."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from mindie_knowledge.local.backend import Hit, UnavailableBackend
from mindie_knowledge.local.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL, model_fingerprint
from mindie_knowledge.local.instance import OPENVIKING_VERSION, instance_for_config, without_proxies
from mindie_knowledge.local.shared import shared_search_uris
from mindie_knowledge.markdown import LAYERS, SHARED_BOOTSTRAP_URI, URI_ROOT, layer_from_uri, parse_markdown

LAYER_ROOTS = {layer: f"{URI_ROOT}/{layer}" for layer in LAYERS}
LAYER_ROOTS["shared"] = SHARED_BOOTSTRAP_URI
READ_TIMEOUT = 5.0
MAINTENANCE_TIMEOUT = 120.0


def _not_found(exc: Exception) -> bool:
    return (
        str(getattr(exc, "code", "")).upper() == "NOT_FOUND"
        or "NOT_FOUND" in str(exc).upper()
        or "not found" in str(exc).lower()
    )


class OpenVikingBackend:
    name = "openviking"

    def __init__(self, config: Any):
        self.config = config
        self.instance = instance_for_config(config)
        self._client: Any = None
        self._client_binding: tuple[str, str] | None = None
        self._reader: Any = None
        self._reader_binding: tuple[str, str] | None = None
        self._reason = ""

    def available(self) -> tuple[bool, str]:
        try:
            self._connect(start=True, reader=False)
        except Exception as exc:  # noqa: BLE001
            self._reason = f"{type(exc).__name__}: {exc}"
            return False, self._reason
        return True, "openviking ready"

    def ready(self) -> tuple[bool, str]:
        try:
            # A connected HTTP reader proves its liveness with the ensuing
            # bounded request. Repeating the process-ownership/CIM audit here
            # makes every warm query pay a maintenance cost on Windows.
            self._read_client()
        except Exception as exc:  # noqa: BLE001 - query never prepares the engine
            return False, f"{type(exc).__name__}: {exc}"
        return True, "openviking ready"

    def index_fingerprint(self) -> Mapping[str, Any]:
        cache = getattr(self.instance, "cache_dir", None)
        return {
            "backend": self.name,
            "openviking": OPENVIKING_VERSION,
            "embedding_provider": "fastembed-onnxruntime-cpu",
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimension": EMBEDDING_DIMENSION,
            "embedding_files_sha256": model_fingerprint(cache) if cache is not None else None,
        }

    def check_document(self, uri: str, content: str) -> bool:
        """Check content and the exact native L2 vector record.

        OpenViking 0.4.19 ``stat(uri)`` returns the deterministic vector id;
        ``stat(id)`` resolves that id through VikingDB before reading the file.
        A surviving file with a missing vector therefore fails this check.
        Unlike directory consistency reports, this is not truncated at 20
        missing records and never requires reindexing an unowned subtree.
        """

        client = self._maintenance_client(start=False)
        try:
            stored = client.read(uri)
            normalized = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
            if not isinstance(stored, str) or normalized(stored) != normalized(content):
                return False
            physical = client.stat(uri)
            record_id = physical.get("id") if isinstance(physical, dict) else None
            if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", record_id):
                return False
            indexed = client.stat(record_id)
            return bool(
                isinstance(indexed, dict)
                and indexed.get("uri") == uri
                and not indexed.get("isDir")
            )
        except Exception as exc:  # noqa: BLE001
            if _not_found(exc):
                return False
            raise

    def upsert(self, uri: str, content: str, *, layer: str, wait: bool = True) -> None:
        client = self._maintenance_client()
        self._ensure_layer(client, layer)
        client.write(
            uri,
            content,
            wait=wait,
            options={"processing_mode": "vectors_only"},
        )
        if wait:
            client.wait_processed(timeout=120)

    def delete(self, uri: str) -> None:
        client = self._maintenance_client()
        try:
            client.rm(uri, wait=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            if not _not_found(exc):
                raise

    def read(self, uri: str) -> str | None:
        client = self._read_client()
        try:
            return client.read(uri)
        except Exception as exc:  # noqa: BLE001
            if _not_found(exc):
                return None
            self._drop_reader(client)
            raise

    def search(
        self,
        text: str,
        *,
        layers: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[Hit]:
        client = self._read_client()
        wanted = [name for name in (layers or LAYERS) if name in LAYER_ROOTS]
        fetch = max(int(limit or 8) * 4, 16)
        hits: dict[str, Hit] = {}
        state_root = getattr(self.instance, "state_root", None)
        targets: list[str] = []
        for layer in wanted:
            targets.extend(shared_search_uris(state_root) if layer == "shared" else (LAYER_ROOTS[layer],))
        targets = list(dict.fromkeys(targets))
        if not targets:
            return []
        try:
            payload = client.find(
                text or "",
                target_uri=targets,
                limit=fetch,
                options={"level": 2, "read_content": True, "score_threshold": 0},
            )
        except Exception:
            # Return this failure, without a hidden second network timeout.
            # The next request reconnects to the then-current owned instance.
            self._drop_reader(client)
            raise
        resources = payload.get("resources") if isinstance(payload, dict) else None
        for item in resources if isinstance(resources, list) else ():
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri") or "")
            if not any(uri.startswith(root + "/") for root in targets):
                continue
            content = str(item.get("content") or item.get("text") or "")
            title, body = parse_markdown(content) if content else ("", "")
            title = title or Path(uri).stem
            score = float(item.get("score") or item.get("similarity") or 0.0)
            hit = Hit(
                uri=uri, score=score, title=title,
                excerpt=" ".join((body or content).split())[:240],
                layer=layer_from_uri(uri) or "", content=content,
            )
            if uri not in hits or hit.score > hits[uri].score:
                hits[uri] = hit
        return sorted(hits.values(), key=lambda hit: (-hit.score, hit.uri))[: max(int(limit or 8), 1)]

    def _read_client(self) -> Any:
        return self._reader if self._reader is not None else self._connect(start=False, reader=True)

    def _drop_reader(self, client: Any) -> None:
        if client is not self._reader:
            return
        self._reader = None
        self._reader_binding = None
        try:
            client.close()
        except Exception:
            pass

    def _maintenance_client(self, *, start: bool = True) -> Any:
        return self._client if self._client is not None else self._connect(start=start, reader=False)

    def _connect(self, *, start: bool, reader: bool) -> Any:
        # Only the maintenance path may own startup/downloads. Readers use a
        # separate short-timeout HTTP pool and never wait on an instance lock.
        # Model downloads may require the caller's network proxy. Loopback
        # health uses a dedicated proxy-free opener inside LocalInstance.
        status = self.instance.ensure() if start else self.instance.describe()
        if not status.get("live"):
            raise RuntimeError("knowledge engine is not running; background preparation is pending")
        binding = (status["openviking_url"], self.instance.data_key())
        if not reader and self._reader is not None and self._reader_binding != binding:
            self._drop_reader(self._reader)
        previous = self._reader if reader else self._client
        previous_binding = self._reader_binding if reader else self._client_binding
        if previous is not None and binding == previous_binding:
            return previous
        if previous is not None:
            try:
                previous.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            from openviking_sdk import SyncHTTPClient
        except ImportError as exc:
            raise RuntimeError("openviking_sdk is not installed") from exc
        with without_proxies():
            client = SyncHTTPClient(
                url=binding[0], api_key=binding[1],
                timeout=READ_TIMEOUT if reader else MAINTENANCE_TIMEOUT,
            )
            client.initialize()
        if reader:
            self._reader, self._reader_binding = client, binding
        else:
            self._client, self._client_binding = client, binding
            if start:
                for layer in LAYERS:
                    self._ensure_layer(client, layer)
        return client

    def _ensure_layer(self, client: Any, layer: str) -> None:
        roots = (f"{URI_ROOT}/shared", SHARED_BOOTSTRAP_URI) if layer == "shared" else (LAYER_ROOTS[layer],)
        for uri in roots:
            try:
                client.mkdir(uri)
            except Exception as exc:  # noqa: BLE001
                text = str(exc).upper()
                if "ALREADY" not in text and "EXISTS" not in text:
                    raise


def try_openviking(config: Any) -> Any:
    try:
        backend = OpenVikingBackend(config)
        ok, reason = backend.available()
        if ok:
            return backend
        return UnavailableBackend(reason)
    except Exception as exc:  # noqa: BLE001
        return UnavailableBackend(f"openviking unavailable: {type(exc).__name__}: {exc}")
