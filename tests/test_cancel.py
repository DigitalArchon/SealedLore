"""Stop aborts the request in flight at once, over a real socket.

The server here is on loopback, so nothing leaves the machine. The cases are
the two a stalled Stop used to wait out: a model slow to send its first byte,
and a stream that goes quiet partway.
"""

from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Iterator

import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import ChatRequest, StreamCancelled, StreamCompleted, TextDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

CHUNK = b'data: {"choices": [{"delta": {"content": "word "}}]}\n\n'


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.startswith("/stall"):
            time.sleep(10)  # never answers in time
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path.startswith("/quick"):
            self.wfile.write(CHUNK + b"data: [DONE]\n\n")
            return
        try:
            self.wfile.write(CHUNK)
            self.wfile.flush()
            time.sleep(10)  # then goes quiet
        except OSError:
            pass

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def provider_at(url: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(name="t", base_url=url, api_key="k", timeout_seconds=30)
    )


def request() -> ChatRequest:
    return ChatRequest(
        model="m", messages=[PromptMessage(role="user", parts=(ContentPart(text="hi"),))]
    )


def run_and_cancel(provider: OpenAICompatibleProvider, after: float) -> tuple[list, object, float]:
    events: list = []
    outcome: dict = {}

    def consume() -> None:
        try:
            for event in provider.stream(request()):
                events.append(event)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=consume)
    thread.start()
    time.sleep(after)
    started = time.monotonic()
    provider.cancel()
    thread.join(5)
    assert not thread.is_alive(), "cancel didn't end the stream"
    return events, outcome.get("error"), time.monotonic() - started


def test_cancel_ends_a_request_still_waiting_for_its_first_byte(server: str):
    provider = provider_at(server + "/stall")
    events, error, took = run_and_cancel(provider, after=0.5)
    assert isinstance(error, StreamCancelled)
    assert events == [] and took < 2


def test_cancel_ends_a_stream_gone_quiet_without_calling_it_complete(server: str):
    provider = provider_at(server + "/quiet")
    events, error, took = run_and_cancel(provider, after=0.5)
    # A cut-off stream must never pass for a finished one.
    assert isinstance(error, StreamCancelled)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["word "]
    assert not any(isinstance(e, StreamCompleted) for e in events)
    assert took < 2


def test_a_cancel_before_the_request_goes_out_still_stops_it(server: str):
    # Stop pressed while the job is still assembling its prompt: a review or
    # a summary streams nothing to notice it by, so the cancel has to wait.
    provider = provider_at(server + "/stall")
    provider.cancel()
    started = time.monotonic()
    with pytest.raises(StreamCancelled):
        list(provider.stream(request()))
    assert time.monotonic() - started < 1


def test_reset_clears_a_leftover_cancel_for_the_next_job(server: str):
    provider = provider_at(server + "/quick")
    provider.cancel()
    provider.reset_cancel()
    events = list(provider.stream(request()))
    assert isinstance(events[-1], StreamCompleted)
    # And a request after a cancelled one starts clean once reset.
    stalled = provider_at(server + "/stall")
    run_and_cancel(stalled, after=0.3)
    stalled.reset_cancel()
    stalled.config.base_url = server + "/quick"
    assert isinstance(list(stalled.stream(request()))[-1], StreamCompleted)


def test_the_mock_can_be_cancelled_mid_stream():
    provider = MockChatProvider(["word " * 50], chunk_size=5)
    stream = provider.stream(request())
    next(stream)
    provider.cancel()
    with pytest.raises(StreamCancelled):
        list(stream)
