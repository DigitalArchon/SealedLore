"""The phone's API (phone/api.py) and its event stream (phone/sse.py),
against a stand-in for the window: who may ask, about which story, and what
they may send."""

from __future__ import annotations

import json

import pytest

from sealedlore.phone import api as phone_api
from sealedlore.phone.api import Api, PhoneTurn, Refused, Retake, StoryRow
from sealedlore.phone.http import SECURITY_HEADERS, Request, RequestParser, Response, render
from sealedlore.phone.pairing import Pairing, token_hash
from sealedlore.phone.sse import MAX_STREAMS_PER_DEVICE, EventHub, frame

ONION = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz2345.onion"
TOKEN = "good-token"
SECRET = "ZQX-NOT-FOR-PHONES"


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


class FakeBackend:
    def __init__(self) -> None:
        self.rows = [
            StoryRow("shared", "Doomsville", 12, chat=False, keeps_map=False),
            StoryRow("private", f"Title {SECRET}", 3, chat=False, keeps_map=False),
            StoryRow("mapped", "Doomsville with a map", 5, chat=False, keeps_map=True),
            StoryRow("chat", "A chat", 2, chat=True, keeps_map=False),
        ]
        self.open_id: str | None = "shared"
        self.shared = {"shared", "mapped", "chat"}
        self.device_list = [("phone-1", token_hash(TOKEN))]
        self.calls: list[tuple] = []
        self.refuse: Refused | None = None
        self.warnings: list[str] = []

    def _row(self):
        return next((r for r in self.rows if r.id == self.open_id), None)

    def _secret(self) -> str:
        return SECRET if self.open_id == "private" else "story text"

    def open_story(self):
        return self._row()

    def story_rows(self):
        return list(self.rows)

    def shared_ids(self):
        return self.shared

    def devices(self):
        return self.device_list

    def state(self):
        return {"busy": False, "status": f"Model {self._secret()}"}

    def transcript(self, before, limit):
        self.calls.append(("transcript", before, limit))
        return {"messages": [{"id": "n1", "text": self._secret()}]}

    def composer(self):
        return {
            "speakers": [
                {"value": "narration", "label": "Narration"},
                {"value": "c1", "label": "John"},
                {"value": "question", "label": "Question"},
            ],
            "lengths": [{"value": "brief", "label": "Brief"}],
            "outcomes": [{"value": "fiat", "label": "Fiat"}],
            "skills": [],
            "difficulties": [{"value": "hard", "label": "Hard"}],
            "secret": self._secret(),
        }

    def branches(self):
        return {"branches": [{"start_id": "", "name": "Main"}, {"start_id": "n5", "name": "Two"}]}

    def picture(self, image_id, thumb):
        self.calls.append(("picture", image_id, thumb))
        return (b"\xff\xd8jpeg", "image/jpeg") if image_id == "img1" else None

    def _act(self, *call):
        if self.refuse is not None:
            raise self.refuse
        self.calls.append(call)

    def open(self, story_id):
        self._act("open", story_id)
        self.open_id = story_id

    def send(self, turn, device):
        self._act("send", turn, device)
        return {"queued": False}

    def stop(self):
        self._act("stop")

    def regenerate(self, retake):
        self._act("regenerate", retake)

    def reroll(self, node_id):
        self._act("reroll", node_id)

    def switch_take(self, node_id, step):
        self._act("take", node_id, step)

    def switch_branch(self, start_id):
        self._act("branch", start_id)

    def claimed(self, claim):
        self.calls.append(("claimed", claim.id, claim.check))

    def seen(self, device):
        pass

    def forget(self, device):
        self.calls.append(("forget", device))

    def warn(self, message):
        self.warnings.append(message)


def make(backend: FakeBackend | None = None, clock: Clock | None = None):
    backend = backend or FakeBackend()
    clock = clock or Clock()
    api = Api(
        backend,
        pairing=Pairing(clock),
        onion=lambda: ONION,
        static={
            "/index.html": (b"<!doctype html>", "text/html"),
            "/app.js": (b"1", "application/javascript"),
        },
        clock=clock,
    )
    return api, backend


def request(
    method: str,
    path: str,
    body: object | None = None,
    *,
    token: str | None = TOKEN,
    host: str = ONION,
    content_type: str = "application/json",
) -> Request:
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    data = b""
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        lines += [f"Content-Type: {content_type}", f"Content-Length: {len(data)}"]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + data
    (parsed,) = RequestParser().feed(raw)
    return parsed


