"""Maps drawn up beforehand: a plot file's `# Map` (engine/plot_md.py), the
map it gives a story (models/world.py), its fog, and what the plot hides.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world import applied, resolve_reply, world_at
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle

SAMPLES = Path(__file__).resolve().parent.parent / "SampleStories"

PLOT = """\
# World
A frozen country.

# Places

## Hellsville
A town.

## The caves
- hidden: yes

Caves in the hills.

# Map
- unit: km
- start: Farm

## Farm
- kind: site
- at: 0, 0
- size: 0.4
- way: Hellsville | north, five miles
- thing: Pickup truck | out of fuel

## Barn
- kind: building
- in: Farm
- at: 0.1, -0.05

## Hellsville
- kind: settlement
- at: 0, 8
- known: yes

## The caves
- kind: site
- at: 4, 8

## Cave mouth
- kind: area
- in: The caves

# Events

## Found the caves
- reveals: The caves
- trigger: They search the hills.
- tell: They find caves.
"""


def scenario():
    parsed = parse_plot_markdown(PLOT)
    assert parsed.ok, [p.describe() for p in parsed.problems]
    return parsed.scenario


def place(world, name):
    return next(p for p in world.places.values() if p.name == name)


def test_a_map_section_gives_the_story_a_fixed_map_and_a_start():
    found = scenario()
    assert found.world_map and found.world_base is not None
    world = applied(world_at([]), found.world_base, base=True)
    farm, barn, town = (place(world, n) for n in ("Farm", "Barn", "Hellsville"))
    assert (farm.x, farm.y, farm.size) == (0.0, 0.0, 400.0)
    assert (barn.x, barn.y) == (100.0, -50.0)  # from the place it is in
    assert barn.inside == farm.id and town.y == 8000.0
    assert found.world_start == farm.id
    (way,) = world.links
    assert (way.direction, way.distance) == ("north", "five miles")
    (truck,) = world.things.values()
    assert truck.at == farm.id and truck.state == "out of fuel"
    assert town.lore_id is not None  # the Places entry of the same name
    assert world.seen == [town.id]  # known; the rest is under fog


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("- at: north", "`at` is two numbers"),
        ("- kind: castle", "`kind` is one of"),
        ("- in: Nowhere", "“Nowhere” isn't a place on the map"),
        ("- way: Nowhere | north", "“Nowhere” isn't another place on the map"),
        ("- size: -2", "`size` is how wide it is"),
    ],
)
def test_mistakes_on_the_map_are_errors_with_a_line(line: str, message: str):
    text = PLOT.replace("# Events", f"## Shed\n{line}\n\n# Events")
    parsed = parse_plot_markdown(text)
    errors = [p for p in parsed.problems if p.error]
    assert errors and message in errors[0].message
    assert errors[0].line == text.split("\n").index(line) + 1


def test_a_start_not_on_the_map_is_an_error():
    parsed = parse_plot_markdown(PLOT.replace("- start: Farm", "- start: Atlantis"))
    assert any("Atlantis" in p.message and p.error for p in parsed.problems)


def test_a_plot_hidden_place_and_all_in_it_stay_off_the_map_until_revealed():
    found = scenario()
    caves = next(e for e in found.lore if e.title == "The caves")
    everything = world_at([], found.world_base)
    hidden = world_at([], found.world_base, hidden={caves.id})
    assert {"The caves", "Cave mouth"} <= {p.name for p in everything.places.values()}
    assert not {"The caves", "Cave mouth"} & {p.name for p in hidden.places.values()}


def test_a_read_naming_a_place_of_the_map_lifts_its_fog_and_adds_no_second():
    world = world_at([], scenario().world_base)
    town = place(world, "Hellsville")
    farm = place(world, "Farm")
    source = "She walked north and came in through the south gate of Hellsville."
    delta, _ = resolve_reply(
        world,
        {
            "where": [{"who": "Jane", "place": "Hellsville", "quote": "came in through"}],
            "places": [{"name": "Farm", "what": "behind her", "quote": "She walked north"}],
        },
        source=source,
        people=[],
    )
    after = applied(world, delta)
    assert len(after.places) == len(world.places)
    assert farm.id in after.seen and town.id in after.seen
    assert (after.places[farm.id].x, after.places[farm.id].y) == (0.0, 0.0)  # never moved


def test_a_new_playthrough_keeps_the_map(tmp_path: Path):
    bundle = bundle_from_scenario(scenario())
    assert bundle.story.defaults.world_map and bundle.story.world_base is not None
    again = scenario_from_bundle(bundle)
    assert again.world_base == bundle.story.world_base
    assert again.world_start == bundle.story.world_start


def test_in_play_the_map_starts_from_the_file_and_the_plot_keeps_its_secret(tmp_path: Path):
    bundle = bundle_from_scenario(scenario())
    bundle.story.defaults.main_model = "m/story"
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        scene_reads="manual",
        map_art=False,
    )
    session = StorySession(
        bundle,
        config,
        MockChatProvider(["x"]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    world = session.world()
    assert world.start == place(world, "Farm").id
    assert "The caves" not in {p.name for p in world.places.values()}  # until revealed
    assert "Barn" in {p.name for p in world.places.values()}


def test_the_mapped_sample_imports_with_its_map():
    text = (SAMPLES / "Doomsville with a map.md").read_text(encoding="utf-8")
    parsed = parse_plot_markdown(text)
    assert parsed.ok and parsed.problems == []
    base = parsed.scenario.world_base
    names = {p.name for p in base.places}
    assert {"The farmhouse", "Hellsville", "The diner", "The Hidden Caves"} <= names
    caves = next(p for p in base.places if p.name == "The Hidden Caves")
    hidden = next(e for e in parsed.scenario.lore if e.title == "The Hidden Caves")
    assert caves.lore_id == hidden.id and hidden.plot_hidden
    start = next(p for p in base.places if p.id == parsed.scenario.world_start)
    assert start.name == "The farmhouse"
    assert json.loads(base.model_dump_json())  # it serialises
