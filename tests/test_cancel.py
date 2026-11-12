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


def run_and_cancel(
    provider: OpenAICompatibleProvider, after: float, *, first_text: bool = False
) -> tuple[list, object, float]:
    """Stream in a thread and cancel `after` seconds in; with `first_text`,
    `after` seconds once the first text has come (at most 5 s waited for it):
    on the lab's Windows 11 a new client's first chunk took 0.6-0.8 s, and a
    fixed half second cancelled before anything had arrived."""
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
    if first_text:
        deadline = time.monotonic() + 5
        while not any(isinstance(e, TextDelta) for e in events) and time.monotonic() < deadline:
            time.sleep(0.01)
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
    events, error, took = run_and_cancel(provider, after=0.3, first_text=True)
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


def test_stop_ends_every_call_made_side_by_side(server: str):
    """The reads after a passage go out at once, each on a client of its own
    (`side_by_side`): a client tracks one request, so a third read sharing
    the second's client was out of a Stop's reach, and the worker waited for
    it to finish by itself."""
    from sealedlore.models.config import Config
    from sealedlore.providers.base import side_by_side
    from sealedlore.providers.endpoints import EndpointsProvider

    settings = ProviderConfig(name="t", base_url=server + "/stall", api_key="k", timeout_seconds=30)
    for provider in (
        OpenAICompatibleProvider(settings),
        EndpointsProvider(Config(providers=[settings], active_provider_name="t")),
    ):
        lines = side_by_side(provider, 3)
        assert len({id(line) for line in lines}) == 3
        errors: list[BaseException] = []

        def call(line, errors: list[BaseException] = errors) -> None:
            try:
                line.complete(request())
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=call, args=(line,)) for line in lines]
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        started = time.monotonic()
        provider.cancel()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads), "a call was out of reach"
        assert len(errors) == 3 and all(isinstance(e, StreamCancelled) for e in errors)
        assert time.monotonic() - started < 2
        provider.reset_cancel()
        provider.close()


class _Stream:
    """httpcore's network stream as the trace hook sees it."""

    def __init__(self, sock) -> None:
        self.sock = sock

    def get_extra_info(self, name: str):
        return self.sock if name == "socket" else None


# What httpcore traces as a request opens its connection: direct, through an
# HTTP proxy's tunnel (HTTPS_PROXY), and through SOCKS. The step after the TCP
# connect wraps its socket in TLS, which detaches the one it wrapped.
ROUTES = {
    "direct": ("connection.connect_tcp.complete", "connection.start_tls.complete"),
    "http proxy": ("connection.connect_tcp.complete", "proxy.start_tls.complete"),
    "socks": ("socks.connect_tcp.complete", "socks.start_tls.complete"),
}


@pytest.mark.parametrize("route", ROUTES)
def test_stop_shuts_down_the_socket_the_reply_is_read_from_however_it_was_opened(route):
    """Behind a proxy the TLS socket came from the proxy's own step, which the
    hook didn't know: Stop shut down the TCP socket TLS had detached (a
    swallowed EBADF) and waited for the next chunk, and a picture's wait ran
    to its timeout."""
    import socket

    from sealedlore.providers.media_http import MediaHttp

    connect, tls = ROUTES[route]
    for make in (
        lambda: provider_at("https://example.test/v1"),
        lambda: MediaHttp("https://example.test/v1", "k", timeout_seconds=30),
    ):
        tcp, _tcp_peer = socket.socketpair()
        wrapped, peer = socket.socketpair()
        client = make()
        client._trace(connect, {"return_value": _Stream(tcp)})
        client._trace("http11.send_request_headers.complete", {"return_value": None})
        tcp.detach()  # as ssl's wrap_socket does
        client._trace(tls, {"return_value": _Stream(wrapped)})
        if hasattr(client, "_in_flight"):
            client._in_flight = True  # the request the trace belongs to
        client.cancel()
        peer.settimeout(1)
        assert peer.recv(1) == b"", "the socket read from was shut down"
        for sock in (wrapped, peer, _tcp_peer):
            sock.close()
        client.close()