def answer(api: Api, *args, **kwargs) -> tuple[int, dict]:
    response = api.handle(request(*args, **kwargs))
    if response.content_type != "application/json" or not response.body:
        return response.status, {}
    return response.status, json.loads(response.body)


AUTH_ROUTES = [
    ("GET", "/api/state", None),
    ("GET", "/api/stories", None),
    ("POST", "/api/stories/shared/open", {}),
    ("GET", "/api/events", None),
    ("POST", "/api/forget", {}),
    ("GET", "/api/transcript", None),
    ("GET", "/api/composer", None),
    ("POST", "/api/turn", {"text": "Hi", "speaker": "c1"}),
    ("POST", "/api/stop", {}),
    ("POST", "/api/regenerate", {}),
    ("POST", "/api/reroll", {"node_id": "n1"}),
    ("POST", "/api/take", {"node_id": "n1", "step": 1}),
    ("GET", "/api/branches", None),
    ("POST", "/api/branch", {"start_id": "n5"}),
    ("GET", "/api/pictures/img1", None),
]
STORY_ROUTES = [
    r
    for r in AUTH_ROUTES
    if r[1] not in ("/api/state", "/api/stories", "/api/events", "/api/forget")
]
STORY_ROUTES = [r for r in STORY_ROUTES if "/stories/" not in r[1]]


# --- who may ask ---------------------------------------------------------------


def test_another_host_is_answered_with_nothing():
    api, _ = make()
    for method, path, body in AUTH_ROUTES + [("GET", "/", None), ("POST", "/api/pair", {})]:
        status, _ = answer(api, method, path, body, host="localhost:8080")
        assert status == 421, path


def test_no_address_yet_answers_nothing():
    backend = FakeBackend()
    api = Api(backend, pairing=Pairing(), onion=lambda: "")
    assert api.handle(request("GET", "/api/state")).status == 421


@pytest.mark.parametrize("method, path, body", AUTH_ROUTES)
def test_every_api_route_needs_a_paired_phone(method, path, body):
    api, _ = make()
    assert answer(api, method, path, body, token=None)[0] == 401
    assert answer(api, method, path, body, token="wrong")[0] == 401
    assert answer(api, method, path, body)[0] in (200, 202)


def test_wrong_logins_lock_every_phone_out_for_a_minute():
    clock = Clock()
    api, backend = make(clock=clock)
    for _ in range(phone_api.AUTH_FAILURES):
        assert answer(api, "GET", "/api/state", token="wrong")[0] == 401
    assert answer(api, "GET", "/api/state")[0] == 429
    assert backend.warnings
    clock.now += phone_api.LOCKOUT_SECONDS + 1
    assert answer(api, "GET", "/api/state")[0] == 200


def test_the_page_itself_needs_no_login_and_carries_the_headers():
    api, _ = make()
    response = api.handle(request("GET", "/", token=None))
    assert (response.status, response.body) == (200, b"<!doctype html>")
    assert api.handle(request("GET", "/app.js", token=None)).status == 200
    assert api.handle(request("GET", "/../config.json", token=None)).status == 404
    raw = render(response, keep_alive=True).decode()
    assert all(f"{name}: {value}" in raw for name, value in SECURITY_HEADERS.items())


def test_a_method_the_route_doesnt_take():
    api, _ = make()
    response = api.handle(request("GET", "/api/turn"))
    assert response.status == 405 and response.headers["Allow"] == "POST"
    assert api.handle(request("PUT", "/api/turn")).status == 405
    assert api.handle(request("GET", "/api/nothing")).status == 404


# --- which story ----------------------------------------------------------------


def test_only_stories_shared_and_without_a_map_are_listed():
    api, _ = make()
    status, body = answer(api, "GET", "/api/stories")
    assert status == 200
    assert [(s["id"], s["open"]) for s in body["stories"]] == [("shared", True), ("chat", False)]


@pytest.mark.parametrize("story_id", ["private", "mapped", "nonexistent"])
def test_a_story_not_available_cant_be_opened_and_looks_like_no_story(story_id):
    api, backend = make()
    assert answer(api, "POST", f"/api/stories/{story_id}/open", {}) == (404, {"error": "Not Found"})
    assert backend.open_id == "shared"


def test_a_shared_story_opens():
    api, backend = make()
    status, body = answer(api, "POST", "/api/stories/chat/open", {})
    assert status == 200 and body["story"]["title"] == "A chat"
    assert backend.open_id == "chat"


