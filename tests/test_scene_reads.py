"""The scene keeps itself: a read after each passage, snapshots on the nodes.

Replayed over 299 logged turns, the hand-kept roster changed three times and
every presence alert raised against it was wrong. These tests drive the
replacement end to end against the mock provider: each turn is two calls, the
passage and then the scene read's JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import SECTION_REMINDER, TurnRequest
from sealedlore.engine.scene_state import (
    describe_changes,
    log_on_path,
    move_into_cast,
    move_out_of_cast,
    same_person,
    scene_at,
)
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.scene import SceneChange, SceneLogEntry, SceneState
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from tests.conftest import make_exchange

HOLLIS = Character(
    id="card-hollis", name="Constable Hollis", aliases=["Hollis"], summary="Station security."
)
GUS = Character(id="card-gus", name="Gus", summary="Runs the bar.")


def delta(**fields) -> str:
    return json.dumps(fields)


NOTHING = delta()


def make_session(tmp_path: Path, story, cast, responses: list[str]) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    bundle.supporting.characters.extend([HOLLIS.model_copy(deep=True), GUS.model_copy(deep=True)])
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
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


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.scene.tracked = True
    return make_session(
        tmp_path,
        story,
        cast,
        [
            "Hollis stepped in from the corridor, arms folded.",
            delta(arrived=[{"name": "Hollis", "how": "came in from the corridor"}]),
        ],
    )


def turn(text: str = "I put my shoulder to the door.") -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def respond(session: StorySession, *responses: str) -> None:
    session.provider = MockChatProvider(list(responses))


# --- the read after each passage ---------------------------------------------


def test_a_turn_reads_the_scene_and_the_card_follows(session: StorySession):
    events = list(session.send(turn()))

    passage = session.nodes[-1]
    assert passage.meta.scene is not None
    assert passage.meta.scene_read
    assert passage.meta.scene.present_others == ["Constable Hollis"]
    assert session.story.scene.present_others == ["Constable Hollis"]
    notices = [event.text for event in events if isinstance(event, SessionNotice)]
    assert "Reading the scene with small/fast-model…" in notices
    assert "Scene: +Constable Hollis (came in from the corridor)" in notices


def test_the_read_goes_to_the_scene_model_and_is_logged_and_costed(
    session: StorySession, tmp_path: Path
):
    list(session.send(turn()))

    read = session.provider.requests[-1]
    assert read.model == "small/fast-model"
    assert "THE LATEST PASSAGE" in read.messages[-1].text
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["request", "response", "scene_request", "scene_response"]
    assert len(session.unreported_usage) == 1


def test_the_scene_model_defaults_to_the_summarisation_model(session: StorySession):
    session.config.scene_model = None
    session.story.defaults.summarization_model = "cheap/summariser"
    assert session.scene_model == "cheap/summariser"


def test_the_read_survives_a_reload(session: StorySession, tmp_path: Path):
    list(session.send(turn()))
    reloaded = load_story_bundle(session.story.id, root=tmp_path)

    assert reloaded.nodes[-1].meta.scene.present_others == ["Constable Hollis"]
    assert reloaded.story.scene.present_others == ["Constable Hollis"]


def test_a_failed_read_never_costs_the_turn(session: StorySession):
    text = "Maela gasped as Captain Idris took the key from her hand."
    respond(session, text, "I can't tell, sorry.")

    events = list(session.send(turn()))

    passage = session.nodes[-1]
    assert passage.content == text
    assert passage.meta.scene is None and not passage.meta.scene_read
    assert session.story.scene.present_character_ids == ["char-serrik", "char-maela"]
    # No name check stands in any more (it was wrong more often than right).
    assert any("Couldn't read the scene" in e.text for e in events if isinstance(e, SessionNotice))


def test_a_provider_error_during_the_read_is_a_notice(session: StorySession):
    class ReadFails(MockChatProvider):
        def complete(self, request):
            raise ProviderError("timed out")

    session.provider = ReadFails(["The door gives."])
    events = list(session.send(turn()))

    assert session.nodes[-1].content == "The door gives."
    assert any("timed out" in e.text for e in events if isinstance(e, SessionNotice))


def test_with_the_ledger_on_the_name_patterns_leave_presence_alone(session: StorySession):
    """ "Captain Idris crumpled the dispatch" in a cutaway is for the read to judge."""
    respond(session, "Three days north, Captain Idris crumpled the dispatch.", NOTHING)
    list(session.send(turn()))


def test_manual_mode_makes_one_call_a_turn(session: StorySession):
    session.config.scene_reads = "manual"
    list(session.send(turn()))

    assert len(session.provider.requests) == 1
    assert session.nodes[-1].meta.scene is None


def test_a_stopped_passage_is_not_read(session: StorySession):
    session.provider = MockChatProvider(["one two three four five six", NOTHING], chunk_size=4)
    events = session.send(turn())
    next(events)
    events.close()

    assert len(session.provider.requests) == 1
    assert session.nodes[-1].meta.scene is None


def test_a_shortcut_is_flagged_with_its_sentence(session: StorySession):
    passage = "Jane spoke softly. Down the corridor, Gus had been listening from the junction."
    respond(
        session,
        passage,
        delta(
            shortcuts=[
                {
                    "name": "Gus",
                    "kind": "perceived_from_outside",
                    # Retyped by the model: case and quote marks differ.
                    "quote": "down the corridor, Gus had been listening from the junction",
                },
                {
                    "name": "Hollis",
                    "kind": "already_there",
                    "quote": "Hollis was never in this text.",
                },
            ]
        ),
    )

    events = list(session.send(turn()))

    node = session.nodes[-1]
    # The one it couldn't point at is not an accusation it gets to make.
    assert [s.name for s in node.meta.scene_shortcuts] == ["Gus"]
    # No banner: the scene notice names it once.
    notices = [e.text for e in events if isinstance(e, SessionNotice)]
    assert any("shortcut — Gus perceived from outside" in text for text in notices)


def test_editing_away_the_sentence_clears_the_shortcut(session: StorySession):
    passage = "Jane spoke softly. Gus had been listening from the junction."
    respond(
        session,
        passage,
        delta(
            shortcuts=[
                {
                    "name": "Gus",
                    "kind": "perceived_from_outside",
                    "quote": "Gus had been listening from the junction.",
                }
            ]
        ),
    )
    list(session.send(turn()))
    node = session.nodes[-1]

    session.edit_node(node.id, "Jane spoke softly.")

    assert node.meta.scene_shortcuts == []


# --- supporting characters and everyone else --------------------------------


def test_someone_present_rides_the_tail_without_being_named_lately(session: StorySession):
    list(session.send(turn()))  # Hollis comes in
    respond(session, "The lamp hissed.", NOTHING)
    for _ in range(12):
        list(session.send(turn("I wait.")))
        session.nodes[-1].content = "The lamp hissed."

    tail = session.last_prompt.section("tail.supporting").text
    assert "Station security." in tail


def test_a_tracked_scene_names_the_people_on_file_who_are_not_here(session: StorySession):
    list(session.send(turn()))
    prompt = session.assemble(turn("I look around."))
    scene = prompt.section("tail.scene").text

    assert "also here, not in the cast: Constable Hollis" in scene
    assert "ON FILE, NOT HERE YET" in scene
    assert "Gus" in scene.split("ON FILE, NOT HERE YET")[1]
    assert "Constable Hollis" not in scene.split("ON FILE, NOT HERE YET")[1]
    # Who may come, with no "cannot" beside them (see test_prompt_assembly).
    assert "cannot" not in scene.split("ON FILE, NOT HERE YET")[1]
    # Tail only: the cached system block never carries the roster.
    assert "Gus" not in prompt.messages[0].text


def test_an_untracked_scene_says_nothing_about_the_people_on_file(session: StorySession):
    session.story.scene.tracked = False
    scene = session.assemble(turn()).section("tail.scene").text
    assert "ON FILE, NOT HERE YET" not in scene


def test_the_first_read_of_an_untracked_card_looks_back(session: StorySession):
    session.story.scene.tracked = False
    session.set_scene(session.story.scene)
    respond(
        session,
        "Hollis said nothing.",
        delta(already_here=["Serrik Vaun", "Maela Orr", "Hollis"]),
        "The lamp hissed.",
        NOTHING,
    )

    list(session.send(turn()))
    first_read = session.provider.requests[1].messages
    list(session.send(turn("I wait.")))
    second_read = session.provider.requests[3].messages

    assert "already_here" in first_read[0].text
    assert "already_here" not in second_read[0].text
    assert session.story.scene.present_others == ["Constable Hollis"]
    assert session.story.scene.tracked


# --- snapshots follow the story ------------------------------------------------


def test_each_take_keeps_its_own_scene(session: StorySession):
    list(session.send(turn()))  # Hollis arrives in the first take
    first = session.nodes[-1]
    respond(
        session,
        "Gus came in, rubbing his hands.",
        delta(arrived=[{"name": "Gus", "how": "came in"}]),
    )

    list(session.regenerate())
    second = session.nodes[-1]

    assert second.parent_id == first.parent_id
    assert session.story.scene.present_others == ["Gus"]
    # The retake was written against the scene before either take, not the first take's.
    retake_scene = session.provider.requests[0].messages[-1].text
    assert "also here, not in the cast" not in retake_scene

    session.switch_to(first.id)
    assert session.story.scene.present_others == ["Constable Hollis"]


def test_deleting_a_passage_steps_the_scene_back(session: StorySession):
    list(session.send(turn()))
    assert session.story.scene.present_others == ["Constable Hollis"]

    session.delete_from(session.nodes[-1].id)

    assert session.story.scene.present_others == []


def test_branching_from_earlier_gives_that_points_scene(session: StorySession):
    list(session.send(turn()))
    root = session.nodes[0]
    respond(session, "Gus came in.", delta(arrived=["Gus"]))
    list(session.send(turn("I nod.")))
    assert session.story.scene.present_others == ["Constable Hollis", "Gus"]

    session.branch_from(session.nodes[1].id)
    assert session.story.scene.present_others == ["Constable Hollis"]
    session.branch_from(root.id)
    assert session.story.scene.present_others == []


def test_undo_takes_back_what_the_read_did(session: StorySession):
    list(session.send(turn()))
    assert session.scene_changed_here()

    assert session.undo_scene_update()

    assert session.story.scene.present_others == []
    assert not session.scene_changed_here()
    assert not session.undo_scene_update()


def test_the_authors_edit_wins_over_the_read(session: StorySession):
    list(session.send(turn()))
    scene = session.story.scene
    scene.present_others = []
    session.set_scene(scene)

    assert not session.scene_changed_here()
    assert session.nodes[-1].meta.scene.present_others == []
    assert session.nodes[-1].meta.scene.source == "author"


def test_every_path_starts_from_a_scene(session: StorySession):
    list(session.send(turn()))
    root = session.nodes[0]
    assert root.meta.scene is not None
    assert root.meta.scene.present_character_ids == ["char-serrik", "char-maela"]


# --- who knows what -----------------------------------------------------------


def test_a_scene_that_ends_goes_into_the_log_and_the_next_prompt(session: StorySession):
    session.story.scene.privacy = "private"
    session.set_scene(session.story.scene)
    respond(
        session,
        "Serrik left Maela in the undercroft and climbed to the yard.",
        delta(
            location="The yard",
            left=[{"name": "Maela Orr", "how": "stayed below"}],
            scene_closed={"gist": "Serrik told Maela about the debt."},
        ),
    )
    list(session.send(turn()))

    entry = session.story.scene_log[-1]
    assert entry.location == "The undercroft beneath Calder Keep"
    assert entry.privacy == "private"
    assert entry.present_names == ["Serrik Vaun", "Maela Orr"]
    assert entry.gist == "Serrik told Maela about the debt."
    assert session.story.scene.location == "The yard"
    assert session.story.scene.privacy is None

    log = session.assemble(turn("I look up.")).section("tail.scene_log").text
    assert "The undercroft beneath Calder Keep (private): Serrik Vaun, Maela Orr" in log


def test_undoing_the_read_takes_its_log_entry_too(session: StorySession):
    respond(
        session,
        "Serrik climbed to the yard.",
        delta(location="The yard", scene_closed={"gist": "They parted."}),
    )
    list(session.send(turn()))
    assert session.story.scene_log

    session.undo_scene_update()

    assert session.story.scene_log == []


def test_deleting_the_passage_takes_its_log_entry(session: StorySession):
    respond(
        session,
        "Serrik climbed to the yard.",
        delta(location="The yard", scene_closed={"gist": "They parted."}),
    )
    list(session.send(turn()))

    session.delete_from(session.nodes[-1].id)

    assert session.story.scene_log == []


def test_a_private_scene_is_said_so_where_the_model_reads_last(session: StorySession):
    session.story.scene.privacy = "private"
    prompt = session.assemble(turn())

    assert "This scene is private" in prompt.section("tail.scene").text
    assert "stays with those in it" in prompt.section(SECTION_REMINDER).text


def test_the_scene_log_only_shows_what_this_path_passed_through():
    nodes = make_exchange(2)
    entries = [
        SceneLogEntry(location="A", closed_at_node_id=nodes[1].id),
        SceneLogEntry(location="B", closed_at_node_id="another-branch"),
        SceneLogEntry(location="C", closed_at_node_id=nodes[3].id),
    ]
    assert [e.location for e in log_on_path(entries, nodes)] == ["A", "C"]
    assert [e.location for e in log_on_path(entries, nodes, limit=1)] == ["C"]


# --- taking up a character --------------------------------------------------------


def test_holding_someone_elsewhere_cuts_the_scene_to_them(session: StorySession):
    list(session.send(turn()))  # a tracked scene with Hollis in it
    moved = session.hold("char-idris")

    assert moved
    assert session.story.held_character_id == "char-idris"
    assert session.story.scene.present_character_ids == ["char-idris"]
    assert session.story.scene.present_others == []
    assert session.story.scene_log[-1].present_names == [
        "Serrik Vaun",
        "Maela Orr",
        "Constable Hollis",
    ]


def test_on_a_hand_kept_roster_holding_someone_brings_them_in(session: StorySession):
    session.story.scene.tracked = False
    list(session.send(turn()))
    session.story.scene.tracked = False  # as a story before the reads would be
    session.nodes[-1].meta.scene = None

    moved = session.hold("char-idris")

    assert not moved
    assert "char-idris" in session.story.scene.present_character_ids


def test_holding_someone_already_here_changes_nothing(session: StorySession):
    assert not session.hold("char-maela")
    assert session.story.scene.present_character_ids == ["char-serrik", "char-maela"]


# --- moving between cast and supporting ----------------------------------------------


def test_demoting_someone_in_the_room_keeps_them_there_by_name(session: StorySession):
    list(session.send(turn()))
    session.demote("char-maela")

    assert "char-maela" not in session.story.scene.present_character_ids
    assert "Maela Orr" in session.story.scene.present_others
    assert all(
        "char-maela" not in node.meta.scene.present_character_ids
        for node in session.nodes
        if node.meta.scene
    )

    maela = next(c for c in session.supporting if c.id == "char-maela")
    session.promote(maela.id)
    assert "char-maela" in session.story.scene.present_character_ids
    assert "Maela Orr" not in session.story.scene.present_others


# --- the pure helpers ------------------------------------------------------------


def test_the_nearest_snapshot_wins_and_demoted_ids_drop_out():
    nodes = make_exchange(2)
    nodes[1].meta.scene = SceneState(location="One", present_character_ids=["a", "gone"])
    fallback = SceneState(location="Fallback")

    assert scene_at(nodes, fallback, cast_ids={"a"}).present_character_ids == ["a"]
    assert scene_at(nodes[:1], fallback).location == "Fallback"
    nodes[3].meta.scene = SceneState(location="Two")
    assert scene_at(nodes, fallback).location == "Two"


def test_names_match_a_card_either_way_round():
    assert same_person("Hollis", HOLLIS)
    assert same_person("Constable Hollis", Character(name="Hollis"))
    assert not same_person("Gus", HOLLIS)


def test_changes_read_as_one_line():
    line = describe_changes(
        [
            SceneChange(kind="arrived", name="Hollis", detail="from the corridor"),
            SceneChange(kind="left", name="Ruiz"),
            SceneChange(kind="location", detail="the infirmary"),
            SceneChange(kind="privacy", detail="private"),
        ]
    )
    assert line == "+Hollis (from the corridor), −Ruiz, now: the infirmary, private scene"


def test_moving_a_character_between_tiers_keeps_their_place():
    scene = SceneState(present_character_ids=["c1"])
    character = Character(id="c1", name="Maela Orr")
    move_out_of_cast(scene, character)
    assert scene.present_others == ["Maela Orr"]
    move_into_cast(scene, character)
    assert scene.present_character_ids == ["c1"] and scene.present_others == []


# --- the perspective decides what counts as reaching in -----------------------

RADIO_PASSAGE = "Serrik waited. In the radio room, Tess was tracking him on the fence sensors."
RADIO_READ = delta(
    shortcuts=[
        {
            "name": "Tess",
            "kind": "perceived_from_outside",
            "quote": "In the radio room, Tess was tracking him on the fence sensors.",
        }
    ]
)


def test_following_the_whole_story_the_radio_room_may_watch(session: StorySession):
    session.story.style.perspective = "whole_story"
    respond(session, RADIO_PASSAGE, RADIO_READ)
    list(session.send(turn()))

    assert "None of that is a shortcut" in session.provider.requests[-1].messages[0].text


def test_staying_with_the_character_the_radio_room_may_not(session: StorySession):
    session.story.style.perspective = "with_character"
    respond(session, RADIO_PASSAGE, RADIO_READ)
    list(session.send(turn()))

    assert "even if it is" in session.provider.requests[-1].messages[0].text
    assert [s.name for s in session.nodes[-1].meta.scene_shortcuts] == ["Tess"]
