"""Who is there when a story begins: pinned by the author, or placed by the opening.

The author's call (Sept 2026): a new story's cast is the opening's to place,
unless the author pins someone present or elsewhere at the start. Before
this, Setup's starting scene never reached a story that hadn't begun (the
first prompt had no location), and an opening used as written was never
read, so nobody it placed was ever on the roster.
"""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.engine.prompt import SECTION_SCENE, TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle

SERRIK, MAELA, IDRIS = "char-serrik", "char-maela", "char-idris"


def unstarted(tmp_path: Path, cast, responses: list[str], **setup) -> StorySession:
    story = Story(id="story-new", title="New")
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    story.setup.starting_scene = SceneState(location="The gate of Calder Keep")
    for name, value in setup.items():
        setattr(story.setup, name, value)
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        scene_model="small/fast-model",
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def read(**fields) -> str:
    return json.dumps(fields)


def scene_text(session: StorySession) -> str:
    return session.last_prompt.section(SECTION_SCENE).text


def turn(text: str = "I knock.") -> TurnRequest:
    return TurnRequest(speaker_id=SERRIK, user_text=text, controlled_character_id=SERRIK)


def test_setup_s_starting_scene_is_where_an_unstarted_story_begins(tmp_path: Path, cast):
    session = unstarted(
        tmp_path,
        cast,
        ["Rain on the gate.", read(already_here=["Serrik Vaun"])],
        opening_text="Serrik arrives at the gate.",
    )
    assert session.story.scene.location is None  # Setup wrote only the starting scene

    list(session.begin(SERRIK))

    assert "Location: The gate of Calder Keep" in scene_text(session)


def test_the_opening_places_whoever_the_author_did_not_pin(tmp_path: Path, cast):
    session = unstarted(
        tmp_path,
        cast,
        [
            "Maela was already at the gate, picking the lock.",
            read(already_here=["Serrik Vaun", "Maela Orr"]),
            "Maela swore.",
            read(),
        ],
        opening_text="Serrik arrives at the gate.",
    )
    session.pin_at_start(present=[], absent=[IDRIS])

    list(session.begin(SERRIK))
    opening = scene_text(session)
    unplaced = opening.partition("NOT PLACED YET")[2].partition("NOT IN THE SCENE YET")
    assert "Maela Orr" in unplaced[0]
    assert "Captain Idris" in unplaced[2]
    assert MAELA in session.story.scene.present_character_ids

    list(session.send(turn()))
    later = scene_text(session)
    assert "NOT PLACED YET" not in later
    assert "Maela Orr" in later.partition("NOT IN THE SCENE")[0]


def test_someone_pinned_present_stays_whatever_the_opening_mentions(tmp_path: Path, cast):
    session = unstarted(
        tmp_path,
        cast,
        ["Rain on the gate.", read(already_here=["Serrik Vaun"])],
        opening_text="Serrik arrives at the gate.",
    )
    session.pin_at_start(present=[MAELA], absent=[])

    list(session.begin(SERRIK))

    assert "Maela Orr" in scene_text(session).partition("NOT PLACED YET")[0]
    assert session.story.scene.present_character_ids == [SERRIK, MAELA]


def test_the_opening_read_asks_who_was_there_even_on_a_tracked_card(tmp_path: Path, cast):
    """A scenario exported mid-play can carry a tracked card; nothing was under
    way before the first passage, so who it finds there was there all along."""
    session = unstarted(
        tmp_path,
        cast,
        ["Maela was at the gate.", read(already_here=["Serrik Vaun", "Maela Orr"])],
        opening_text="Serrik arrives at the gate.",
        starting_scene=SceneState(location="The gate", tracked=True),
    )

    list(session.begin(SERRIK))

    request = session.provider.requests[-1]
    assert "already_here" in request.messages[0].text
    assert MAELA in session.story.scene.present_character_ids


def test_an_opening_used_as_written_is_read(tmp_path: Path, cast):
    session = unstarted(
        tmp_path,
        cast,
        [read(already_here=["Serrik Vaun", "Maela Orr"], situation="Waiting in the rain.")],
        opening_text="Serrik and Maela waited at the gate.",
        opening_mode="as_written",
    )

    list(session.begin(SERRIK))

    root = session.nodes[0]
    assert root.meta.scene_read
    assert session.story.scene.present_character_ids == [SERRIK, MAELA]
    assert session.story.scene.situation == "Waiting in the rain."
    assert session.last_result is None  # no call wrote the passage


def test_the_scene_page_before_the_start_sets_the_starting_scene(tmp_path: Path, cast):
    session = unstarted(tmp_path, cast, [], absent_at_start=[MAELA])

    session.set_scene(SceneState(location="The ferry", present_character_ids=[MAELA]))

    assert session.story.setup.starting_scene.location == "The ferry"
    assert session.story.setup.starting_scene.present_character_ids == [MAELA]
    assert session.story.setup.absent_at_start == []


def test_a_restart_places_them_the_same_way(tmp_path: Path, cast):
    session = unstarted(tmp_path, cast, [])
    session.pin_at_start(present=[MAELA], absent=[IDRIS, "someone-gone"])

    scenario = scenario_from_bundle(session.bundle)
    again = bundle_from_scenario(scenario)

    assert again.story.setup.starting_scene.present_character_ids == [MAELA]
    assert again.story.setup.absent_at_start == [IDRIS]
