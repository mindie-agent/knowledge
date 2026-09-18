"""Authenticated loopback service; one process and storage root per domain."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from vaws_knowledge.markdown import _atomic_write_text
from .store import canonical, digest

MAX_BODY = 16 * 1024 * 1024


def rpc(connection, method, arguments=None, *, timeout=10):
    url = connection["url"]
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("use HTTPS for a remote knowledge service")
    request = urllib.request.Request(
        url.rstrip("/") + "/rpc",
        data=canonical(dict(method=method, arguments=arguments or {})).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + connection["token"],
        },
    )

    # Never forward the service credential through an HTTP redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    with urllib.request.build_opener(NoRedirect).open(
        request, timeout=timeout
    ) as response:
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ValueError("knowledge response exceeds limit")
    result = json.loads(raw)
    if not result.get("ok"):
        raise ValueError(result.get("error", "knowledge request failed"))
    return result["result"]


class Service:
    def __init__(self, engine, *, connection_path=None, upstream=None):
        self.engine, self.store = engine, engine.store
        self.upstream = upstream
        self.engine.evaluate_uses = not bool(upstream)
        self.token = secrets.token_urlsafe(32)
        self.connection_path = Path(connection_path) if connection_path else None
        self.sync_lock = threading.Lock()
        self.last_sync = None
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/rpc" or self.headers.get("Origin"):
                    self.send_error(403)
                    return
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, "Bearer " + service.token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        self.send_error(413)
                        return
                    payload = json.loads(self.rfile.read(length))
                    if set(payload) != {"method", "arguments"} or not isinstance(
                        payload["arguments"], dict
                    ):
                        raise ValueError("invalid RPC payload")
                    result = dict(
                        ok=True,
                        result=service.call(payload["method"], payload["arguments"]),
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    result = dict(ok=False, error=str(exc)[:500])
                except Exception:
                    result = dict(
                        ok=False,
                        error="knowledge operation failed; inspect service diagnostics",
                    )
                data = canonical(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.connection = dict(
            url=f"http://127.0.0.1:{self.http.server_port}",
            token=self.token,
            domain=self.store.domain,
        )

    def call(self, method, args):
        if method == "query":
            return self.store.query(**args)
        if method == "explain":
            return self.store.get(**args)
        if method == "use":
            return self.store.use(**args)
        if method == "capture":
            return self.engine.capture(**args)
        if method == "status":
            return dict(**self.engine.status(), last_sync=self.last_sync)
        if method == "snapshot":
            return self.store.snapshot()
        if method == "receive_use":
            ident = self.store.receive_use(args["usage"])
            self.engine.wake()
            return dict(use_id=ident)
        if method == "contribute":
            snapshot = args["snapshot"]
            if snapshot.get("feedback"):
                raise ValueError(
                    "contributions contain entries only; the authority judges actual uses"
                )
            installed = self.store.install_snapshot(snapshot)
            for entry in snapshot["entries"]:
                self.store.publish(entry["id"])
            return installed
        if method == "sync":
            return self.sync()
        if method == "stop":
            threading.Thread(target=self.close, daemon=True).start()
            return dict(status="stopping")
        raise ValueError("unsupported operation")

    def sync(self):
        if not self.upstream:
            return dict(status="no_upstream")
        if not self.sync_lock.acquire(blocking=False):
            return dict(status="busy")
        try:
            # Only material explicitly authorized for publication is offered.
            offered = self.store.snapshot()
            offered["feedback"] = []
            offered["version"] = digest(
                {k: v for k, v in offered.items() if k != "version"}
            )
            if offered["entries"]:
                rpc(self.upstream, "contribute", dict(snapshot=offered))
            snapshot = rpc(self.upstream, "snapshot")
            installed = self.store.install_snapshot(snapshot)
            # Only explicitly configured sharing sends completed use evidence.
            # Raw hook captures never leave the local service through this path.
            with self.store.lock:
                rows = [
                    dict(r)
                    for r in self.store.db.execute(
                        "SELECT * FROM uses WHERE outcome!='' AND origin='local'"
                    )
                ]
            for usage in rows:
                if self.store.upstream_entry(usage["entry_id"]):
                    rpc(self.upstream, "receive_use", dict(usage=usage))
            self.last_sync = dict(status="synced", **installed)
        except Exception as exc:
            self.last_sync = dict(status="unavailable", error=type(exc).__name__)
        finally:
            self.sync_lock.release()
        return self.last_sync

    def serve(self):
        self.engine.start()
        if self.connection_path:
            _atomic_write_text(self.connection_path, canonical(self.connection) + "\n")
            self.connection_path.chmod(0o600)

        def maintain():
            while not self.engine.stop.is_set():
                self.sync()
                self.engine.stop.wait(30)

        self.sync_thread = threading.Thread(target=maintain, daemon=True)
        self.sync_thread.start()
        try:
            self.http.serve_forever(poll_interval=0.2)
        finally:
            self.engine.stop.set()
            self.http.server_close()
            self.engine.thread.join(timeout=185)

    def close(self):
        self.http.shutdown()
