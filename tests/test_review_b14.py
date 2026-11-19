"""What the review before 3.1.0b14 found in characters played by their own
model, the story's "encrypted" mark and the prompt editor; each test fails on
the code as it was."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from sealedlore.engine import card_link
from sealedlore.engine.model_tuning import models_left_out_for, models_worded_for
from sealedlore.engine.reactions import in_scene
from sealedlore.engine.session import SessionNotice
from sealedlore.engine.validators import named_only
from sealedlore.models.character import Character
from sealedlore.models.route import ModelRoute
from sealedlore.models.scene import SceneState
from sealedlore.providers.base import ProviderError, StreamCompleted
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage import collection
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.scenario import (
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from tests.test_reactions import MAELA_MODEL, a_cast, a_story, make_session, play, sent, turn


class NoSuchEndpoint(MockChatProvider):
    """As the session's provider answers for a model whose endpoint isn't set
    up: the request can't even be built."""

    def build_payload(self, request):
        if request.model.startswith("Gone::"):
            raise ProviderError(f"{request.model} is on the endpoint 'Gone', which isn't set up")
        return super().build_payload(request)


def notices(events) -> list[str]:
    return [event.text for event in events if isinstance(event, SessionNotice)]


# --- 1: a model that can't be reached never costs the turn -----------------------


def test_an_own_model_on_an_endpoint_not_set_up_is_said_and_the_turn_goes_on(tmp_path: Path):
    cast = a_cast()
    cast[1].own_model = "Gone::maela-lora"
    session = make_session(tmp_path, ["The door gives."], cast=cast, provider=NoSuchEndpoint())
    said = notices(play(session, turn()))
    assert session.path()[-1].content == "The door gives."
    assert any("Maela Orr's model" in text and "couldn't be asked" in text for text in said)


def test_with_the_round_after_the_turn_the_passage_is_still_written(tmp_path: Path):
    cast = a_cast()
    cast[1].own_model = "Gone::maela-lora"
    story = a_story()
    story.defaults.round_order = "after"
    session = make_session(
        tmp_path, ["The door gives."], story=story, cast=cast, provider=NoSuchEndpoint()
    )
    play(session, turn())
    assert session.path()[-1].content == "The door gives."


def test_a_record_keeper_that_cant_be_reached_is_said(tmp_path: Path):
    session = make_session(
        tmp_path, ["The door gives.", "I pick it."], provider=NoSuchEndpoint(), record=True
    )
    session.config.record_model = "Gone::keeper"
    said = notices(play(session, turn()))
    assert any("Couldn't keep the public record" in text for text in said)
    assert session.path()[-1].meta.reactions, "the round went on"


# --- 4: a namesake on the roster isn't them ----------------------------------------


def test_someone_who_shares_a_name_doesnt_put_them_in_the_scene():
    john = Character(name="John Smith", aliases=["the Smith boy"])
    assert not in_scene(john, SceneState(present_others=["Mary Smith"]))
    assert in_scene(john, SceneState(present_others=["John"]))
    assert in_scene(john, SceneState(present_others=["Mr Smith"]))
    assert in_scene(john, SceneState(present_others=["the Smith boy"]))
    assert named_only("Sergeant Hale", Character(name="Tom Hale"))
    assert not named_only("Sergeant", Character(name="Sergeant Hale"))


# --- 5: answers to a passage that has gone on ---------------------------------------


def test_an_answer_doesnt_outlive_a_continue_that_leaves_nobody_to_ask(tmp_path: Path):
    session = make_session(
        tmp_path, ["The door gives.", "I reach for my picks.", "Maela slips away up the stair."]
    )
    play(session, turn())
    passage = session.path()[-1]
    assert [a.text for a in passage.meta.reactions] == ["I reach for my picks."]
    # She has left by the time the passage is carried on.
    session.story.scene.present_character_ids = ["char-serrik"]
    passage.meta.scene = session.story.scene.model_copy(deep=True)
    list(session.continue_passage())
    assert "slips away" in passage.content
    assert passage.meta.reactions == []


# --- 6: the record reads what Continue added ------------------------------------------


def test_the_public_record_reads_a_passage_again_once_it_is_carried_on(tmp_path: Path):
    session = make_session(
        tmp_path,
        [
            "The door gives.",
            '{"facts": ["A door gave."]}',
            "I pick the lock.",
            "Across the town the great bell cracks.",
            '{"facts": ["A door gave.", "The great bell cracked."]}',
            "Hm.",
        ],
        record=True,
    )
    play(session, turn())
    passage = session.path()[-1]
    assert passage.meta.public_record == ["A door gave."]
    list(session.continue_passage())
    keepers = [r for r in session.provider.requests if "THE RECORD NOW" in sent(r)]
    assert len(keepers) == 2 and "great bell cracks" in sent(keepers[1])
    assert passage.meta.public_record == ["A door gave.", "The great bell cracked."]
    # Read against the record before it, not on top of its own first reading.
    assert "1. A door gave." not in sent(keepers[1])


# --- 7: what the characters cost is in the session's spend ------------------------------


def test_own_model_answers_are_counted_in_the_sessions_spend(tmp_path: Path):
    session = make_session(tmp_path, ["The door gives.", "I reach for my picks."])
    play(session, turn())
    [answer] = session.path()[-1].meta.reactions
    assert answer.usage in session.unreported_usage


