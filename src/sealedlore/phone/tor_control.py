"""Tor's control protocol, as much as phone access speaks of it.

Pure: bytes in, replies out; commands as text. The connection itself is the
window's (gui/tor_service.py). What is used, from tor's control spec:

    PROTOCOLINFO 1                      where the cookie is
    AUTHCHALLENGE SAFECOOKIE <nonce>    and AUTHENTICATE <hash>: the cookie is
                                        never sent, and tor proves it read it
    TAKEOWNERSHIP                       tor exits when this connection closes
    SETEVENTS STATUS_CLIENT HS_DESC     bootstrap progress; the descriptor uploaded
    GETINFO status/bootstrap-phase      progress so far
    ADD_ONION …                         the onion service, for as long as
                                        this connection lives

Checked against tor 0.4.9.14 on the author's machine (SealedLoreDocs
design/phone.md).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field

SERVER_TO_CONTROLLER = b"Tor safe cookie authentication server-to-controller hash"
CONTROLLER_TO_SERVER = b"Tor safe cookie authentication controller-to-server hash"
COOKIE_LENGTH = 32
_SERVICE_ID = re.compile(r"[a-z2-7]{56}")


class ControlError(Exception):
    """Tor said no, or said something that can't be read."""


@dataclass
class Reply:
    """One reply: its status, its lines (without the code and separator) and
    the data blocks a `250+` line carried. An async event is code 650."""

    status: int
    lines: list[str] = field(default_factory=list)
    data: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 250

    @property
    def event(self) -> bool:
        return self.status == 650

    def text(self) -> str:
        return " ".join(self.lines)


class ReplyReader:
    """Lines in as bytes come; whole replies out, in order."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lines: list[str] = []
        self._data: dict[str, str] = {}
        self._status: int | None = None
        self._block: tuple[str, list[str]] | None = None  # a 250+ block being read

    def feed(self, data: bytes) -> list[Reply]:
        self._buffer += data
        replies: list[Reply] = []
        while True:
            end = self._buffer.find(b"\r\n")
            if end == -1:
                if len(self._buffer) > 1_000_000:
                    raise ControlError("a control reply line with no end")
                return replies
            raw = self._buffer[:end].decode("utf-8", "replace")
            del self._buffer[: end + 2]
            reply = self._line(raw)
            if reply is not None:
                replies.append(reply)

    def _line(self, raw: str) -> Reply | None:
        if self._block is not None:
            key, lines = self._block
            if raw == ".":
                self._data[key] = "\n".join(lines)
                self._block = None
            else:
                lines.append(raw[1:] if raw.startswith("..") else raw)
            return None
        if len(raw) < 4 or not raw[:3].isdigit() or raw[3] not in " -+":
            raise ControlError(f"a control reply that can't be read: {raw[:80]!r}")
        status, kind, rest = int(raw[:3]), raw[3], raw[4:]
        self._status = status
        if kind == "+":
            key, _, first = rest.partition("=")
            self._lines.append(key)
            self._block = (key, [first] if first else [])
            return None
        self._lines.append(rest)
        if kind == "-":
            return None
        reply = Reply(status, self._lines, self._data)
        self._lines, self._data, self._status = [], {}, None
        return reply


# --- commands -----------------------------------------------------------------------


def quoted(value: str) -> str:
    """A QuotedString as the control protocol (and torrc) reads one."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def new_nonce() -> bytes:
    return secrets.token_bytes(32)


def authchallenge(client_nonce: bytes) -> str:
    return f"AUTHCHALLENGE SAFECOOKIE {client_nonce.hex()}"


def safecookie_hashes(
    cookie: bytes, client_nonce: bytes, server_nonce: bytes
) -> tuple[bytes, bytes]:
    """(the hash tor must have sent, the hash we send back)."""
    message = cookie + client_nonce + server_nonce
    server = hmac.new(SERVER_TO_CONTROLLER, message, hashlib.sha256).digest()
    client = hmac.new(CONTROLLER_TO_SERVER, message, hashlib.sha256).digest()
    return server, client