@pytest.mark.parametrize("open_id", ["private", "mapped"])
@pytest.mark.parametrize("method, path, body", STORY_ROUTES)
def test_nothing_about_a_story_not_shared_reaches_a_phone(open_id, method, path, body):
    api, backend = make()
    backend.open_id = open_id
    status, answered = answer(api, method, path, body)
    assert status == 423
    assert answered == {"error": phone_api.NOT_SHARED, "shared": False}
    assert not [c for c in backend.calls if c[0] != "claimed"]


def test_the_state_of_a_story_not_shared_has_no_title_and_nothing_from_it():
    api, backend = make()
    backend.open_id = "private"
    response = api.handle(request("GET", "/api/state"))
    assert json.loads(response.body) == {"story": None, "shared": False, "open_elsewhere": True}
    for method, path, body in AUTH_ROUTES:
        assert SECRET.encode() not in api.handle(request(method, path, body)).body, path
    backend.open_id = None
    assert answer(api, "GET", "/api/transcript") == (
        423,
        {"error": phone_api.NO_STORY, "shared": False},
    )
    assert answer(api, "GET", "/api/state") == (200, {"story": None, "shared": False})


def test_the_state_of_a_shared_story():
    api, _ = make()
    status, body = answer(api, "GET", "/api/state")
    assert status == 200
    assert body["shared"] is True and body["busy"] is False
    assert body["story"] == {"id": "shared", "title": "Doomsville", "chat": False}


# --- what may be sent -------------------------------------------------------------


def test_a_turn_with_what_the_composer_offers():
    api, backend = make()
    status, body = answer(
        api,
        "POST",
        "/api/turn",
        {"text": "  I open the door.  ", "speaker": "c1", "length": "brief", "difficulty": "hard"},
    )
    assert (status, body) == (202, {"queued": False})
    assert backend.calls[-1] == (
        "send",
        PhoneTurn("I open the door.", "c1", length="brief", difficulty="hard"),
        "phone-1",
    )


@pytest.mark.parametrize(
    "body, status",
    [
        ({"speaker": "c1"}, 400),
        ({"text": "   ", "speaker": "c1"}, 400),
        ({"text": "x" * (phone_api.TEXT_LIMIT + 1), "speaker": "c1"}, 413),
        ({"text": "Hi"}, 400),
        ({"text": "Hi", "speaker": "someone-else"}, 400),
        ({"text": "Hi", "speaker": "c1", "length": "endless"}, 400),
        ({"text": "Hi", "speaker": "c1", "outcome": 3}, 400),
        ({"text": "Hi", "speaker": "c1", "skill": "lockpicking"}, 400),
        (["a list"], 400),
    ],
)
def test_a_turn_with_anything_the_composer_doesnt_offer_is_refused(body, status):
    api, backend = make()
    assert answer(api, "POST", "/api/turn", body)[0] == status
    assert not [c for c in backend.calls if c[0] == "send"]


def test_a_body_that_isnt_json_is_refused():
    api, _ = make()
    assert answer(api, "POST", "/api/turn", b"text=hi", content_type="text/plain")[0] == 415
    assert answer(api, "POST", "/api/turn", b"{not json")[0] == 400


def test_the_desktop_s_refusal_reaches_the_phone():
    api, backend = make()
    backend.refuse = Refused(409, "The desktop is waiting for an answer.")
    assert answer(api, "POST", "/api/turn", {"text": "Hi", "speaker": "c1"}) == (
        409,
        {"error": "The desktop is waiting for an answer."},
    )


def test_retakes_takes_and_branches_are_checked():
    api, backend = make()
    assert answer(api, "POST", "/api/regenerate", {"node_id": "n1", "length": "brief"})[0] == 202
    assert backend.calls[-1] == ("regenerate", Retake("n1", length="brief"))
    assert answer(api, "POST", "/api/regenerate", {"node_id": "../x"})[0] == 400
    assert answer(api, "POST", "/api/take", {"node_id": "n1", "step": 2})[0] == 400
    assert answer(api, "POST", "/api/take", {"node_id": "n1", "step": True})[0] == 400
    assert answer(api, "POST", "/api/take", {"node_id": "n1", "step": -1})[0] == 200
    assert backend.calls[-1] == ("take", "n1", -1)
    assert answer(api, "POST", "/api/branch", {"start_id": "n9"})[0] == 400
    assert answer(api, "POST", "/api/branch", {"start_id": ""})[0] == 200
    assert backend.calls[-1] == ("branch", "")


