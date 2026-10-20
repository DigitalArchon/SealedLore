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
        map_pictures_mode="auto",
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
    # Played on the map, everyone's avatar is the tile run's walker: no
    # picture of each person besides (Oct 18 2026, paid twice).
    assert all(p.subject != "person" for p in art.pictures)
    assert (first.subject, first.subject_id) == ("postcard", "barn")
    data = session.pictures.read(first.file)
    assert data is not None and b"secret camera" not in data  # stripped like any picture
    assert session.map_pictures_left_today() == 0
    assert not session.start_map_pictures()  # nothing more today
    log_file = tmp_path / "stories" / session.story.id / "api_log.jsonl"
    log = [json.loads(line) for line in log_file.read_text().splitlines()]
    kinds = [entry["kind"] for entry in log]
    assert kinds.count("map_picture_request") == 2 and kinds.count("map_picture_response") == 2
    assert any(first.prompt in json.dumps(e) for e in log if e["kind"] == "map_picture_request")


def test_on_by_default_and_none_when_turned_off(session):
    from sealedlore.models.config import Config

    assert Config().map_pictures_mode == "auto"  # the author, Oct 17 2026
    session.config.map_pictures_mode = "off"
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

    session.config.map_pictures_mode = "off"  # the press is the approval
    session.config.map_isometric_model = "qwen-image"
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
    assert session.start_isometric("farm", lambda size: png(16, 12))
    session._isometric_thread.join(10)
    art = session.map_art_copy()
    view = art.picture_for("isometric", "farm")
    assert view is not None and view.state == isometric_state(session.world(), "farm")
    payload = FakeImages.sent[-1]
    assert payload["model"] == "qwen-image"
    assert payload["size"] == "1024x576"  # the plan's shape, near enough
    assert "Keep the layout exactly as the picture has it" in payload["prompt"]
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
    assert session.start_isometric("farm", lambda size: None)
    session._isometric_thread.join(10)
    assert len(FakeImages.sent) == sent


def test_the_isometric_view_is_painted_from_the_program_s_render_with_one_line(session, tmp_path):
    """Measured (design/map.md, Oct 15 2026): sent the program's isometric
    render instead of the top-down plan, every model kept the layout, and
    one line of words beat the prompt writer's paragraph. So: the render at
    the size the model paints, the area's first sentence, no writer call."""
    from sealedlore.engine.world_pictures import one_line

    session.config.map_pictures_mode = "off"
    session.config.map_isometric_model = "qwen-image"
    session.config.image_models = [
        {
            "id": "qwen-image",
            "supported_parameters": {
                "resolutions": ["1024x768", "1024x576"],
                "max_input_images": 3,
            },
        }
    ]
    sizes: list[tuple[int, int]] = []

    def render(size: tuple[int, int]) -> bytes:
        sizes.append(size)
        return png(size[0] // 64, size[1] // 64)

    assert session.start_isometric("farm", render)
    session._isometric_thread.join(10)
    assert sizes == [(1024, 576)]  # the render at the size painted: one shape in and out
    prompt = FakeImages.sent[-1]["prompt"]
    assert prompt.startswith("This picture is the game map of a place as it stands right now")
    farm = session.world().places["farm"]
    assert f"The place: {one_line(farm)}" in prompt
    assert one_line(farm) == "The farm."  # the fixture's farm has no description
    assert "The barn" not in prompt  # no paragraph of the places' words
    assert session.provider.requests == []  # and no prompt writer call
    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "stories" / session.story.id / "api_log.jsonl")
        .read_text()
        .splitlines()
    ]
    assert "isometric_prompt_request" not in kinds


def test_one_line_is_the_first_sentence_or_the_name():
    from sealedlore.engine.world_pictures import one_line

    assert one_line(WorldPlace(name="The yard", what="Mud and  straw.\nA dog.")) == "Mud and straw."
    assert one_line(WorldPlace(name="The yard", what="Mud and straw")) == "Mud and straw."
    assert one_line(WorldPlace(name="The yard", what="")) == "The yard."