def authenticate(cookie: bytes, client_nonce: bytes, challenge: Reply) -> str:
    """The AUTHENTICATE command for tor's AUTHCHALLENGE reply, once tor has
    shown it read the same cookie: a server hash that doesn't match means
    something else answered on the socket."""
    if not challenge.ok:
        raise ControlError(f"tor refused the challenge: {challenge.text()}")
    fields = _fields(challenge.text())
    try:
        server_hash = bytes.fromhex(fields["SERVERHASH"])
        server_nonce = bytes.fromhex(fields["SERVERNONCE"])
    except (KeyError, ValueError) as exc:
        raise ControlError("tor's challenge has no hash and nonce") from exc
    expected, client_hash = safecookie_hashes(cookie, client_nonce, server_nonce)
    if not hmac.compare_digest(expected, server_hash):
        raise ControlError("the control socket didn't prove it is tor")
    return f"AUTHENTICATE {client_hash.hex()}"


def add_onion(key: str | None, target: str) -> str:
    """The onion service on port 80 to `target` ("unix:/path"), with a key
    kept from before or a new one. Not detached: it lives as long as the
    control connection, so it can't outlive SealedLore."""
    blob = key if key else "NEW:ED25519-V3"
    return (
        f"ADD_ONION {blob} Flags=MaxStreamsCloseCircuit MaxStreams=16 "
        f"PoWDefensesEnabled=1 Port=80,{target}"
    )


# --- what tor says ----------------------------------------------------------------------


def cookie_file(protocolinfo: Reply) -> str:
    """Where tor keeps the cookie, from PROTOCOLINFO; SAFECOOKIE must be offered."""
    for line in protocolinfo.lines:
        if line.startswith("AUTH "):
            fields = _fields(line[5:])
            methods = fields.get("METHODS", "").split(",")
            if "SAFECOOKIE" not in methods:
                raise ControlError(f"tor offers no SAFECOOKIE login ({fields.get('METHODS')})")
            path = fields.get("COOKIEFILE")
            if path:
                return path
    raise ControlError("tor didn't say where its cookie is")


@dataclass(frozen=True)
class Bootstrap:
    progress: int
    summary: str


def bootstrap_of(text: str) -> Bootstrap | None:
    """From GETINFO status/bootstrap-phase's value or a STATUS_CLIENT event."""
    if "BOOTSTRAP" not in text:
        return None
    fields = _fields(text)
    try:
        progress = int(fields.get("PROGRESS", ""))
    except ValueError:
        return None
    return Bootstrap(max(0, min(100, progress)), fields.get("SUMMARY", ""))


@dataclass(frozen=True)
class Onion:
    service_id: str
    private_key: str | None  # given only when tor made it


def onion_of(reply: Reply) -> Onion:
    if not reply.ok:
        raise ControlError(f"tor refused the onion service: {reply.text()}")
    service_id, key = None, None
    for line in reply.lines:
        name, _, value = line.partition("=")
        if name == "ServiceID":
            service_id = value
        elif name == "PrivateKey":
            key = value
    if service_id is None or not _SERVICE_ID.fullmatch(service_id):
        raise ControlError("tor gave no onion address")
    return Onion(service_id, key)


def descriptor_uploaded(event: Reply, service_id: str) -> bool:
    """An HS_DESC event saying the descriptor reached a directory."""
    words = event.text().split()
    return (
        event.event
        and len(words) >= 3
        and words[0] == "HS_DESC"
        and words[1] == "UPLOADED"
        and words[2] == service_id
    )


def _fields(text: str) -> dict[str, str]:
    """KEY=value and KEY="quoted value" pairs, anywhere in a line."""
    out: dict[str, str] = {}
    for match in re.finditer(r'([A-Za-z_]+)=("(?:[^"\\]|\\.)*"|\S*)', text):
        value = match.group(2)
        if value.startswith('"'):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out[match.group(1)] = value
    return out
