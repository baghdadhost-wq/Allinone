#!/usr/bin/env python3
"""A stand-in gateway speaking the Anthropic Messages API, for tests.

GET  /health          200
GET  /v1/models       200 with Bearer FAKE_TOKEN (401 otherwise)
POST /v1/messages     echoes the last user message
Every request is appended to $FAKE_GATEWAY_LOG as a JSON line.
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FAKE_TOKEN", "fake-token")
LOG = os.environ.get("FAKE_GATEWAY_LOG")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _log(self, body=None):
        if LOG:
            with open(LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": self.command, "path": self.path,
                                     "auth": self.headers.get("Authorization", ""),
                                     "body": body}) + "\n")

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}" or self.headers.get("x-api-key") == TOKEN

    def do_GET(self):
        self._log()
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/v1/models":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"data": [{"id": "auto"}, {"id": "fake/model-1"}, {"id": "fake/model-2"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self._log(body)
        if self.path != "/v1/messages":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"type": "error", "error": {"type": "authentication_error"}})
        last = body["messages"][-1]["content"]
        if isinstance(last, list):
            last = " ".join(b.get("text", "") for b in last if isinstance(b, dict))
        if "FAIL_429" in str(last):
            return self._send(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "All models are rate limited"}})
        if body.get("stream"):  # Anthropic-style server-sent events, as real gateways send them
            text = f"echo: {last}"
            events = [("message_start", {"type": "message_start", "message": {"id": "msg_fake", "model": body.get("model"),
                                                                            "usage": {"input_tokens": 7}}}),
                      ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
            events += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": text[i:i + 5]}}) for i in range(0, len(text), 5)]
            events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                       ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
                       ("message_stop", {"type": "message_stop"})]
            data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._send(200, {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [{"type": "text", "text": f"echo: {last}"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
