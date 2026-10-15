"""Map pictures from an image model (engine/world_pictures.py,
engine/session_world_art.py): what is asked for, in what order, and how a
picture is kept. No network: the image endpoint is a stand-in."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from sealedlore.engine import session_world_art
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world_pictures import (
    Person,
    isometric_size,
    looks_of,
    picture_size,
    plan_pictures,
)
from sealedlore.models.character import Character
from sealedlore.models.config import Config, MediaEndpoint, ProviderConfig
from sealedlore.models.node import NodeMeta
from sealedlore.models.world import (
    MapPicture,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldThing,
)
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import ImageModelInfo, ImageReply
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.conftest import make_exchange


def png(width: int = 8, height: int = 8) -> bytes:
    """A small white PNG with a red square in it, and a text chunk to strip."""
    rows = b""
    for y in range(height):
        rows += b"\0" + b"".join(
            b"\xb3\x38\x4a" if 2 <= x < 6 and 2 <= y < 6 else b"\xff\xff\xff" for x in range(width)
        )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"tEXt", b"Comment\0secret camera")
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def farm() -> WorldMap:
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    barn = WorldPlace(id="barn", name="The barn", kind="building", inside="farm", x=60.0, y=0.0)
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0)
    return WorldMap(
        places={"farm": farm, "barn": barn, "town": town},
        where={"Jane": "barn", "Ben": "barn", "Marcus Webb": "town"},
        visited={"Jane": ["farm", "barn"]},
        seen=["town"],
        things={"truck": WorldThing(id="truck", name="Pickup truck", at="barn")},
    )


PEOPLE = [Person("Marcus Webb", "Runs the town."), Person("Ben", "A hermit."), Person("Jane", "")]


def test_you_first_then_who_is_with_you_then_the_rest_then_places_and_things():
    jobs = plan_pictures(
        farm(), WorldArt(), people=PEOPLE, held="Jane", setting="snow", edits=True, limit=20
    )
    order = [(j.subject, j.subject_id) for j in jobs]
    assert order[:3] == [("person", "Jane"), ("person", "Ben"), ("person", "Marcus Webb")]
    assert ("place", "barn") in order and ("thing", "truck") in order
    assert order.index(("place", "barn")) < order.index(("place", "town"))  # nearest first
    assert order.index(("place", "town")) < order.index(("thing", "truck"))
    assert "Jane" in jobs[0].prompt and "white background" in jobs[0].prompt


def test_a_changed_place_is_drawn_from_its_last_picture_and_nothing_twice():
    world = farm()
    art = WorldArt(
        pictures=[
            MapPicture(subject="place", subject_id="barn", name="The barn", file="images/map/b.png")
        ]
    )
    jobs = plan_pictures(world, art, people=[], held="Jane", setting="", edits=True, limit=20)
    assert ("place", "barn") not in {(j.subject, j.subject_id) for j in jobs}  # drawn already
    world.places["barn"].state = "blown up"
    jobs = plan_pictures(world, art, people=[], held="Jane", setting="", edits=True, limit=20)
    (change,) = [j for j in jobs if (j.subject, j.subject_id) == ("place", "barn")]
    assert change.reference == "images/map/b.png" and "blown up" in change.prompt
    # A model that takes no picture draws it afresh.
    (fresh,) = [
        j
        for j in plan_pictures(
            world, art, people=[], held="Jane", setting="", edits=False, limit=20
        )
        if (j.subject, j.subject_id) == ("place", "barn")
    ]
    assert fresh.reference is None
    art.failures[change.key] = 2
    jobs = plan_pictures(world, art, people=[], held="Jane", setting="", edits=True, limit=20)
    assert ("place", "barn") not in {(j.subject, j.subject_id) for j in jobs}  # failed twice


def test_someone_the_story_has_not_placed_gets_no_avatar_yet():
    jobs = plan_pictures(
        farm(), WorldArt(), people=[Person("Elena Ruiz", "")], held="Jane", setting="", edits=True
    )
    assert "Elena Ruiz" not in {j.subject_id for j in jobs}


def test_sizes_and_looks():
    info = ImageModelInfo(id="m", name="m", resolutions=("256*256", "512*512", "1024*1024"))
    assert picture_size(info) == "512*512"
    assert picture_size(None) == "1024x1024"
    assert looks_of("Short.", None) == "Short."
    assert len(looks_of("A sentence here. " * 60, None)) <= 421


class FakeImages:
    """Stands in for ImageClient: records what it was sent."""

    sent: list[dict] = []

    def __init__(self, base_url, api_key, *, api=None, **_):
        self.base_url, self.api = base_url, api or "nanogpt"

    def generate(self, payload):
        FakeImages.sent.append(payload)
        return ImageReply(images=[png()], raw={}, cost=None)

    def cancel(self):
        pass

    def close(self):
        pass


@pytest.fixture
def session(tmp_path: Path, monkeypatch, story, cast):
    FakeImages.sent = []
    monkeypatch.setattr(session_world_art, "ImageClient", FakeImages)
    monkeypatch.setattr(session_world_art, "list_image_models", lambda *a, **k: [])
    nodes = make_exchange(1)
    nodes[-1].meta = NodeMeta(
        world=WorldDelta(places=list(farm().places.values()), where={"Jane": "barn"})
    )
    story.defaults.world_map = True
    jane = Character(name="Jane", summary="A survivor in a heavy coat.")
    bundle = StoryBundle(story=story, cast=[*cast, jane], nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    bundle.story.held_character_id = jane.id
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[
            ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", api_key="k")
        ],
        active_provider_name="nano",
        scene_reads="manual",
        map_art=True,
        map_pictures=True,
        map_picture_daily_limit=2,
        image_endpoint=MediaEndpoint(),
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(["x"] * 20),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def run_pictures(session) -> None:
    assert session.start_map_pictures()
    session._picture_thread.join(10)


def test_pictures_are_drawn_kept_stripped_and_logged_within_the_daily_limit(session, tmp_path):
    run_pictures(session)
    art = session.map_art_copy()
    assert len(art.pictures) == 2 and len(FakeImages.sent) == 2  # the limit, not every job
    first = art.pictures[0]
    assert (first.subject, first.subject_id) == ("person", "Jane")
    assert "heavy coat" in first.prompt
    data = session.pictures.read(first.file)
    assert data is not None and b"secret camera" not in data  # stripped like any picture
    assert session.map_pictures_left_today() == 0
    assert not session.start_map_pictures()  # nothing more today
    log_file = tmp_path / "stories" / session.story.id / "api_log.jsonl"
    log = [json.loads(line) for line in log_file.read_text().splitlines()]
    kinds = [entry["kind"] for entry in log]
    assert kinds.count("map_picture_request") == 2 and kinds.count("map_picture_response") == 2
    assert any(first.prompt in json.dumps(e) for e in log if e["kind"] == "map_picture_request")


def test_off_until_turned_on(session):
    session.config.map_pictures = False
    assert session.map_picture_jobs() == [] and not session.start_map_pictures()


def test_each_place_you_have_been_gets_a_postcard_where_you_are_first():
    jobs = plan_pictures(
        farm(), WorldArt(), people=PEOPLE, held="Jane", setting="snow", edits=True, limit=30
    )
    order = [(j.subject, j.subject_id) for j in jobs]
    cards = [o for o in order if o[0] == "postcard"]
    assert set(cards) == {("postcard", "farm"), ("postcard", "barn")}  # not the town: never been
    # Where the story is now (the barn, and the farm it is in) right after the people.
    assert order[3:5] == [("postcard", "barn"), ("postcard", "farm")]
    barn = next(j for j in jobs if j.subject == "postcard" and j.subject_id == "barn")
    assert "full-bleed" in barn.prompt and "white background" not in barn.prompt
    assert "postcard" not in barn.prompt  # the word drew a card with a frame and lettering
    info = ImageModelInfo(id="m", name="m", resolutions=("1024x1024", "1360x768", "1024x768"))
    assert picture_size(info, landscape=True) == "1360x768"


def test_the_isometric_view_is_painted_nearest_the_shape_of_its_plan():
    """The map lays the view over the plan it was painted from (5:3): the
    listed size nearest that shape, not the widest (Qwen's 4:3 lost a tenth
    at the top and the bottom)."""
    qwen = ImageModelInfo(
        id="q", name="q", resolutions=("auto", "1024x1024", "768x1024", "1024x768", "1024x576")
    )
    assert isometric_size(qwen) == "1024x576"
    klein = ImageModelInfo(
        id="k", name="k", resolutions=("1024*1024", "1280*720", "1536*1024", "1536*1536")
    )
    assert isometric_size(klein) == "1280*720"
    # Sizes it doesn't read ("1k") or no listing: as a postcard.
    assert isometric_size(ImageModelInfo(id="n", name="n", resolutions=("1k", "2k"))) == "1k"
    assert isometric_size(None) == "1024x768"


def test_the_isometric_view_is_painted_only_when_asked_from_the_plan(session, tmp_path):
    """Map → Isometric: the author asks (no standing approval needed), the
    plan's own picture goes with the prompt, it counts against the day's map
    pictures, and is kept for the map as it is now."""
    from sealedlore.engine.world_pictures import isometric_state

    session.config.map_pictures = False  # the press is the approval
    session.config.image_models = [
        {
            "id": "qwen-image",
            "supported_parameters": {
                "resolutions": ["1024x768", "1024x576"],
                "max_input_images": 3,
            },
        }
    ]
    model, _price, problem = session.isometric_offer()
    assert model == "qwen-image" and problem is None
    assert session.map_picture_jobs() == []  # nothing painted on its own
    assert session.start_isometric("farm", lambda: png(16, 12))
    session._isometric_thread.join(10)
    art = session.map_art_copy()
    view = art.picture_for("isometric", "farm")
    assert view is not None and view.state == isometric_state(session.world(), "farm")
    payload = FakeImages.sent[-1]
    assert payload["model"] == "qwen-image"
    assert payload["size"] == "1024x576"  # the plan's shape, near enough
    assert "Redraw this game map as an isometric view" in payload["prompt"]
    assert "data:image/png;base64" in json.dumps(payload)  # the plan's picture went with it
    log_file = tmp_path / "stories" / session.story.id / "api_log.jsonl"
    requests = [
        json.loads(line)
        for line in log_file.read_text().splitlines()
        if '"map_picture_request"' in line
    ]
    assert "the map of The farm" in json.dumps(requests[-1])  # logged by name, not base64
    assert session.map_pictures_left_today() == 1  # counted like any map picture
    # Without a plan to paint from, nothing is sent.
    sent = len(FakeImages.sent)
    assert session.start_isometric("farm", lambda: None)
    session._isometric_thread.join(10)
    assert len(FakeImages.sent) == sent


def test_the_isometric_view_is_painted_from_what_a_picture_shows_not_the_map_s_words(
    session, tmp_path
):
    """The places' own text carries names and story, and Qwen Image lettered
    them onto the view (writing in 11 of 12, none once the prompt writer had
    turned them into what can be seen). The writer gets the map's words; the
    painter gets the writer's scene. If the writer fails, the map's words go."""
    session.provider.responses = ["  A snowbound yard   with a red barn.  "]
    assert session.start_isometric("farm", lambda: png(16, 12))
    session._isometric_thread.join(10)
    written = session.provider.payloads[-1]
    system, notes = (m["content"] for m in written["messages"])
    assert "describe only what can be seen" in json.dumps(system)
    assert "The farm" in json.dumps(notes) and "The barn" in json.dumps(notes)
    prompt = FakeImages.sent[-1]["prompt"]
    assert "What is on it: A snowbound yard with a red barn." in prompt
    assert "The barn" not in prompt  # the names stayed with the writer
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "stories" / session.story.id / "api_log.jsonl")
        .read_text()
        .splitlines()
    ]
    assert kinds.count("isometric_prompt_request") == 1
    assert kinds.count("isometric_prompt_response") == 1

    session.provider.error = ProviderError("down")
    assert session.start_isometric("farm", lambda: png(16, 12))
    session._isometric_thread.join(10)
    assert "What is on it: The farm" in FakeImages.sent[-1]["prompt"]


def test_a_scene_that_quotes_words_is_asked_for_again_and_never_lettered(session, tmp_path):
    """The writer once wrote 'a bulkhead plaque lettered "PALE MARTYR"' and
    the painter lettered it on: quotation marks get the scene asked for once
    more, and what is still quoted the second time is left out."""
    session.provider.responses = [
        'A wreck with a plaque lettered "PALE MARTYR" on its hull.',
        "A wreck half buried in ash.",
    ]
    assert session.start_isometric("farm", lambda: png(16, 12))
    session._isometric_thread.join(10)
    prompt = FakeImages.sent[-1]["prompt"]
    assert "What is on it: A wreck half buried in ash." in prompt
    session.provider.responses = [
        'A plaque lettered "PALE MARTYR".',
        "A plaque lettered “PALE MARTYR” on a hull.",
    ]
    assert session.start_isometric("farm", lambda: png(16, 12))
    session._isometric_thread.join(10)
    prompt = FakeImages.sent[-1]["prompt"]
    assert "What is on it: A plaque lettered on a hull." in prompt
    assert "MARTYR" not in prompt
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "stories" / session.story.id / "api_log.jsonl")
        .read_text()
        .splitlines()
    ]
    assert kinds.count("isometric_prompt_request") == 4  # each asked twice, each logged


def test_the_isometric_view_names_a_change_first_and_goes_out_of_date_with_it():
    from sealedlore.engine.world_pictures import isometric_job, isometric_state

    world = farm()
    before = isometric_state(world, "farm")
    world.places["barn"] = world.places["barn"].model_copy(update={"state": "burning"})
    job = isometric_job(world, WorldArt(), "farm", look="snow")
    assert isometric_state(world, "farm") != before and job.state == "The barn: burning"
    assert "As the story has it now, The barn is burning: draw it so" in job.prompt
    assert job.prompt.index("burning: draw it so") < job.prompt.index("What is on it")
    assert "Hellsville" not in job.prompt  # only what the plan has


def test_the_isometric_view_says_a_change_in_what_its_plan_shows():
    """Live, the barn's change was the story's words ("past saving, a
    furnace now"), which never say fire, beside its old description ("its
    roof still holding"): Qwen drew it intact every time. Now the change is
    what the redrawn plan shows, and the old description is gone."""
    from sealedlore.engine.world_pictures import isometric_job, seen_change
    from sealedlore.models.world import PlaceLayout

    world = farm()
    world.places["barn"] = world.places["barn"].model_copy(
        update={"state": "past saving, a furnace now", "what": "Its roof still holding."}
    )
    art = WorldArt(
        layouts={
            "barn": {
                "": PlaceLayout(place_id="barn", rows=["#####", "#___#", "#####"]),
                "past saving, a furnace now": PlaceLayout(
                    place_id="barn", rows=[":::::", ":**::", "#:::#"]
                ),
            }
        }
    )
    assert seen_change(art, world.places["barn"]) == "on fire, in ruins, most of its walls down"
    prompt = isometric_job(world, art, "farm", look="snow").prompt
    assert "The barn is on fire, in ruins, most of its walls down (the story: past saving" in prompt
    assert "roof still holding" not in prompt
    assert "The ground: snow-covered country in winter." in prompt


def test_a_plan_redrawn_when_asked_goes_with_its_failures_and_the_art_runs_again(
    session, monkeypatch
):
    """Redraw plan…: a plan drawn badly stayed so, since plans are kept."""
    from sealedlore.models.world import PlaceLayout, WorldArt

    session._art = WorldArt(
        layouts={
            "farm": {
                "": PlaceLayout(place_id="farm", rows=["#####", "#___#", "#####"]),
                "burning": PlaceLayout(place_id="farm", rows=["*****"], state="burning"),
            },
            "barn": {"": PlaceLayout(place_id="barn", rows=["###"])},
        },
        failures={"layout:farm": 3, "layout:farm:burning": 1, "layout:farmhouse": 2},
    )
    ran: list[bool] = []
    monkeypatch.setattr(session, "start_map_art", lambda: ran.append(True) or True)
    assert session.redraw_plan("farm")
    art = session.map_art_copy()
    assert "farm" not in art.layouts and "barn" in art.layouts  # the inside plan stays
    assert art.failures == {"layout:farmhouse": 2}
    assert ran == [True]
    assert not session.redraw_plan("farm")  # nothing left to redraw
    session.enter_private(keep="memory", provider=MockChatProvider(["x"]), model="m")
    assert not session.redraw_plan("barn")  # paused in a private scene
