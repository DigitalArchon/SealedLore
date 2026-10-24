"""The phone's API: who may ask, about which story, and what they may send.

Pure: a request in, a response out. The story is asked through a `Backend`,
which the window implements on the GUI thread. The rules that keep a story
to itself are here, once, for every route:

- every `/api/` route but pairing needs a paired phone's token;
- only stories the author shared reach a phone, and no visual story
  (version 1 is for text stories and chats). A story that isn't available is
  answered as if it didn't exist, and while one is open on the desktop every
  route about the open story answers 423 with no title;
- every value a phone sends is checked against what the composer offers now.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from sealedlore.phone.http import (
    HttpError,
    Request,
    Response,
    error_response,
    host_matches,
    json_response,
)
from sealedlore.phone.pairing import Claim, Pairing, PairingRefused, device_for
from sealedlore.phone.sse import EventHub

TEXT_LIMIT = 20_000
TRANSCRIPT_PAGE = 40
TRANSCRIPT_MOST = 200
AUTH_FAILURES = 20  # wrong tokens in a minute before every token waits
LOCKOUT_SECONDS = 60
_ID = re.compile(r"[A-Za-z0-9_.-]{1,96}")

NOT_SHARED = "The story open on the desktop isn't shared with this phone."
NO_STORY = "No story is open on the desktop."


@dataclass(frozen=True)
class StoryRow:
    id: str
    title: str
    messages: int
    chat: bool
    keeps_map: bool


@dataclass(frozen=True)
class PhoneTurn:
    text: str
    speaker: str  # one of the composer's speaker values; "question" asks one
    length: str | None = None
    outcome: str | None = None
    skill: str | None = None
    difficulty: str | None = None


@dataclass(frozen=True)
class Retake:
    node_id: str | None = None
    length: str | None = None
    outcome: str | None = None


class Refused(Exception):
    """The backend can't do it now: busy, the desktop waiting on an answer,
    a private scene's rule. The phone shows the reason."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Backend(Protocol):
    def open_story(self) -> StoryRow | None: ...

    def story_rows(self) -> list[StoryRow]: ...

    def shared_ids(self) -> Collection[str]: ...

    def devices(self) -> Iterable[tuple[str, str]]: ...

    def state(self) -> dict: ...

    def transcript(self, before: str | None, limit: int) -> dict: ...

    def composer(self) -> dict: ...

    def branches(self) -> dict: ...

    def picture(self, image_id: str, thumb: bool) -> tuple[bytes, str] | None: ...

    def open(self, story_id: str) -> None: ...

    def send(self, turn: PhoneTurn, device: str) -> dict: ...

    def stop(self) -> None: ...

    def regenerate(self, retake: Retake) -> None: ...

    def reroll(self, node_id: str) -> None: ...

    def switch_take(self, node_id: str, step: int) -> None: ...

    def switch_branch(self, start_id: str) -> None: ...

    def claimed(self, claim: Claim) -> None: ...

    def seen(self, device: str) -> None: ...

    def forget(self, device: str) -> None: ...

    def warn(self, message: str) -> None: ...


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    handler: Callable[..., Response]
    auth: bool = True
    story: bool = False  # about the open story: only while it is available