# --- 3: the model that plays them stays with the story -----------------------------------


def own_model_character() -> Character:
    return Character(
        name="Maela Orr",
        own_model=MAELA_MODEL,
        own_route=ModelRoute(priority="latency"),
        own_notes="Play her wary.",
    )


def gone(character: Character) -> bool:
    return (character.own_model, character.own_route, character.own_notes) == (None, None, None)


def test_saved_to_your_characters_they_go_without_their_model():
    assert gone(card_link.kept_from_story(own_model_character(), "The Sundering"))


def test_a_card_joining_a_story_brings_no_model():
    card = collection.kept("Maela Orr")
    card.own_model, card.own_notes = MAELA_MODEL, "Play her wary."
    assert gone(card_link.story_copy(card, [card], "Serrik"))


def test_a_scenario_file_carries_no_own_model_and_restart_keeps_it(tmp_path: Path):
    bundle = StoryBundle(story=a_story(), cast=[own_model_character()])
    path = tmp_path / "s.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle))
    assert MAELA_MODEL not in path.read_text(encoding="utf-8")
    assert gone(read_scenario(path).cast[0])
    # The story's own copy isn't touched by the export.
    assert bundle.cast[0].own_model == MAELA_MODEL
    # A file made elsewhere that names one: this computer's to choose.
    scenario = scenario_from_bundle(bundle)
    data = scenario.model_dump_json()
    path.write_text(data, encoding="utf-8")
    assert gone(read_scenario(path).cast[0])
    # Restart and Duplicate settings stay on this computer: kept.
    assert fresh_playthrough(bundle, "Again").cast[0].own_model == MAELA_MODEL


# --- 2 and 8: the story's "encrypted" mark --------------------------------------------------


class SealsPrivate(MockChatProvider):
    """Replies sealed for a private/ model, as its client gives them."""

    def stream(self, request):
        for event in super().stream(request):
            if isinstance(event, StreamCompleted) and request.model.startswith("private/"):
                event = dataclasses.replace(event, sealed=True)
            yield event


def sealed_story(tmp_path: Path, responses: list[str], **config):
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.models.config import Config, ProviderConfig

    story = a_story()
    story.defaults.main_model = None
    cast = a_cast()
    cast[1].own_model = None
    settings = {"lore_model": "private/deepseek-v4-1-flash", **config}
    session = StorySession(
        StoryBundle(story=story, cast=cast),
        Config(
            providers=[ProviderConfig(name="NanoGPT", api_key="k", model="private/glm-5-3")],
            active_provider_name="NanoGPT",
            scene_reads="manual",
            plot_reads=False,
            **settings,
        ),
        SealsPrivate(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    return session


def test_a_story_is_marked_only_when_every_model_is_encrypted(tmp_path: Path):
    session = sealed_story(tmp_path / "all", ["The torch gutters."])
    play(session, turn())
    user, passage = session.path()[-2:]
    assert (user.meta.tee, passage.meta.tee) == ("encrypted", "encrypted")
    # One job on an ordinary model: the passage would be read in the clear.
    for name, changes in {
        "scene": {"scene_model": "z-ai/glm-5.3"},
        "lore": {"lore_model": "deepseek/deepseek-v4.1-flash"},
        "jev": {"lore_selector": "jev"},
    }.items():
        mixed = sealed_story(tmp_path / name, ["The torch gutters."], **changes)
        play(mixed, turn())
        user, passage = mixed.path()[-2:]
        assert (user.meta.tee, passage.meta.tee) == (None, None), name


def test_a_character_on_a_plain_model_of_their_own_means_no_mark(tmp_path: Path):
    session = sealed_story(tmp_path, ["The torch gutters.", "I watch the dark."])
    session.bundle.cast[1].own_model = MAELA_MODEL
    session.config.public_record = False
    play(session, turn())
    assert session.path()[-1].meta.tee is None


def test_carried_on_by_a_plain_model_the_passage_and_its_turn_lose_the_mark(tmp_path: Path):
    session = sealed_story(tmp_path, ["The torch gutters.", "Then it dies."])
    play(session, turn())
    user, passage = session.path()[-2:]
    assert passage.meta.tee == "encrypted"
    session.config.active_provider().model = "anthropic/claude-sonnet-4.6"
    list(session.continue_passage())
    assert "Then it dies." in passage.content
    assert (user.meta.tee, passage.meta.tee) == (None, None)


def test_carried_on_still_sealed_it_keeps_the_mark(tmp_path: Path):
    session = sealed_story(tmp_path, ["The torch gutters.", "Then it dies."])
    play(session, turn())
    list(session.continue_passage())
    user, passage = session.path()[-2:]
    assert (user.meta.tee, passage.meta.tee) == ("encrypted", "encrypted")


# --- 9: the prompt editor says which it is ------------------------------------------------------


def test_a_text_left_out_for_a_model_isnt_said_to_take_the_authors_edit():
    assert models_worded_for("turn.round.remember") == ""
    assert "Opus" in models_left_out_for("turn.round.remember")
    assert models_left_out_for("characters.own.ask") == ""
    assert models_worded_for("characters.own.ask")
