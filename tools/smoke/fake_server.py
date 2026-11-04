"""A tiny OpenAI-compatible endpoint for driving SealedLore without a real model.

usage: fake_server.py PORT TAG   (TAG is written into every prose reply)
Logs one line per request to stdout so a test can see what was called.
"""

import base64
import json
import re
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


def looks_for(text: str) -> list[dict]:
    """The look pick (engine/look_pick.py): everyone in their second picture,
    so a walk sees a look change."""
    block = text.split("THE PEOPLE AND THEIR PICTURES:", 1)[-1]
    block = block.split("THE AUTHOR'S TURN:", 1)[0].split("THE LATEST PASSAGE:", 1)[0]
    rows = []
    for person in block.strip().split("\n\n"):
        lines = person.strip().splitlines()
        if not lines:
            continue
        name = lines[0].split(" (shown now as", 1)[0].strip()
        handles = [line[2:].split(":", 1)[0].strip() for line in lines[1:] if line.startswith("- ")]
        if name and handles:
            rows.append({"name": name, "picture": handles[min(1, len(handles) - 1)], "missing": ""})
    return rows


def draft_for(text: str) -> dict:
    """A story drafted from a premise (engine/authoring.py): a small setup,
    with the author's own characters (each under a "## Name" heading in the
    request) in its cast, each with what this story changes of them."""
    own = [name.strip() for name in re.findall(r"^## (.+)$", text, re.M)]
    cast = [{"name": "Wren", "summary": "A courier who carries medicine.", "playable": True}]
    cast += [
        {"name": name, "playable": False, "in_this_story": f"{name} runs the clinic. ({TAG})"}
        for name in own
    ]
    return {
        "title": f"A drafted story ({TAG})",
        "description": "Medicine through the snow.",
        "world": "A frozen county, four winters after the end of the world.",
        "cast": cast,
        "supporting": [],
        "lore": [],
        "play_as": "Wren",
        "opening": "Snow at the clinic door, and a knock.",
        "starting_scene": {"location": "The clinic", "present": [c["name"] for c in cast]},
        "style": {},
        "agency_mode": "contested",
    }


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
    if '"play_as": "a cast member\'s name"' in text:
        return json.dumps(draft_for(text))
    if '{"references": [' in text and '"prompt"' in text:
        # A picture's prompt writer (engine/image_prompt.py): none of the
        # pictures offered, a prompt of its own.
        return json.dumps(
            {"references": [], "prompt": f"A farmhouse porch at dusk, snow on the rail. ({TAG})"}
        )
    if 'Reply with JSON only: {"prompt"' in text:
        # A video's prompt writer (engine/video_prompt.py).
        return json.dumps({"prompt": f"Snow drifts past the porch light. ({TAG})"})
    if '{"looks": [{"name"' in text:
        return json.dumps({"looks": looks_for(text)})
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
