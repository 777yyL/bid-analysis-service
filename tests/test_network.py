import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from analyzer.cli import run
from analyzer.config import Settings
from analyzer.source import SourceClient


@pytest.fixture
def local_http(monkeypatch):
    # A developer's OS/environment proxy must not capture private deployment checks.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"notice_id": 1, "title": "本地公告", "body": "采购摄像机"}
            if self.path in ("/healthz", "/source/check"):
                body = {"ok": True}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_cli_checks_private_service_without_inheriting_proxy(local_http):
    result = run(argparse.Namespace(base_url=local_http, api_key="test", command="check"))
    assert result["analyzer"] == {"ok": True}
    assert result["source"] == {"ok": True}


async def test_source_client_defaults_to_direct_private_network(local_http):
    source = SourceClient(Settings(_env_file=None, source_base_url=local_http))
    try:
        result = await source.fetch_notice(1)
        assert result.body == "采购摄像机"
    finally:
        await source.aclose()