def test_the_view_shown_is_the_one_chosen_for_the_map_as_it_is_now():
    """An area may have several views (Paint it again, pictures of one's
    own), and the author chooses which lies over the map (the author, Oct
    15 2026). The choice is for the map's state there: back on a branch
    where the barn stands, the view chosen for that stands, not the one
    chosen for it burning; a choice naming a picture that is gone falls
    back to the latest that matches."""
    from sealedlore.engine.world_pictures import chosen_view, isometric_state, shown_view, views_of

    world = farm()
    standing = isometric_state(world, "farm")
    first = MapPicture(subject="isometric", subject_id="farm", name="F", state=standing, file="a")
    second = MapPicture(subject="isometric", subject_id="farm", name="F", state=standing, file="b")
    art = WorldArt(pictures=[first, second])
    assert views_of(art, "farm") == [first, second]
    assert shown_view(world, art, "farm") is second  # the latest, nothing chosen
    art.chosen_views = {"farm": {standing: first.id}}
    assert chosen_view(world, art, "farm") is first and shown_view(world, art, "farm") is first
    # The barn burns: nothing chosen for that state, so the latest that matches.
    burning = world.model_copy(deep=True)
    burning.places["barn"] = burning.places["barn"].model_copy(update={"state": "burning"})
    fire = MapPicture(
        subject="isometric",
        subject_id="farm",
        name="F",
        state=isometric_state(burning, "farm"),
        file="c",
    )
    art.pictures.append(fire)
    assert chosen_view(burning, art, "farm") is None and shown_view(burning, art, "farm") is fire
    assert shown_view(world, art, "farm") is first  # the branch where it stands keeps its choice
    # A choice of a picture that is gone: as if none.
    art.chosen_views = {"farm": {standing: "gone"}}
    assert shown_view(world, art, "farm") is second


def test_painting_a_view_or_bringing_one_in_chooses_it_and_the_author_may_choose_another(
    session, tmp_path
):
    """The view just painted is the one over the map; so is a picture of
    one's own; Choose a view… (or Media) puts another there, for the map as
    it is now. Never a picture that isn't one of the area's views."""
    from sealedlore.engine.world_pictures import isometric_state, shown_view

    session.config.map_pictures_mode = "off"
    session.config.map_picture_daily_limit = 5
    state = isometric_state(session.world(), "farm")
    assert session.start_isometric("farm", lambda size: png(16, 12))
    session._isometric_thread.join(10)
    art = session.map_art_copy()
    painted = art.picture_for("isometric", "farm")
    assert painted is not None and art.chosen_views == {"farm": {state: painted.id}}
    assert session.use_own_isometric("farm", png(20, 12), ".png")
    art = session.map_art_copy()
    own = art.picture_for("isometric", "farm")
    assert own is not None and own.id != painted.id and own.model == ""
    assert art.chosen_views["farm"][state] == own.id
    assert shown_view(session.world(), art, "farm") is own
    # Back to the painted one.
    assert session.choose_isometric("farm", painted.id)
    art = session.map_art_copy()
    assert art.chosen_views["farm"][state] == painted.id
    assert shown_view(session.world(), art, "farm").id == painted.id
    # Not a picture of another area, not a picture that isn't a view, not a
    # place that isn't on the map; the choice stands.
    assert not session.choose_isometric("town", painted.id)
    assert not session.choose_isometric("farm", "nothing")
    assert not session.choose_isometric("nowhere", painted.id)
    assert session.map_art_copy().chosen_views["farm"][state] == painted.id
    # Kept with the art on disk.
    from sealedlore.storage.world_art import load_world_art

    assert load_world_art(session.story.id, tmp_path).chosen_views["farm"][state] == painted.id


def test_the_isometric_view_names_a_change_first_and_goes_out_of_date_with_it():
    from sealedlore.engine.world_pictures import isometric_job, isometric_state

    world = farm()
    before = isometric_state(world, "farm")
    world.places["barn"] = world.places["barn"].model_copy(update={"state": "burning"})
    job = isometric_job(world, WorldArt(), "farm", look="snow")
    assert isometric_state(world, "farm") != before and job.state == "The barn: burning"
    assert "As the story has it now, The barn is burning: paint it so" in job.prompt
    assert job.prompt.index("burning: paint it so") < job.prompt.index("The place:")
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


