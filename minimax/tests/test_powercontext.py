from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from sdd_context.models import TaskState
from sdd_context.powercontext import PowerContextClient


class MemoryHandler(BaseHTTPRequestHandler):
    entries: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        if self.path == "/v1/memory/remember":
            self.entries.append(payload["text"])
            response = {"status": "remembered"}
        elif self.path == "/v1/memory/search":
            response = {"hits": [{"text": text} for text in self.entries if payload["query"] in text]}
        else:
            self.send_error(404)
            return
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def test_state_is_recovered_by_a_new_client_after_restart() -> None:
    MemoryHandler.entries = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MemoryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        first_process = PowerContextClient(url, "scope-1")
        first_process.save_state(TaskState("task-7", "filter", ["spec"], ["code"], {}))

        second_process = PowerContextClient(url, "scope-1")
        recovered = second_process.load_state("task-7")
    finally:
        server.shutdown()
        server.server_close()
    assert recovered is not None
    assert recovered.completed == ["spec"]
    assert recovered.todo == ["code"]
