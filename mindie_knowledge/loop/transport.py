"""Authenticated loopback service; one process and storage root per domain.

Loopback only: the service binds 127.0.0.1, requires a per-process bearer
token, refuses Origin-headed browser requests, bounds body size, worker
count and stalled reads. There is no remote upstream, authority, contribute
or raw-outcome route — distribution happens through the outbox and the
community package, retrieval through the model-free Git feed sync.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mindie_knowledge.markdown import _atomic_write_text

from .store import canonical, session_key

MAX_BODY = 2 * 1024 * 1024


def rpc(connection, method, arguments=None, *, timeout=10):
    url = connection["url"]
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("the knowledge service is loopback-only")
    request = urllib.request.Request(
        url.rstrip("/") + "/rpc",
        data=canonical(dict(method=method, arguments=arguments or {})).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + connection["token"],
        },
    )

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


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Threading server with an explicit admission bound."""

    daemon_threads = True

    def __init__(self, address, handler, *, max_workers, slot_wait):
        self.slots = threading.BoundedSemaphore(max_workers)
        self.slot_wait = slot_wait
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        try:
            if not self.slots.acquire(timeout=self.slot_wait):
                request.close()
                return
        except OSError:
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Service:
    def __init__(self, engine, *, connection_path=None, admission=None, feeds=(),
                 max_workers=8, request_timeout=10.0):
        self.engine, self.store = engine, engine.store
        self.admission = admission
        self.feeds = list(feeds)
        self.token = secrets.token_urlsafe(32)
        self.connection_path = Path(connection_path) if connection_path else None
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(request_timeout)

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
                except (TimeoutError, OSError):
                    self.close_connection = True
                    return
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

        self.http = _BoundedHTTPServer(
            ("127.0.0.1", 0),
            Handler,
            max_workers=max_workers,
            slot_wait=min(5.0, request_timeout),
        )
        self.connection = dict(
            url=f"http://127.0.0.1:{self.http.server_port}",
            token=self.token,
            domain=self.store.domain,
        )

    # -------------------------------------------------------------- routing

    def _identify(self, args, *, capture=False):
        """Pop and validate the internal identity fields.

        ``_activation`` proves the call with the adapter-issued token (Hook /
        CLI path). ``_session_verified`` means the MCP layer already bound the
        call to verified host metadata; the lease is still re-checked here.
        Capture always requires the token and the current lease schema.
        """
        args = dict(args)
        session = args.pop("_session_id", None)
        if not isinstance(session, str) or not session:
            raise ValueError("call identity is missing")
        if self.admission is None:
            raise ValueError("no adapter admission is configured")
        token = args.pop("_activation", None)
        if token is not None:
            if capture:
                lease = self.admission.capture_lease(session, token)
            else:
                lease = self.admission.check(session, token)
        elif not capture and args.pop("_session_verified", False):
            lease = self.admission.active_lease(session)
            if lease is None:
                raise ValueError("session is not manually activated")
        else:
            raise ValueError("call identity cannot be verified")
        return args, session, lease

    def call(self, method, args):
        if method in {"query", "explain", "feedback", "capture"}:
            args, session, lease = self._identify(args, capture=method == "capture")
        if method == "query":
            return self.store.query(
                args["query"], limit=args.get("limit", 5),
                conditions=args.get("conditions"),
            )
        if method == "explain":
            return self.store.explain(
                args["ref"], offset=args.get("offset", 0), limit=args.get("limit")
            )
        if method == "feedback":
            settings = self.engine._settings()
            root_session = (lease or {}).get("root_session") or session
            scope = self.admission.scope_root(session) if self.admission else None
            publishable = bool(
                settings.allows_capture() and scope and settings.in_scope(scope)
            )
            vote = self.store.record_vote(
                root_hash=session_key(root_session), ref=args["ref"],
                rating=args["rating"], reason=args.get("reason", ""),
                publishable=publishable,
                generation=settings.generation if publishable else None,
            )
            if vote["publishable"]:
                self.engine.last_activity = time.monotonic()
            return vote
        if method == "capture":
            return self.engine.capture(**args)
        if method == "status":
            return self.engine.status()
        if method == "sharing_status":
            return dict(
                self.engine._settings().public_status(),
                outbox=self.store.status()["outbox"],
            )
        if method == "sync":
            return [feed.sync(force=True) for feed in self.feeds]
        if method == "maintenance_resume":
            return self.engine.budget.resume()
        if method == "stop":
            threading.Thread(target=self.close, daemon=True).start()
            return dict(status="stopping")
        raise ValueError("unsupported operation")

    # -------------------------------------------------------------- serving

    def serve(self):
        self.engine.start()
        if self.connection_path:
            _atomic_write_text(self.connection_path, canonical(self.connection) + "\n")
            self.connection_path.chmod(0o600)
        try:
            self.http.serve_forever(poll_interval=0.2)
        finally:
            # Cancel in-flight model work first; interrupted attempts are
            # recorded and never replayed.
            self.engine.shutdown()
            self.http.server_close()

    def close(self):
        self.engine.stop.set()
        self.engine._cancel.set()
        self.http.shutdown()
