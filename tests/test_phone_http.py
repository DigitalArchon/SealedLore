"""The phone page's HTTP (phone/http.py): reading requests as bytes come,
refusing what can't be read, and the headers every response carries."""

from __future__ import annotations

import pytest

from sealedlore.phone.http import (
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    SECURITY_HEADERS,
    HttpError,
    RequestParser,
    Response,
    host_matches,
    json_response,
    render,
)

ONION = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz2345.onion"


def head(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def get(path: str = "/api/state", **extra: str) -> bytes:
    lines = [f"GET {path} HTTP/1.1", f"Host: {ONION}"]
    lines += [f"{k.replace('_', '-')}: {v}" for k, v in extra.items()]
    return head(lines)


def post(path: str, body: bytes, content_type: str = "application/json") -> bytes:
    return (
        head(
            [
                f"POST {path} HTTP/1.1",
                f"Host: {ONION}",
                f"Content-Type: {content_type}",
                f"Content-Length: {len(body)}",
            ]
        )
        + body
    )


def test_a_request_read_in_pieces():
    parser = RequestParser()
    data = post("/api/turn?x=1", b'{"text": "Hello"}')
    requests = []
    for i in range(len(data)):
        requests += parser.feed(data[i : i + 1])
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.path, request.query) == ("POST", "/api/turn", {"x": "1"})
    assert request.json() == {"text": "Hello"}
    assert request.keep_alive


def test_requests_one_after_another_on_one_connection():
    parser = RequestParser()
    requests = parser.feed(get("/a") + get("/b") + get("/c")[:10])
    assert [r.path for r in requests] == ["/a", "/b"]
    assert [r.path for r in parser.feed(get("/c")[10:])] == ["/c"]


def test_connection_close_and_http_1_0():
    parser = RequestParser()
    (closing,) = parser.feed(get(Connection="close"))
    assert not closing.keep_alive
    (old,) = RequestParser().feed(head(["GET / HTTP/1.0"]))
    assert not old.keep_alive


@pytest.mark.parametrize(
    "data, status",
    [
        (head(["GET /api/state"]), 400),
        (head(["GET /api/state HTTP/2.0", f"Host: {ONION}"]), 505),
        (head(["GET http://evil/ HTTP/1.1", f"Host: {ONION}"]), 400),
        (head(["GET /api/state HTTP/1.1"]), 400),  # no Host
        (head(["GET /api/state HTTP/1.1", f"Host: {ONION}", f"Host: {ONION}"]), 400),
        (head(["GET /api/state HTTP/1.1", f"Host: {ONION}", " folded"]), 400),
        (head(["GET /api/state HTTP/1.1", f"Host: {ONION}", "Bad Header: x"]), 400),
        (head(["POST /api/turn HTTP/1.1", f"Host: {ONION}", "Transfer-Encoding: chunked"]), 501),
        (head(["POST /api/turn HTTP/1.1", f"Host: {ONION}", "Content-Length: -4"]), 400),
        (
            head(
                ["POST /api/t HTTP/1.1", f"Host: {ONION}", f"Content-Length: {MAX_BODY_BYTES + 1}"]
            ),
            413,
        ),
        ("GET /é HTTP/1.1\r\n\r\n".encode(), 400),
    ],
)
def test_what_cant_be_read_is_refused_with_a_status(data, status):
    with pytest.raises(HttpError) as caught:
        RequestParser().feed(data)
    assert caught.value.status == status


def test_a_head_that_never_ends_is_refused():
    parser = RequestParser()
    with pytest.raises(HttpError) as caught:
        parser.feed(b"GET / HTTP/1.1\r\nX: " + b"a" * (MAX_HEADER_BYTES + 1))
    assert caught.value.status == 431


def test_a_body_that_isnt_json_is_refused():
    (request,) = RequestParser().feed(post("/api/turn", b"not json"))
    with pytest.raises(HttpError) as caught:
        request.json()
    assert caught.value.status == 400
    (form,) = RequestParser().feed(post("/api/turn", b"a=b", "application/x-www-form-urlencoded"))
    with pytest.raises(HttpError) as caught:
        form.json()
    assert caught.value.status == 415


def test_only_the_onion_is_answered():
    def host(value: str):
        (request,) = RequestParser().feed(head(["GET / HTTP/1.1", f"Host: {value}"]))
        return host_matches(request, ONION)

    assert host(ONION)
    assert host(ONION.upper())
    assert host(f"{ONION}:80")
    assert not host("127.0.0.1")
    assert not host("localhost:8080")
    assert not host(f"{ONION}:8080")
    assert not host("evil.example")
    (request,) = RequestParser().feed(get())
    assert not host_matches(request, "")  # no address yet: nothing is answered


def test_every_response_carries_the_security_headers():
    for response in (
        json_response(200, {"a": 1}),
        json_response(404, {}),
        Response(200, b"x", "text/html"),
    ):
        raw = render(response, keep_alive=True).decode()
        for name, value in SECURITY_HEADERS.items():
            assert f"\r\n{name}: {value}\r\n" in raw
    assert "Cache-Control: no-store" in raw
    assert "frame-ancestors 'none'" in raw


def test_a_response_is_sized_and_says_whether_the_connection_stays():
    raw = render(json_response(200, {"text": "é"}), keep_alive=False)
    head_part, _, body = raw.partition(b"\r\n\r\n")
    assert body == '{"text":"é"}'.encode()
    assert f"Content-Length: {len(body)}".encode() in head_part
    assert b"Connection: close" in head_part
    assert b"Content-Type: application/json; charset=utf-8" in head_part
    assert b"Connection: keep-alive" in render(json_response(200, {}), keep_alive=True)


def test_an_event_stream_is_headers_only_and_ends_with_its_connection():
    raw = render(Response(200, content_type="text/event-stream", stream=True), keep_alive=True)
    assert raw.endswith(b"\r\n\r\n")
    assert b"Content-Length" not in raw
    assert b"Connection: close" in raw
