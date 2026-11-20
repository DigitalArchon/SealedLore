"""Every call this run made to a model, for the author to see where their
stories go and what they cost (the author, Nov 19 2026: "just shows the AI
calls, the model and provider … time and tokens sent and received,
encryption status, price if available … stored in memory for the current
session"). View → AI calls this session… shows it (gui/call_log_view.py).

Never anything that was said: what each call was for, the model, where it
went, how it was protected, its tokens, cost and time. Kept in memory for the
run only, never written anywhere, the newest `KEPT` calls.

Every client records its own calls: the chat clients in
`providers/streaming.py` (all of them: OpenAI's protocol, Anthropic's,
NanoGPT's Private Mode, Tinfoil, Chutes), pictures, videos, embeddings and
Jev. What a call was for comes from the story's API log: logging a request
labels the next call of that model on the same thread (`label`).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

KEPT = 5000

# What kind of call.
TEXT = "Text"
PICTURE = "Picture"
VIDEO = "Video"
EMBEDDINGS = "Embeddings"
DECISION = "Decision"

# How a call's content travelled.
PLAIN_HTTPS = "HTTPS only: the provider can read it"
PLAIN_LOCAL = "Unencrypted, on your own network"
ON_THIS_COMPUTER = "Never left this computer"
# What a speed test's or a host check's call is for (they keep no API log).
SPEED_TEST = "speed_test"
# A picture or video endpoint's service, by the protocol it speaks.
MEDIA_SERVICES = {
    "nanogpt": "NanoGPT",
    "openrouter": "OpenRouter",
    "wavespeed": "WaveSpeed",
    "openai": "OpenAI-compatible",
    "comfyui": "ComfyUI",
    "a1111": "AUTOMATIC1111 / Forge",
}
# A label older than this, for a call whose model it can't check, isn't
# taken: it is another call's.
LABEL_SECONDS = 10.0


@dataclass
class Call:
    number: int
    started: float  # time.time()
    kind: str
    model: str
    endpoint: str  # the endpoint's name in Settings, or the service's
    host: str
    protection: str
    purpose: str = ""  # the API log's kind of request ("scene_request"), or ""
    route: str = ""  # the host or order asked of NanoGPT, if one was
    seconds: float | None = None
    status: str = "running"  # running, done, failed, stopped
    detail: str = ""  # why it failed, or how it ended
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_written: int = 0
    cost: float | None = None  # as the endpoint stated it
    extra: dict = field(default_factory=dict)


class CallLog:
    def __init__(self, kept: int = KEPT) -> None:
        self._lock = threading.Lock()
        self._calls: deque[Call] = deque(maxlen=kept)
        self._number = 0
        # Moves on every change, for a view to tell when to redraw.
        self.version = 0
        self._labels = threading.local()

    def start(
        self,
        kind: str,
        model: str,
        *,
        endpoint: str,
        address: str,
        protection: str,
        route: str = "",
        purpose: str = "",
    ) -> Call:
        purpose = purpose or self._take_label(model)
        with self._lock:
            self._number += 1
            call = Call(
                number=self._number,
                started=time.time(),
                kind=kind,
                model=model,
                endpoint=endpoint,
                host=host_of(address),
                protection=protection,
                purpose=purpose,
                route=route,
            )
            self._calls.append(call)
            self.version += 1
        return call

    def finish(
        self,
        call: Call,
        status: str,
        *,
        detail: str = "",
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cache_read: int | None = None,
        cache_written: int | None = None,
        cost: float | None = None,
        protection: str | None = None,
    ) -> None:
        with self._lock:
            if call.status != "running":
                return
            call.status = status
            call.detail = detail[:300]
            call.seconds = time.time() - call.started
            for name, value in (
                ("tokens_in", tokens_in),
                ("tokens_out", tokens_out),
                ("cache_read", cache_read),
                ("cache_written", cache_written),
            ):
                if value is not None:
                    setattr(call, name, int(value))
            if cost is not None:
                call.cost = float(cost)
            if protection:
                call.protection = protection
            self.version += 1

    def calls(self) -> list[Call]:
        with self._lock:
            return list(self._calls)

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()
            self.version += 1

    # --- what a call was for --------------------------------------------------

    def built(self, request: object) -> None:
        """A chat request whose body was just built on this thread: the API
        log writes it next, and so says what it is for (`label`), however
        many threads away it is then sent."""
        self._labels.built = request

    def label(self, purpose: str, model: str = "") -> None:
        """What a call of `model` is for: the request just built on this
        thread, when it is that model's (it carries it wherever it is sent),
        else the next call this thread makes of `model` (any model when
        blank)."""
        request = getattr(self._labels, "built", None)
        self._labels.built = None
        if request is not None and (
            not model or bare(getattr(request, "model", "")) == bare(model)
        ):
            try:
                object.__setattr__(request, "purpose", purpose)
                return
            except (AttributeError, TypeError):
                pass
        self._labels.value = (purpose, bare(model), time.monotonic())

    def _take_label(self, model: str) -> str:
        found = getattr(self._labels, "value", None)
        if found is None:
            return ""
        purpose, labelled, at = found
        if labelled:
            if labelled != bare(model):
                return ""
        elif time.monotonic() - at > LABEL_SECONDS:
            self._labels.value = None
            return ""
        self._labels.value = None
        return purpose


def bare(model: str) -> str:
    """A model's id without the endpoint a reference names ("Name::id")."""
    return (model or "").split("::", 1)[-1].strip()


def host_of(address: str) -> str:
    try:
        return (urlparse(address).hostname or "").lower()
    except ValueError:
        return ""


def plain_protection(address: str) -> str:
    """How a call to an ordinary endpoint travels."""
    try:
        scheme = urlparse(address).scheme.lower()
    except ValueError:
        scheme = ""
    return PLAIN_LOCAL if scheme == "http" else PLAIN_HTTPS


def route_words(body: object) -> str:
    """What a NanoGPT `provider` object asked for, in a few words."""
    if not isinstance(body, dict) or not body:
        return ""
    parts = []
    hosts = body.get("only") or body.get("order")
    if isinstance(hosts, list) and hosts:
        parts.append(("only " if body.get("only") else "") + ", ".join(map(str, hosts)))
    sort = body.get("sort")
    if isinstance(sort, str) and sort:
        parts.append(f"by {sort}")
    if body.get("min_quantization"):
        parts.append(f"{body['min_quantization']}+")
    return " · ".join(parts)


# The one log of the run.
CALLS = CallLog()
