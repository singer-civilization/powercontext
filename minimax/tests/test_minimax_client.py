from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from sdd_context.minimax_client import MiniMaxClient


class MiniMaxHandler(BaseHTTPRequestHandler):
    request: ClassVar[dict[str, object]] = {}

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.__class__.request = json.loads(self.rfile.read(length))
        payload = {
            "id": "mock-request-1",
            "model": "MiniMax-test",
            "choices": [{"message": {"content": "```diff\ndiff --git a/a.py b/a.py\n```"}}],
            "usage": {"total_tokens": 42},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def test_minimax_receives_retrieved_context() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MiniMaxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = MiniMaxClient("test-key", "MiniMax-test", f"http://127.0.0.1:{server.server_port}/chat")
        result = client.generate_patch("[specs/task-filter.md:5#abc] status filter", "implement filter")
    finally:
        server.shutdown()
        server.server_close()

    messages = MiniMaxHandler.request["messages"]
    assert isinstance(messages, list)
    assert "specs/task-filter.md" in messages[1]["content"]
    assert result.content.startswith("diff --git")
    assert result.usage["total_tokens"] == 42
