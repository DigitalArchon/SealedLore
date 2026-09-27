"""A tiny OpenAI-compatible endpoint for driving SealedLore without a real model.

usage: fake_server.py PORT TAG   (TAG is written into every prose reply)
Logs one line per request to stdout so a test can see what was called.
"""

import base64
import json
import struct
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, TAG = int(sys.argv[1]), sys.argv[2]


def png() -> bytes:
    raw = b"".join(b"\x00" + bytes([200, 60, 40]) * 64 for _ in range(48))

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def text_of(messages) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(out)


def reply_for(body) -> str:
    text = text_of(body.get("messages", []))
    if '"scene_closed"' in text:
        return json.dumps(
            {
                "location": None,
                "time_of_day": None,
                "situation": None,
                "privacy": None,
                "arrived": [],
                "left": [],
                "scene_closed": None,
                "shortcuts": [],
            }
        )
    if '"happened"' in text and '"elapsed"' in text:
        return json.dumps(
            {
                "elapsed": {"amount": 10, "unit": "minutes", "quote": None},
                "ends_at": None,
                "day": None,
                "place": None,
                "facts": [],
                "happened": [],
            }
        )
    if '"directions"' in text:
        return json.dumps({"directions": []})
    if '"pick"' in text:
        return json.dumps({"pick": []})
    if "JSON array" in text or "JSON list" in text:
        return "[]"
    if "summar" in text.lower() and "scene" in text.lower() and TAG.startswith("PRIVATE"):
        return (
            f"In private they spoke of the town's debts and agreed to meet at dawn. ({TAG}-SUMMARY)"
        )
    return (
        "The dust settles over the main street of Doomsville as the saloon doors swing. "
        f"John eases back on his heels and lets the silence do the talking. ({TAG})"
    )


def log(line: str) -> None:
    sys.stdout.write(f"[{PORT}] {line}\n")
    sys.stdout.flush()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.command} {self.path}")

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):

            def model(mid):
                return {
                    "id": mid,
                    "name": mid,
                    "context_length": 128000,
                    "owned_by": "fake",
                    "architecture": {"modality": "text->text", "output_modalities": ["text"]},
                    "pricing": {"prompt": "0.000001", "completion": "0.000003"},
                }

            return self._json(
                200,
                {"data": [model("fake/storyteller"), model("fake/private"), model("fake/small")]},
            )
        if self.path.startswith("/v1/image-models"):
            return self._json(
                200,
                {
                    "data": [
                        {
                            "id": "fake/painter",
                            "name": "Fake Painter",
                            "supported_parameters": {
                                "resolutions": ["1k", "2k"],
                                "max_input_images": 4,
                                "max_images": 2,
                            },
                            "pricing": {"per_image": {"1k": 0.03, "2k": 0.06}},
                        }
                    ]
                },
            )
        return self._json(404, {"error": "no such route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/v1/chat/completions"):
            text = reply_for(body)
            kind = "json" if text[:1] in "{[" else "prose"
            tail = text_of(body.get("messages", []))[-90:]
            log(f"  -> {kind}: {tail!r}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def send(obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()

            words = text.split(" ")
            try:
                for i in range(0, len(words), 6):
                    piece = " ".join(words[i : i + 6]) + (" " if i + 6 < len(words) else "")
                    send({"id": f"chatcmpl-{PORT}-{i}", "choices": [{"delta": {"content": piece}}]})
                send(
                    {
                        "id": f"chatcmpl-{PORT}",
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 1200,
                            "completion_tokens": len(words),
                            "cost": 0.0012,
                        },
                    }
                )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log("  client closed the stream early")
            return
        if self.path.startswith("/v1/embeddings"):
            inputs = body.get("input") or []
            rows = [
                {"index": i, "embedding": [((hash(t) >> k) % 7) / 7.0 for k in range(1024)]}
                for i, t in enumerate(inputs)
            ]
            return self._json(200, {"data": rows, "usage": {"prompt_tokens": 10 * len(inputs)}})
        if self.path.startswith("/v1/images/generations"):
            picture = {"b64_json": base64.b64encode(png()).decode()}
            return self._json(200, {"data": [picture] * int(body.get("n") or 1), "cost": 0.03})
        return self._json(404, {"error": "no such route"})


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
