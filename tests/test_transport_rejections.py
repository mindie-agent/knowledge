"""Real loopback HTTP: request rejection is not wire/service corruption."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mindie_knowledge.loop.transport import RequestRejected, rpc


@pytest.mark.parametrize('payload,error', [
    ({'ok': False, 'error_kind': 'invalid_request', 'error': 'unknown reference'}, RequestRejected),
    ({'ok': False, 'error_kind': 'operation_failed', 'error': 'broken database'}, RuntimeError),
    ({'ok': False, 'error': 'untyped service failure'}, RuntimeError),
    (b'{not json}', ValueError),
])
def test_only_explicit_request_rejection_is_nonfatal(payload, error):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(error) as caught:
            rpc({'url': f'http://127.0.0.1:{server.server_port}', 'token': 'test'}, 'query', {'query': 'rms_norm'})
        assert isinstance(caught.value, RequestRejected) == (error is RequestRejected)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
