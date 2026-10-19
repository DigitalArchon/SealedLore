"""The event stream to the phones: what happens on the desktop, as it happens.

Each event is `event: <name>` and one `data:` line of JSON; the page reads
the stream with fetch, since EventSource can't send the token. Events that
carry anything of the story go out only while the gate says the open story is
shared with phones (`content=True`); the others (the stream's own state,
a revoked phone) go regardless. Writers are the transport's: one per
connection, returning False when that connection is to be dropped (closed,
or too far behind). A subscriber the hub lets go of is closed through its
`close`, so the transport ends the connection too.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass

MAX_STREAMS = 8
MAX_STREAMS_PER_DEVICE = 2
HEARTBEAT = b": still here\n\n"

Writer = Callable[[bytes], bool]


def frame(event: str, data: object) -> bytes:
    """One event. JSON keeps any newline in the text inside its one line."""
    if not event or any(ch in event for ch in "\r\n:"):
        raise ValueError(f"bad event name {event!r}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


@dataclass
class _Subscriber:
    device: str
    write: Writer
    close: Callable[[], None]


class EventHub:
    def __init__(self, gate: Callable[[], bool]) -> None:
        self._gate = gate
        self._subscribers: dict[int, _Subscriber] = {}
        self._ids = itertools.count(1)

    def can_subscribe(self, device: str) -> bool:
        return len(self._subscribers) < MAX_STREAMS and self.count(device) < MAX_STREAMS_PER_DEVICE

    def subscribe(self, device: str, write: Writer, close: Callable[[], None]) -> int:
        handle = next(self._ids)
        self._subscribers[handle] = _Subscriber(device, write, close)
        return handle

    def unsubscribe(self, handle: int) -> None:
        """The transport's: its connection has gone."""
        self._subscribers.pop(handle, None)

    def _let_go(self, handle: int) -> None:
        subscriber = self._subscribers.pop(handle, None)
        if subscriber is not None:
            subscriber.close()

    def count(self, device: str | None = None) -> int:
        return sum(1 for s in self._subscribers.values() if device is None or s.device == device)

    def devices(self) -> set[str]:
        return {s.device for s in self._subscribers.values()}

    def send_to(self, handle: int, event: str, data: object) -> None:
        """To one subscriber: the hello a new stream starts with."""
        subscriber = self._subscribers.get(handle)
        if subscriber is not None and not subscriber.write(frame(event, data)):
            self._let_go(handle)

    def publish(self, event: str, data: object, *, content: bool = True) -> None:
        if content and not self._gate():
            return
        self._write_all(frame(event, data))

    def heartbeat(self) -> None:
        self._write_all(HEARTBEAT)

    def drop_device(self, device: str, event: str = "revoked") -> None:
        """A phone no longer allowed: told so, and its streams closed."""
        message = frame(event, {})
        for handle, subscriber in list(self._subscribers.items()):
            if subscriber.device == device:
                subscriber.write(message)
                self._let_go(handle)

    def drop_all(self, event: str = "stopped") -> None:
        message = frame(event, {})
        for handle, subscriber in list(self._subscribers.items()):
            subscriber.write(message)
            self._let_go(handle)

    def _write_all(self, data: bytes) -> None:
        for handle, subscriber in list(self._subscribers.items()):
            if not subscriber.write(data):
                self._let_go(handle)
