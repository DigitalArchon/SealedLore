"""What an endpoint sends back is read only so far (providers/http.py, the DA
apps' standard, Nov 14 2026): only gzip and deflate are asked for and read,
and a reply read whole is read to 32 MB at most, decoded bytes counted.
Before, httpx asked for brotli and zstd too wherever their packages were
installed, decoded whatever a server sent (each piece of a brotli or a
stacked reply could expand without bound), and read any reply but a stream
whole into memory, however large."""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from sealedlore.providers import http as http_module
from sealedlore.providers.http import (
    REPLY_MAX_BYTES,
    ReplyTooLarge,
    UnreadEncoding,
    make_client,
)

MB = 1024 * 1024


class Pieces(httpx.SyncByteStream):
    """A body sent a piece at a time, as a socket gives it: never read whole
    before the client reads it."""

    def __init__(self, *pieces: bytes) -> None:
        self.pieces = pieces
        self.sent = 0

    def __iter__(self):
        for piece in self.pieces:
            self.sent += len(piece)
            yield piece


def client_answering(stream: httpx.SyncByteStream, seen: list | None = None, **headers: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, headers=headers, stream=stream)

    return make_client("https://example.test", transport=httpx.MockTransport(handler))


def test_only_gzip_and_deflate_are_asked_for_whatever_httpx_could_decode(monkeypatch):
    """With brotli or zstandard installed (a source install may have them),
    httpx's own clients ask for br and zstd too."""
    import httpx._client as httpx_client

    monkeypatch.setattr(httpx_client, "ACCEPT_ENCODING", "gzip, deflate, br, zstd")
    seen: list[httpx.Request] = []
    client_answering(Pieces(b"{}"), seen).get("https://example.test/models")
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"
    plain: list[httpx.Request] = []
    httpx.Client(
        transport=httpx.MockTransport(lambda r: plain.append(r) or httpx.Response(200))
    ).get("https://example.test/")
    assert "br" in plain[0].headers["accept-encoding"], "what make_client keeps from happening"


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, gzip", "gzip, deflate", "deflate,br"])
def test_a_reply_compressed_any_other_way_is_refused_unread(encoding):
    body = Pieces(b"anything")
    with pytest.raises(UnreadEncoding, match="only gzip or deflate, once"):
        client_answering(body, **{"content-encoding": encoding}).get("https://example.test/")
    assert body.sent == 0, "refused before a byte was read"
    # A stream is refused alike: before the body, as the headers come.
    with pytest.raises(UnreadEncoding):
        with client_answering(Pieces(b"x"), **{"content-encoding": encoding}).stream(
            "GET", "https://example.test/"
        ):
            pass


@pytest.mark.parametrize("encoding", ["gzip", "identity", "GZIP", "x-unknown"])
def test_gzip_once_is_read_and_a_name_httpx_doesnt_know_passes_as_it_is(encoding):
    raw = b'{"data": []}'
    sent = gzip.compress(raw) if encoding.lower() == "gzip" else raw
    response = client_answering(Pieces(sent), **{"content-encoding": encoding}).get(
        "https://example.test/models"
    )
    assert response.json() == {"data": []}


def test_a_reply_read_whole_stops_past_32_mb():
    """Read a megabyte at a time: refused as the 33rd comes, the rest unread."""
    body = Pieces(*[bytes(MB)] * 40)
    with pytest.raises(ReplyTooLarge, match="more than 32 MB"):
        client_answering(body).get("https://example.test/models")
    assert body.sent <= REPLY_MAX_BYTES + MB


def test_the_limit_counts_what_gzip_decodes_to():
    """Forty megabytes of zeros gzip to about forty kilobytes."""
    bomb = gzip.compress(bytes(40 * MB))
    assert len(bomb) < MB
    with pytest.raises(ReplyTooLarge):
        client_answering(Pieces(bomb), **{"content-encoding": "gzip"}).get("https://example.test/")


def test_a_length_declared_past_32_mb_is_refused_before_reading():
    body = Pieces(b"{}")
    with pytest.raises(ReplyTooLarge):
        client_answering(body, **{"content-length": str(40 * MB)}).get("https://example.test/")
    assert body.sent == 0
    # A HEAD's length is of a body it never sends.
    head = client_answering(Pieces(), **{"content-length": str(40 * MB)})
    assert head.head("https://example.test/").status_code == 200


def test_a_reply_within_it_reads_as_httpx_would_read_it():
    """The client keeps what it read where httpx's own `read` keeps it: a
    change of httpx's that broke this fails here, not in a listing."""
    listing = json.dumps({"data": [{"id": "m"}]}).encode()
    response = client_answering(Pieces(listing[:5], listing[5:])).get("https://example.test/")
    assert response.content == listing and response.read() == listing
    assert response.json() == {"data": [{"id": "m"}]} and response.text == listing.decode()
    assert response.is_closed
    exactly = client_answering(Pieces(bytes(REPLY_MAX_BYTES))).get("https://example.test/")
    assert len(exactly.content) == REPLY_MAX_BYTES


def test_a_stream_is_read_by_its_reader_not_held_to_the_limit():
    """A stream's lines (streaming.read_lines) and a download's body
    (media_http) have limits of their own."""
    client = client_answering(Pieces(*[bytes(MB)] * 33))
    with client.stream("GET", "https://example.test/") as response:
        assert sum(len(piece) for piece in response.iter_bytes()) == 33 * MB


def test_every_client_the_app_builds_is_this_one():
    """`make_client` is the one place (providers/http.py); test_beta_review
    holds src/ to it."""
    assert isinstance(make_client("https://example.test"), http_module._Client)


def test_a_refusals_body_is_read_to_64_kb_its_start_kept_and_the_rest_unread():
    """An error body at most 64 KB, read only that far (the DA apps'
    standard): what the endpoint said, never however much it sent."""
    from sealedlore.providers.http import ERROR_BODY_BYTES

    words = b'{"error": {"message": "no such model"}}'
    body = Pieces(words + b" " * MB, *[bytes(MB)] * 40)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-length": str(41 * MB)}, stream=body)

    client = make_client("https://example.test", transport=httpx.MockTransport(handler))
    response = client.get("https://example.test/models")
    assert response.status_code == 404 and len(response.content) == ERROR_BODY_BYTES
    assert response.content.startswith(words)
    assert body.sent <= 2 * MB, "the rest left unread"


def test_a_redirects_own_body_is_read_to_64_kb_followed_or_not():
    """httpx reads a redirect it follows whole before following it, where the
    client's own reading never sees it: a 302 sending megabytes is read to
    64 KB, its start kept and the rest unread, and the redirect still goes."""
    sent: list[Pieces] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved":
            body = Pieces(*[b"r" * MB for _ in range(8)])
            sent.append(body)
            return httpx.Response(302, headers={"Location": "/here"}, stream=body)
        return httpx.Response(200, content=b"fine")

    client = make_client("https://example.test", transport=httpx.MockTransport(handler))
    followed = client.get("https://example.test/moved", follow_redirects=True)
    assert followed.text == "fine"
    assert len(followed.history[0].content) == http_module.ERROR_BODY_BYTES == 64 * 1024
    assert sent[0].sent == MB, "one piece read of eight"
    not_followed = client.get("https://example.test/moved")
    assert not_followed.status_code == 302 and len(not_followed.content) == 64 * 1024
    assert sent[1].sent == MB
