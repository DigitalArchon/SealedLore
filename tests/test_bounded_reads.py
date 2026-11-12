"""What is read from an endpoint is bounded (providers/streaming.py,
providers/media_http.py): a stream's lines, a refusal's body and a picture or
video downloaded, each read a piece at a time and only so far; and a stream
chunk of the wrong shape is the endpoint's malformed chunk, never a TypeError."""

from __future__ import annotations

import gzip
import json
import time

import httpx
import pytest

from sealedlore.models.config import ProviderConfig
from sealedlore.providers import media_http, streaming
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCompleted
from sealedlore.providers.media_http import MediaHttp
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.streaming import read_lines


class Pieces:
    """A body served a piece at a time, counting how many were taken."""

    def __init__(self, pieces) -> None:
        self._pieces = iter(pieces)
        self.taken = 0

    def __iter__(self):
        for piece in self._pieces:
            self.taken += 1
            yield piece


def streamed(pieces, status: int = 200, headers: dict | None = None) -> httpx.Response:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=pieces, headers=headers or {})
        )
    )
    return client.send(client.build_request("GET", "https://x.test/"), stream=True)


def provider(pieces, status: int = 200) -> OpenAICompatibleProvider:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=pieces))
    return OpenAICompatibleProvider(
        ProviderConfig(name="p", base_url="https://x.test/v1", model="m"),
        client=httpx.Client(transport=transport),
    )


def run(provider: OpenAICompatibleProvider) -> list:
    return list(provider.stream(ChatRequest(model="m", messages=[])))


# --- a stream's lines -----------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_lines_read_in_any_pieces_are_the_lines_iter_lines_gives(size):
    text = "data: one\r\ndata: two\rdata: thrée\n\ndata: four\r\n\r\nlast"
    data = text.encode()
    pieces = [data[i : i + size] for i in range(0, len(data), size)]
    expected = list(streamed([data]).iter_lines())
    assert list(read_lines(streamed(pieces))) == expected
    assert expected[-1] == "last"


def test_a_long_line_costs_its_length_not_its_length_squared():
    """Every piece was added to the line so far and the whole of it split
    again: a megabyte-long line in small pieces took tens of seconds."""
    data = b"data: " + b"x" * (1024 * 1024) + b"\n"
    pieces = [data[i : i + 32] for i in range(0, len(data), 32)]
    started = time.monotonic()
    (line,) = read_lines(streamed(pieces))
    assert len(line) == len(data) - 1
    assert time.monotonic() - started < 5.0


def test_a_line_that_never_ends_is_refused_before_it_fills_memory(monkeypatch):
    monkeypatch.setattr(streaming, "MAX_LINE_CHARS", 10_000)
    body = Pieces([b"data: " + b"x" * 1000] + [b"x" * 1000] * 10_000)
    with pytest.raises(ProviderError, match="a line of more than"):
        run(provider(body))
    assert body.taken < 20, "read no further than the limit"


def test_a_stream_still_carries_its_reply():
    stream = (
        b'data: {"id": "c1", "choices": [{"delta": {"reasoning": "hm", "content": "Hel"}}]}\r\n'
        b"\r\n"
        b'data: {"choices": [{"delta": {"content": [{"text": "lo"}]}, "finish_reason": "stop"}]}'
        b"\n\ndata: [DONE]\n\n"
    )
    events = run(provider([stream[i : i + 5] for i in range(0, len(stream), 5)]))
    assert "".join(e.text for e in events if type(e).__name__ == "TextDelta") == "Hello"
    done = events[-1]
    assert isinstance(done, StreamCompleted)
    assert (done.response_id, done.finish_reason) == ("c1", "stop")


# --- shapes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunk",
    [
        {"choices": 5},
        {"choices": {"delta": {"content": "x"}}},
        {"choices": "abc"},
        {"choices": [{"delta": {"reasoning": 5}}]},
        {"choices": [{"delta": {"reasoning": {"text": "x"}}}]},
        {"choices": [{"delta": {"reasoning_content": ["x"]}}]},
        {"choices": [{"delta": {"content": [{"text": 5}]}}]},
        {"choices": [{"finish_reason": 7}]},
    ],
)
def test_a_chunk_of_the_wrong_shape_is_a_malformed_chunk(chunk):
    """A number for `choices` or `reasoning` raised TypeError (no reply, and
    no word of why), and a dict for `reasoning` went on as a reasoning
    "text" that broke whatever joined it."""
    stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
    with pytest.raises(ProviderError, match="malformed stream chunk"):
        run(provider([stream]))


def test_a_chunk_nested_past_pythons_reach_is_a_malformed_chunk():
    stream = b"data: " + b"[" * 100_000 + b"]" * 100_000 + b"\n\n"
    with pytest.raises(ProviderError, match="malformed stream chunk"):
        run(provider([stream]))


# --- refusals -------------------------------------------------------------------------


def test_a_refusal_is_read_only_as_far_as_its_words():
    body = Pieces([b'{"error": "no"}' + b" " * 1000] + [b" " * 1024] * 50_000)
    with pytest.raises(ProviderError) as caught:
        run(provider(body, status=400))
    assert caught.value.status_code == 400
    assert caught.value.body.startswith('{"error": "no"}')
    assert len(caught.value.body) <= streaming.ERROR_BODY_BYTES
    assert body.taken < 100, "the rest of a 50 MB refusal was never read"


# --- pictures and videos --------------------------------------------------------------


def media(handler) -> MediaHttp:
    return MediaHttp(
        "https://media.test/v1",
        "key",
        timeout_seconds=30,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_a_download_past_the_limit_is_refused_and_read_no_further(monkeypatch):
    """A picture or video was read whole into memory, however large."""
    monkeypatch.setattr(media_http, "MEDIA_MAX_BYTES", 1024 * 1024)
    body = Pieces([b"x" * 65536] * 1000)
    http = media(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ProviderError, match="more than 1 MB"):
        http.request("GET", "https://storage.test/video.mp4", authorised=False)
    assert body.taken <= 17


def test_a_download_said_to_be_too_large_is_refused_unread(monkeypatch):
    monkeypatch.setattr(media_http, "MEDIA_MAX_BYTES", 1024 * 1024)
    body = Pieces([b"x" * 10])
    http = media(
        lambda request: httpx.Response(
            200, content=body, headers={"content-length": str(50 * 1024 * 1024)}
        )
    )
    with pytest.raises(ProviderError, match="more than 1 MB"):
        http.request("GET", "https://storage.test/p.png", authorised=False)
    assert body.taken == 0


def test_a_reply_within_the_limit_reads_as_before_compressed_or_not():
    picture = b"\x89PNG" + bytes(range(256)) * 100
    http = media(lambda request: httpx.Response(200, content=picture))
    assert http.request("GET", "https://storage.test/p.png", authorised=False).content == picture
    packed = gzip.compress(json.dumps({"data": [{"url": "u"}]}).encode())
    http = media(
        lambda request: httpx.Response(200, content=packed, headers={"content-encoding": "gzip"})
    )
    assert http.json("GET", "https://media.test/v1/status") == {"data": [{"url": "u"}]}


def test_a_media_refusal_is_read_only_as_far_as_its_words():
    body = Pieces([b"no such job" + b" " * 1000] + [b" " * 1024] * 50_000)
    http = media(lambda request: httpx.Response(404, content=body))
    with pytest.raises(ProviderError, match="HTTP 404: no such job") as caught:
        http.request("GET", "https://media.test/v1/status")
    assert caught.value.status_code == 404
    assert body.taken < 100