def test_a_plan_redrawn_when_asked_keeps_the_old_one_until_the_new_is_drawn(session, monkeypatch):
    """Redraw plan…: a plan drawn badly stayed so, since plans are kept. The
    review: the plan went before a new one existed, so with Draw the map off,
    or once the art model failed twice, the area had no plan for good."""
    from sealedlore.engine.world_art import MAX_FAILURES, adopt, plan_art
    from sealedlore.engine.world_pictures import view_current
    from sealedlore.models.world import MapPicture, PlaceLayout, WorldArt

    old = PlaceLayout(place_id="farm", rows=["#####", "#___#", "#####"], made_at="t1")
    session._art = WorldArt(
        layouts={
            "farm": {
                "": old,
                "burning": PlaceLayout(place_id="farm", rows=["*****"], state="burning"),
            },
            "barn": {"": PlaceLayout(place_id="barn", rows=["###"])},
        },
        failures={"layout:farm:redraw": 2, "layout:farmhouse": 2},
        pictures=[
            MapPicture(subject="isometric", subject_id="farm", name="F", file="f.png", plan="t1")
        ],
    )
    ran: list[bool] = []
    monkeypatch.setattr(session, "start_map_art", lambda: ran.append(True) or True)
    session.config.map_art = False
    assert "Draw the map is off" in session.redraw_problem("farm")
    assert not session.redraw_plan("farm")
    session.config.map_art = True
    assert session.redraw_plan("farm")
    art = session.map_art_copy()
    assert art.layouts["farm"][""] == old and art.redraw == ["farm"]  # kept meanwhile
    assert "layout:farm:redraw" not in art.failures  # asked again: tried again
    assert ran == [True]
    # Planned afresh, wherever the story is (the area in view needn't be visited).
    world = session.world()
    jobs = plan_art(world, art, here=None, setting="", limit=40)
    redraws = [job for job in jobs if job.key == "layout:farm:redraw"]
    assert len(redraws) == 1
    # First in the run, whatever is nearer: twelve nearer pieces kept it out.
    assert jobs[0].key == "layout:farm:redraw"
    assert plan_art(world, art, here=None, setting="", limit=1)[0].key == "layout:farm:redraw"
    view = art.pictures[0]
    assert view_current(world, art, "farm", view)
    # Drawn: it replaces every plan the farm had, and its view is out of date.
    new = PlaceLayout(place_id="farm", rows=["....."], made_at="t2")
    adopt(art, new, redraw=True)
    assert art.layouts["farm"] == {"": new} and art.redraw == []
    assert not view_current(world, art, "farm", view)
    # A redraw that keeps failing leaves the old plan, and is let be.
    art.redraw.append("farm")
    art.failures["layout:farm:redraw"] = MAX_FAILURES
    jobs = plan_art(world, art, here=None, setting="", limit=40)
    assert not any(job.key == "layout:farm:redraw" for job in jobs)
    assert art.layouts["farm"] == {"": new}
    session.enter_private(keep="memory", provider=MockChatProvider(["x"]), model="m")
    assert not session.redraw_plan("barn")  # paused in a private scene


def test_leaving_the_story_stops_its_isometric_view_and_writes_nothing(
    session, tmp_path, monkeypatch
):
    """The review: nothing could stop the painting. Left (or deleted) while
    it painted, the story's folder came back with its map art and picture,
    an unreadable ghost, and the window was called back after closing."""
    import shutil
    import threading

    gate = threading.Event()

    class Slow(FakeImages):
        cancelled = False

        def generate(self, payload):
            gate.wait(10)
            if self.cancelled:
                raise ProviderError("cancelled")
            return super().generate(payload)

        def cancel(self):
            self.cancelled = True
            gate.set()

    monkeypatch.setattr(session_world_art, "ImageClient", Slow)
    called: list[bool] = []
    session.on_map_art = lambda: called.append(True)
    assert session.start_isometric("farm", lambda size: png(16, 12))
    folder = tmp_path / "stories" / session.story.id
    session.close(wait_seconds=0)
    shutil.rmtree(folder)
    session._isometric_thread.join(10)
    assert not session._isometric_thread.is_alive()
    assert not folder.exists() and called == []
    assert not session.start_isometric("farm", lambda size: png(16, 12))  # left for good


def test_a_painting_s_prompt_names_the_hour_the_weather_the_people_and_the_things():
    """Paintings of a place (the author, Oct 17 2026): consistent with the
    map, and a beautiful rendition of the scene as it is now, the people and
    things present included. The people go in by their looks, never a name
    (no writing on the picture), and "isometric" is nowhere in it: the model
    is asked for a three-quarter view matching the picture's angle."""
    from sealedlore.engine.world_pictures import isometric_job

    job = isometric_job(
        farm(),
        WorldArt(),
        "farm",
        look="snow",
        style="gritty and cold",
        when="It is dusk. The weather: snowing hard.",
        people=["A survivor in a heavy coat.", "A tall man with a scar", ""],
        things=["the stone well", "a rifle leaning by the door", "the stone well"],
    )
    prompt = job.prompt
    assert "It is dusk. The weather: snowing hard." in prompt
    assert "A survivor in a heavy coat; A tall man with a scar" in prompt
    assert "the stone well, a rifle leaning by the door" in prompt  # each once
    assert "gritty and cold" in prompt and "snow-covered country" in prompt
    assert "Keep the layout exactly as the picture has it" in prompt
    assert "three-quarter view from above, matching the picture's angle" in prompt
    assert "No writing of any kind" in prompt
    assert "Jane" not in prompt and "isometric" not in prompt.lower()
    assert job.scene == "It is dusk. The weather: snowing hard. 2 there."
    # Nobody there and nothing at hand: the sentences are left out, not emptied.
    bare = isometric_job(farm(), WorldArt(), "farm", look="snow").prompt
    assert "The people in it" not in bare and "The things standing about" not in bare
    assert "It is " not in bare