class Api:
    def __init__(
        self,
        backend: Backend,
        *,
        pairing: Pairing,
        onion: Callable[[], str],
        static: Mapping[str, tuple[bytes, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.pairing = pairing
        self._onion = onion
        self._static = dict(static or {})
        self._clock = clock
        self._failures: deque[float] = deque()
        self._locked_until = 0.0
        self.hub = EventHub(self.open_available)
        self._routes = [
            _Route("POST", re.compile(r"/api/pair"), self._pair, auth=False),
            _Route(
                "GET", re.compile(r"/api/pair/([A-Za-z0-9_-]{1,64})"), self._pair_poll, auth=False
            ),
            _Route("GET", re.compile(r"/api/state"), self._state),
            _Route("GET", re.compile(r"/api/stories"), self._stories),
            _Route("POST", re.compile(r"/api/stories/([^/]+)/open"), self._open),
            _Route("GET", re.compile(r"/api/events"), self._events),
            _Route("POST", re.compile(r"/api/forget"), self._forget),
            _Route("GET", re.compile(r"/api/transcript"), self._transcript, story=True),
            _Route("GET", re.compile(r"/api/composer"), self._composer, story=True),
            _Route("POST", re.compile(r"/api/turn"), self._turn, story=True),
            _Route("POST", re.compile(r"/api/stop"), self._stop, story=True),
            _Route("POST", re.compile(r"/api/regenerate"), self._regenerate, story=True),
            _Route("POST", re.compile(r"/api/reroll"), self._reroll, story=True),
            _Route("POST", re.compile(r"/api/take"), self._take, story=True),
            _Route("GET", re.compile(r"/api/branches"), self._branches, story=True),
            _Route("POST", re.compile(r"/api/branch"), self._branch, story=True),
            _Route(
                "GET",
                re.compile(r"/api/pictures/([A-Za-z0-9_.-]{1,96})"),
                self._picture,
                story=True,
            ),
        ]

    # --- availability --------------------------------------------------------

    def available(self, row: StoryRow) -> bool:
        return not row.keeps_map and row.id in set(self.backend.shared_ids())

    def open_available(self) -> bool:
        row = self.backend.open_story()
        return row is not None and self.available(row)

    def state_payload(self) -> dict:
        """The state a phone is told, by route and by event: nothing of a
        story that isn't available, not even its title."""
        row = self.backend.open_story()
        if row is None:
            return {"story": None, "shared": False}
        if not self.available(row):
            return {"story": None, "shared": False, "open_elsewhere": True}
        state = dict(self.backend.state())
        state["story"] = {"id": row.id, "title": row.title, "chat": row.chat}
        state["shared"] = True
        return state

    # --- dispatch ------------------------------------------------------------

    def handle(self, request: Request) -> Response:
        if not host_matches(request, self._onion()):
            return error_response(421, "This server answers its onion address only.")
        if request.method not in ("GET", "POST"):
            return _with(error_response(405), Allow="GET, POST")
        if request.method == "GET" and not request.path.startswith("/api/"):
            static = self._static.get("/index.html" if request.path == "/" else request.path)
            if static is None:
                return error_response(404)
            body, content_type = static
            return Response(200, body, content_type)
        try:
            return self._dispatch(request)
        except HttpError as exc:
            return error_response(exc.status, exc.detail)
        except Refused as exc:
            return error_response(exc.status, exc.reason)

    def _dispatch(self, request: Request) -> Response:
        allowed: set[str] = set()
        for route in self._routes:
            match = route.pattern.fullmatch(request.path)
            if match is None:
                continue
            if route.method != request.method:
                allowed.add(route.method)
                continue
            device = ""
            if route.auth:
                device = self._authenticate(request)
                if not device:
                    return error_response(
                        401, "This phone isn't paired. Pair it again from the desktop."
                    )
                self.backend.seen(device)
            if route.story:
                refusal = self._story_refusal()
                if refusal is not None:
                    return refusal
            return route.handler(request, *match.groups(), device=device)
        if allowed:
            return _with(error_response(405), Allow=", ".join(sorted(allowed)))
        return error_response(404)

    def _authenticate(self, request: Request) -> str:
        now = self._clock()
        if now < self._locked_until:
            raise HttpError(429, "Too many wrong logins. Wait a minute.")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        device = (
            device_for(token.strip(), self.backend.devices())
            if scheme.lower() == "bearer"
            else None
        )
        if device:
            return device
        while self._failures and now - self._failures[0] > 60:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= AUTH_FAILURES:
            self._failures.clear()
            self._locked_until = now + LOCKOUT_SECONDS
            self.backend.warn(
                "Phone access: many wrong logins in a minute; refusing every phone for a minute."
            )
        return ""

    def _story_refusal(self) -> Response | None:
        row = self.backend.open_story()
        if row is None:
            return json_response(423, {"error": NO_STORY, "shared": False})
        if not self.available(row):
            return json_response(423, {"error": NOT_SHARED, "shared": False})
        return None

    # --- pairing -------------------------------------------------------------

    def _pair(self, request: Request, *, device: str) -> Response:
        body = _object(request)
        try:
            claim = self.pairing.claim(body.get("code"), body.get("name"))
        except PairingRefused as exc:
            return error_response(exc.status, exc.reason)
        self.backend.claimed(claim)
        return json_response(202, {"claim": claim.id, "check": claim.check})

    def _pair_poll(self, request: Request, claim_id: str, *, device: str) -> Response:
        state, token = self.pairing.poll(claim_id)
        answer: dict[str, str] = {"state": state}
        if token:
            answer["token"] = token
        return json_response(200, answer)

    # --- about the phone and the stories ------------------------------------

    def _state(self, request: Request, *, device: str) -> Response:
        return json_response(200, self.state_payload())

    def _stories(self, request: Request, *, device: str) -> Response:
        rows = [row for row in self.backend.story_rows() if self.available(row)]
        open_row = self.backend.open_story()
        open_id = open_row.id if open_row is not None and self.available(open_row) else None
        return json_response(
            200,
            {
                "stories": [
                    {
                        "id": r.id,
                        "title": r.title,
                        "messages": r.messages,
                        "chat": r.chat,
                        "open": r.id == open_id,
                    }
                    for r in rows
                ]
            },
        )

    def _open(self, request: Request, story_id: str, *, device: str) -> Response:
        row = next((r for r in self.backend.story_rows() if r.id == story_id), None)
        if row is None or not self.available(row):
            return error_response(404)
        self.backend.open(story_id)
        return json_response(200, self.state_payload())

    def _events(self, request: Request, *, device: str) -> Response:
        if not self.hub.can_subscribe(device):
            return error_response(429, "Too many open streams.")
        return Response(200, content_type="text/event-stream", stream=True, device=device)

    def _forget(self, request: Request, *, device: str) -> Response:
        self.backend.forget(device)
        self.hub.drop_device(device, "forgotten")
        return json_response(200, {"forgotten": True})

    # --- the open story ------------------------------------------------------

    def _transcript(self, request: Request, *, device: str) -> Response:
        before = request.query.get("before") or None
        if before is not None and not _ID.fullmatch(before):
            raise HttpError(400, "a bad message id")
        limit = _int(request.query.get("limit"), TRANSCRIPT_PAGE)
        limit = max(1, min(limit, TRANSCRIPT_MOST))
        return json_response(200, self.backend.transcript(before, limit))

    def _composer(self, request: Request, *, device: str) -> Response:
        return json_response(200, self.backend.composer())

    def _turn(self, request: Request, *, device: str) -> Response:
        body = _object(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HttpError(400, "Write something to send.")
        if len(text) > TEXT_LIMIT:
            raise HttpError(413, f"At most {TEXT_LIMIT} characters.")
        options = self.backend.composer()
        turn = PhoneTurn(
            text=text.strip(),
            speaker=_choice(body, "speaker", options, required=True) or "",
            length=_choice(body, "length", options),
            outcome=_choice(body, "outcome", options),
            skill=_choice(body, "skill", options),
            difficulty=_choice(body, "difficulty", options),
        )
        return json_response(202, self.backend.send(turn, device))

    def _stop(self, request: Request, *, device: str) -> Response:
        self.backend.stop()
        return json_response(200, {"stopping": True})

    def _regenerate(self, request: Request, *, device: str) -> Response:
        body = _object(request)
        options = self.backend.composer()
        retake = Retake(
            node_id=_node_id(body, required=False),
            length=_choice(body, "length", options),
            outcome=_choice(body, "outcome", options),
        )
        self.backend.regenerate(retake)
        return json_response(202, {"started": True})

    def _reroll(self, request: Request, *, device: str) -> Response:
        self.backend.reroll(_node_id(_object(request)) or "")
        return json_response(202, {"started": True})

    def _take(self, request: Request, *, device: str) -> Response:
        body = _object(request)
        step = body.get("step")
        if step not in (-1, 1) or isinstance(step, bool):
            raise HttpError(400, "step is -1 or 1")
        self.backend.switch_take(_node_id(body) or "", step)
        return json_response(200, {"switched": True})

    def _branches(self, request: Request, *, device: str) -> Response:
        return json_response(200, self.backend.branches())

    def _branch(self, request: Request, *, device: str) -> Response:
        start_id = _object(request).get("start_id")
        known = {b.get("start_id") for b in self.backend.branches().get("branches", [])}
        if not isinstance(start_id, str) or start_id not in known:
            raise HttpError(400, "no such branch")
        self.backend.switch_branch(start_id)
        return json_response(200, {"switched": True})

    def _picture(self, request: Request, image_id: str, *, device: str) -> Response:
        found = self.backend.picture(image_id, request.query.get("thumb") == "1")
        if found is None:
            return error_response(404)
        body, content_type = found
        return Response(200, body, content_type)


def _with(response: Response, **headers: str) -> Response:
    response.headers.update(headers)
    return response


def _object(request: Request) -> dict:
    body = request.json()
    if not isinstance(body, dict):
        raise HttpError(400, "send a JSON object")
    return body


def _int(value: str | None, default: int) -> int:
    if value is None or not value.isdigit():
        return default
    return int(value)


def _node_id(body: dict, *, required: bool = True) -> str | None:
    value = body.get("node_id")
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HttpError(400, "a bad message id")
    return value


_OPTION_LISTS = {
    "speaker": "speakers",
    "length": "lengths",
    "outcome": "outcomes",
    "skill": "skills",
    "difficulty": "difficulties",
}


def _choice(body: dict, field: str, options: dict, *, required: bool = False) -> str | None:
    """A value the composer offers now, or None for the story's own default.
    A value it doesn't offer is refused, not passed on."""
    value = body.get(field)
    if value is None:
        if required:
            raise HttpError(400, f"choose a {field}")
        return None
    offered = {o.get("value") for o in options.get(_OPTION_LISTS[field], []) if isinstance(o, dict)}
    if not isinstance(value, str) or value not in offered:
        raise HttpError(400, f"{field} {value!r} isn't offered")
    return value
