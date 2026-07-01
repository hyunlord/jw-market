from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.serving.brand_activity.mcp_protocol import handle_jsonrpc
from pipeline.scripts.serving.brand_activity.topic_jobs import TopicJobStore

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8710
STREAM_PATH: Final = "/mcp"


class TopicMcpHandler(BaseHTTPRequestHandler):
    """HTTP adapter exposing the topic MCP JSON-RPC endpoint."""

    store = TopicJobStore()

    def do_POST(self) -> None:
        """Handle one MCP JSON-RPC POST request."""
        if self.path != STREAM_PATH:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid json: {exc.msg}"})
            return
        response = handle_jsonrpc(payload, self.store)
        self._write_json(HTTPStatus.OK, response)

    def do_GET(self) -> None:
        """Expose a small health endpoint for local smoke checks."""
        if self.path not in {"", "/", "/health"}:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._write_json(HTTPStatus.OK, {"status": "ok", "path": STREAM_PATH})

    def log_message(self, format: str, *args: str) -> None:
        """Silence default stderr request logs; code-serving captures child stdout/stderr."""

    def _read_json(self) -> dict[str, JsonValue]:
        length = int(self.headers.get("content-length", "0"))
        if length == 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        value = json.loads(body)
        if isinstance(value, dict):
            return value
        return {"method": None}

    def _write_json(self, status: HTTPStatus, payload: dict[str, JsonValue]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve() -> None:
    """Run the child MCP server until the container stops it."""
    host = os.environ.get("MCP_HOSTNAME", DEFAULT_HOST)
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((host, port), TopicMcpHandler)
    print(f"brand_activity topic MCP server listening on http://{host}:{port}{STREAM_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
