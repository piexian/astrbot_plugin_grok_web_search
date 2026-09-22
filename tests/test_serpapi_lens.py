"""SerpAPI 适配回归：上传与检索必须共用同一个超时预算。"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import load

UPLOAD_DELAY = 0.7
SEARCH_DELAY = 0.7


class _DelayedHandler(BaseHTTPRequestHandler):
    """上传延迟 0.7s、检索延迟 0.7s：总耗 1.4s，超出 1.0s 预算。"""

    def log_message(self, *args):
        pass

    def _send_json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(UPLOAD_DELAY)
        self._send_json({"image_id": "test-id"})

    def do_GET(self):
        time.sleep(SEARCH_DELAY)
        self._send_json({"visual_matches": []})


def test_serpapi_timeout_budget_covers_upload_and_search(monkeypatch):
    """旧实现两阶段各自限时 1s 会成功返回（总耗 1.4s）；新实现必须整体超时。"""
    mod = load("api.serpapi_lens")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DelayedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        mod, "SERPAPI_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        loop = asyncio.new_event_loop()
        try:
            start = loop.time()
            res = loop.run_until_complete(
                mod.serpapi_lens_search(
                    (b"x" * 64, "image/png"), api_key="k", timeout=1.0
                )
            )
            elapsed = loop.time() - start
        finally:
            loop.close()
    finally:
        server.shutdown()
        server.server_close()

    assert not res["ok"]
    assert "超时" in res["error"]
    assert elapsed < 2.5  # 预算 1.0s：不允许两阶段串行各自放宽


def test_serpapi_success_within_budget(monkeypatch):
    """正常快速响应时仍应成功（预算充足不被误伤）。"""

    class _FastHandler(_DelayedHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._send_json({"image_id": "test-id"})

        def do_GET(self):
            self._send_json({"visual_matches": [{"title": "T", "link": "https://e/1"}]})

    mod = load("api.serpapi_lens")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FastHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        mod, "SERPAPI_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(
                mod.serpapi_lens_search(
                    (b"x" * 64, "image/png"), api_key="k", timeout=5.0
                )
            )
        finally:
            loop.close()
    finally:
        server.shutdown()
        server.server_close()

    assert res["ok"]
    assert res["payload"]["visual_matches"][0]["title"] == "T"