def test_transcript_pages_are_bounded():
    api, backend = make()
    answer(api, "GET", "/api/transcript?before=n9&limit=100000")
    assert backend.calls[-1] == ("transcript", "n9", phone_api.TRANSCRIPT_MOST)
    answer(api, "GET", "/api/transcript")
    assert backend.calls[-1] == ("transcript", None, phone_api.TRANSCRIPT_PAGE)
    assert answer(api, "GET", "/api/transcript?before=%2E%2E%2Fx")[0] == 400


def test_a_picture_comes_as_its_bytes():
    api, backend = make()
    response = api.handle(request("GET", "/api/pictures/img1?thumb=1"))
    assert (response.status, response.body, response.content_type) == (
        200,
        b"\xff\xd8jpeg",
        "image/jpeg",
    )
    assert backend.calls[-1] == ("picture", "img1", True)
    assert api.handle(request("GET", "/api/pictures/other")).status == 404


# --- pairing through the API ----------------------------------------------------


def test_pairing_from_the_phone_s_side():
    api, backend = make()
    assert answer(api, "POST", "/api/pair", {"code": "x", "name": "P"}, token=None)[0] == 410
    code = api.pairing.open()
    status, body = answer(api, "POST", "/api/pair", {"code": code, "name": "Pixel"}, token=None)
    assert status == 202 and len(body["check"]) == 4
    assert backend.calls[-1] == ("claimed", body["claim"], body["check"])
    assert answer(api, "GET", f"/api/pair/{body['claim']}", token=None) == (
        200,
        {"state": "waiting"},
    )
    token = api.pairing.answer(body["claim"], allow=True)
    backend.device_list.append(("phone-2", token_hash(token)))
    assert answer(api, "GET", f"/api/pair/{body['claim']}", token=None) == (
        200,
        {"state": "approved", "token": token},
    )
    assert answer(api, "GET", "/api/state", token=token)[0] == 200


# --- the event stream -----------------------------------------------------------


def test_an_event_stream_is_for_its_phone_and_limited_per_phone():
    api, _ = make()
    response = api.handle(request("GET", "/api/events"))
    assert isinstance(response, Response)
    assert (response.status, response.stream, response.device) == (200, True, "phone-1")
    for _ in range(MAX_STREAMS_PER_DEVICE):
        api.hub.subscribe("phone-1", lambda data: True, lambda: None)
    assert api.handle(request("GET", "/api/events")).status == 429


def test_forgetting_a_phone_closes_its_streams():
    api, backend = make()
    written: list[bytes] = []
    closed: list[bool] = []
    api.hub.subscribe(
        "phone-1", lambda data: written.append(data) or True, lambda: closed.append(True)
    )
    assert answer(api, "POST", "/api/forget", {}) == (200, {"forgotten": True})
    assert backend.calls[-1] == ("forget", "phone-1")
    assert written == [frame("forgotten", {})] and closed == [True]


def test_story_events_go_only_while_the_open_story_is_shared():
    api, backend = make()
    written: list[bytes] = []
    api.hub.subscribe("phone-1", lambda data: written.append(data) or True, lambda: None)
    api.hub.publish("text", {"text": "shared words"})
    backend.open_id = "private"
    api.hub.publish("text", {"text": SECRET})
    api.hub.publish("state", api.state_payload(), content=False)
    assert written[0] == frame("text", {"text": "shared words"})
    assert len(written) == 2 and SECRET.encode() not in written[1]


def test_a_stream_that_falls_behind_is_let_go_and_closed():
    hub = EventHub(lambda: True)
    closed: list[str] = []
    hub.subscribe("a", lambda data: False, lambda: closed.append("a"))
    hub.subscribe("b", lambda data: True, lambda: closed.append("b"))
    hub.publish("text", {"text": "x"})
    hub.heartbeat()
    assert closed == ["a"] and hub.count() == 1


def test_an_event_is_one_json_line_whatever_the_text():
    data = frame("text", {"text": "line one\n\ndata: fake\nevent: revoked"})
    assert data.count(b"\n") == 3 and data.endswith(b"\n\n")
    assert data.startswith(b"event: text\ndata: {")
    with pytest.raises(ValueError):
        frame("text\nevent: revoked", {})
