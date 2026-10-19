"""The HTTP the phone's page speaks: requests read from bytes as they come,
responses written with the same security headers every time.

Only what the page needs: GET and POST, a body by Content-Length, kept-alive
connections (a new stream over Tor costs a round trip to the onion), and an
event stream that holds its connection until it closes. Anything else is
refused with a status, never guessed at: the other end is on the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import parse_qsl

MAX_HEADER_BYTES = 8 * 1024
MAX_BODY_BYTES = 64 * 1024

# On every response, errors included. The page loads nothing from anywhere
# else, runs no inline script, is framed by nothing, and keeps nothing.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Server": "SealedLore",
}

_TOKEN_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class HttpError(Exception):
    """A request that can't be read. The connection answers with the status
    and closes."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(detail or HTTPStatus(status).phrase)
        self.status = status
        self.detail = detail or HTTPStatus(status).phrase


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]  # names in lower case
    body: bytes
    version: str  # "HTTP/1.1" or "HTTP/1.0"

    @property
    def keep_alive(self) -> bool:
        tokens = {t.strip().lower() for t in self.headers.get("connection", "").split(",")}
        if self.version == "HTTP/1.0":
            return "keep-alive" in tokens
        return "close" not in tokens

    def json(self) -> object:
        """The body as JSON. A body that isn't JSON is the client's error."""
        if self.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HttpError(415, "send application/json")
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(400, "the body isn't JSON") from exc


class RequestParser:
    """Bytes in, requests out. One per connection; requests come in order."""

    def __init__(
        self, *, max_header: int = MAX_HEADER_BYTES, max_body: int = MAX_BODY_BYTES
    ) -> None:
        self._buffer = bytearray()
        self._max_header = max_header
        self._max_body = max_body
        self._head: tuple[str, str, str, dict[str, str]] | None = None
        self._need = 0

    def feed(self, data: bytes) -> list[Request]:
        """The requests completed by `data`. Raises HttpError on the first
        that can't be read; nothing after it is read."""
        self._buffer += data
        out: list[Request] = []
        while True:
            if self._head is None:
                end = self._buffer.find(b"\r\n\r\n")
                if end == -1:
                    if len(self._buffer) > self._max_header:
                        raise HttpError(431)
                    return out
                if end > self._max_header:
                    raise HttpError(431)
                head = bytes(self._buffer[:end])
                del self._buffer[: end + 4]
                self._head = _parse_head(head)
                self._need = _body_length(self._head[3], self._max_body)
            if len(self._buffer) < self._need:
                return out
            method, target, version, headers = self._head
            body = bytes(self._buffer[: self._need])
            del self._buffer[: self._need]
            self._head = None
            path, _, query = target.partition("?")
            out.append(
                Request(
                    method=method,
                    path=path,
                    query=dict(parse_qsl(query, keep_blank_values=True)),
                    headers=headers,
                    body=body,
                    version=version,
                )
            )


def _parse_head(head: bytes) -> tuple[str, str, str, dict[str, str]]:
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HttpError(400, "the request head isn't ASCII") from exc
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HttpError(400, "a bad request line")
    method, target, version = parts
    if not method or not set(method) <= _TOKEN_CHARS:
        raise HttpError(400, "a bad method")
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(505)
    if not target.startswith("/") or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in target):
        raise HttpError(400, "a bad target")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[0] in " \t":
            raise HttpError(400, "a folded or empty header line")
        name, sep, value = line.partition(":")
        if not sep or not name or not set(name) <= _TOKEN_CHARS:
            raise HttpError(400, "a bad header")
        name = name.lower()
        value = value.strip(" \t")
        if name in headers:
            if name in ("host", "content-length", "authorization", "content-type"):
                raise HttpError(400, f"{name} given twice")
            headers[name] = f"{headers[name]}, {value}"
        else:
            headers[name] = value
    if version == "HTTP/1.1" and "host" not in headers:
        raise HttpError(400, "no Host")
    return method, target, version, headers


def _body_length(headers: dict[str, str], most: int) -> int:
    if "transfer-encoding" in headers:
        raise HttpError(501, "send a Content-Length, not a transfer encoding")
    value = headers.get("content-length")
    if value is None:
        return 0
    if not value.isdigit():
        raise HttpError(400, "a bad Content-Length")
    length = int(value)
    if length > most:
        raise HttpError(413)
    return length


def host_matches(request: Request, onion: str) -> bool:
    """Whether the request is for this onion: only Tor brings a request with
    it. A browser on the computer that found the socket some other way, or a
    page using DNS rebinding, names another host."""
    host = request.headers.get("host", "").strip().lower()
    onion = onion.lower()
    return bool(onion) and host in (onion, f"{onion}:80")


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    # An event stream: written with its headers, then held open for events,
    # its end the connection's end; `device` is the paired phone it is for.
    stream: bool = False
    device: str = ""


def json_response(status: int, value: object, **headers: str) -> Response:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, body, "application/json", dict(headers))


def error_response(status: int, detail: str = "") -> Response:
    return json_response(status, {"error": detail or HTTPStatus(status).phrase})


def render(response: Response, *, keep_alive: bool, head_only: bool = False) -> bytes:
    """The response as bytes: status line, headers, body."""
    phrase = HTTPStatus(response.status).phrase
    lines = [f"HTTP/1.1 {response.status} {phrase}"]
    headers = dict(SECURITY_HEADERS)
    headers.update(response.headers)
    content_type = response.content_type
    if content_type.startswith("text/") or content_type in (
        "application/json",
        "application/javascript",
    ):
        content_type += "; charset=utf-8"
    headers["Content-Type"] = content_type
    if response.stream:
        headers["Connection"] = "close"
    else:
        headers["Content-Length"] = str(len(response.body))
        headers["Connection"] = "keep-alive" if keep_alive else "close"
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    if response.stream or head_only:
        return head
    return head + response.body