def test_the_default_painting_model_and_an_older_config_s_both_paint(session):
    """FLUX.3 Klein (the sprites' model, its edit version sent for it) is the
    default painting model; a config still naming FLUX.2 klein 9B (the
    default before Oct 17 2026) keeps painting with it."""
    from sealedlore.models.config import Config

    listed = lambda model: {  # noqa: E731 - a one-line stand-in for the listing
        "id": model,
        "supported_parameters": {"resolutions": ["1024x768", "1024x576"], "max_input_images": 3},
    }
    session.config.map_pictures_mode = "off"
    default = Config().map_isometric_model
    assert default == "blackforestlabs/flux-3/klein/text-to-image" == Config().map_picture_model
    session.config.image_models = [listed(default), listed("flux-2-klein-9b")]
    session.config.map_isometric_model = default
    assert session.isometric_offer()[0] == default and session.isometric_offer()[2] is None
    session.config.map_isometric_model = "flux-2-klein-9b"
    model, _price, problem = session.isometric_offer()
    assert (model, problem) == ("flux-2-klein-9b", None)
    assert session.start_isometric("farm", lambda size: png(16, 12))
    session._isometric_thread.join(10)
    assert FakeImages.sent[-1]["model"] == "flux-2-klein-9b"
    painted = session.map_art_copy().picture_for("isometric", "farm")
    assert painted is not None and painted.model == "flux-2-klein-9b"
    # The session's own scene for the farm: the hour (the story's clock
    # begins at midnight), nobody's name, Jane's looks from her card.
    when, people, _things = session.painting_scene("farm")
    assert when == "It is the small hours."
    assert people == ["A survivor in a heavy coat."] and "Jane" not in painted.prompt
    assert "heavy coat" in painted.prompt and painted.scene == "It is the small hours. 1 there."


def test_asking_first_draws_nothing_until_approved_and_then_exactly_what_was_approved(session):
    """Settings → Images → Draw map pictures: Ask before drawing (the
    author, Oct 17 2026). What the run would draw waits with its price; the
    approved ones go as approved, each once; the declined aren't asked
    about again while the story is open."""
    from dataclasses import replace

    session.config.map_pictures_mode = "ask"
    session.config.image_models = [
        {"id": session.config.map_picture_model, "pricing": {"per_image": {"1024x1024": 0.01}}}
    ]
    assert not session.start_map_pictures() and FakeImages.sent == []
    waiting = session.map_pictures_waiting()
    # Everything the map wants (the day's limit caps the run, not what is
    # asked: an approval beyond the day's room used to be dropped).
    assert len(waiting) == 4 and session.map_picture_jobs(None) == [w.job for w in waiting]
    assert all(w.model == session.config.map_picture_model and w.run == "map" for w in waiting)
    first, second = waiting[:2]
    assert session.approve_map_pictures([replace(first.job, prompt="Exactly this.")])
    session._picture_thread.join(10)
    assert [p["prompt"] for p in FakeImages.sent] == ["Exactly this."]
    # (The play view's avatars wait beside them: that is the tile run's.)
    map_run = [w for w in session.map_pictures_waiting() if w.run == "map"]
    assert [w.job.key for w in map_run] == [w.job.key for w in waiting[1:]]
    assert not session.map_pictures_approved_pending  # an approval is for one drawing
    session.decline_map_pictures([second.job.key])
    map_run = [w for w in session.map_pictures_waiting() if w.run == "map"]
    assert [w.job.key for w in map_run] == [w.job.key for w in waiting[2:]]
    session.config.map_picture_daily_limit = 10
    session.start_map_pictures()
    assert second.job.key not in {w.job.key for w in session.map_pictures_waiting()}
    assert len(FakeImages.sent) == 1
    # Automatically: drawn without asking.
    session.config.map_pictures_mode = "auto"
    run_pictures(session)
    assert len(FakeImages.sent) > 1 and session.map_pictures_waiting() == []
