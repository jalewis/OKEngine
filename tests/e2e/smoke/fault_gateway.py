#!/usr/bin/env python3
"""Deterministic Responses gateway used only by the disposable resilience stack."""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


EVENTS: list[dict] = []
SLOT = threading.BoundedSemaphore(1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(json.dumps({"event": "http", "message": fmt % args}), flush=True)

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, b'{"ok":true}')
        elif self.path == "/events":
            self._send(200, json.dumps(EVENTS).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        mode = self.headers.get("X-Fault-Mode", "normal")
        try:
            requested_model = str(json.loads(payload).get("model") or "")
        except (json.JSONDecodeError, AttributeError):
            requested_model = ""
        if requested_model.startswith("fault-"):
            mode = requested_model.removeprefix("fault-")
        event = {
            "mode": mode,
            "client_id": self.headers.get("X-Client-Id"),
            "conversation_id": self.headers.get("X-Conversation-Id"),
            "oneshot": self.headers.get("X-Oneshot"),
            "path": self.path,
            "body_bytes": len(payload),
        }
        EVENTS.append(event)
        print(json.dumps({"event": "request", **event}), flush=True)
        if self.path != "/v1/responses":
            self._send(404, b'{"error":"wrong endpoint"}')
        elif mode == "timeout":
            time.sleep(3)
            self._send(200, b'{"output":[]}' )
        elif mode == "malformed":
            body = b"data: {not-json}\n\ndata: [DONE]\n\n"
            self._send(200, body, "text/event-stream")
        elif mode == "503":
            self._send(503, b'{"error":"saturated"}')
        elif mode == "saturated":
            if not SLOT.acquire(blocking=False):
                self._send(503, b'{"error":"queue saturated"}')
                return
            try:
                time.sleep(0.5)
                body = b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                self._send(200, body, "text/event-stream")
            finally:
                SLOT.release()
        elif mode == "disk-full":
            target = "/fault-disk/exhaustion.bin"
            try:
                with open(target, "wb") as handle:
                    while True:
                        handle.write(b"x" * 16384)
                        handle.flush()
            except OSError as exc:
                EVENTS.append({"mode": mode, "error": str(exc), "terminal": "failed"})
                self._send(507, json.dumps({"error": "disk full", "detail": str(exc)}).encode())
            finally:
                try:
                    os.unlink(target)
                except OSError:
                    pass
        else:
            body = (
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                b'data: [DONE]\n\n'
            )
            self._send(200, body, "text/event-stream")


class LoadSafeHTTPServer(ThreadingHTTPServer):
    """Keep the deterministic fixture from becoming the load test's bottleneck.

    ``TCPServer`` defaults to a listen backlog of five.  A synchronized 100-user burst can then
    be reset by the kernel before a handler thread exists, which measures fixture admission rather
    than the Responses contract.  Production gateways have a substantially larger admission
    queue; mirror that boundary while retaining the container's CPU, memory, and PID limits.
    """

    request_queue_size = 256


if __name__ == "__main__":
    LoadSafeHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
