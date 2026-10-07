"""Deterministic OpenAI-compatible embedding endpoint for local smoke tests only."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    dimension = 1536

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/embeddings"):
            self.send_response(404)
            self.end_headers()
            return
        rows = [
            {"index": index, "embedding": [0.01] * self.dimension}
            for index, _text in enumerate(payload.get("input", []))
        ]
        body = json.dumps({"data": rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--dimension", type=int, default=1536)
    arguments = parser.parse_args()
    Handler.dimension = arguments.dimension
    ThreadingHTTPServer((arguments.host, arguments.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
