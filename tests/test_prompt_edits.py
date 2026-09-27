"""The author's edits to the prompt texts: per story, for every story, and kept."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_edits import (
    copied,
    default_changed,
    missing_slots,
    reset,
    set_text,
    source_of,
    texts_in_force,
    unknown_keys,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.prompt_edit import PromptEdit
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_config,
    load_story_bundle,
    save_config,
    save_story_bundle,
)
from sealedlore.storage.scenario import (
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)

RULES = "storyteller.rules"


def edits(**texts: str) -> dict[str, PromptEdit]:
    found: dict[str, PromptEdit] = {}
    for key, text in texts.items():
        set_text(found, key.replace("__", "."), text)
    return found


def test_a_storys_edit_wins_over_one_for_every_story():
    app = edits(storyteller__rules="For every story.", question__rules="Asked plainly.")
    story = edits(storyteller__rules="For this story.")
    texts = texts_in_force(app, story)
    assert texts[RULES] == "For this story."
    assert texts["question.rules"] == "Asked plainly."
    assert texts["turn.ooc"] == DEFAULT_TEXTS["turn.ooc"]
    assert source_of(RULES, app, story) == "story"
    assert source_of("question.rules", app, story) == "app"
    assert source_of("turn.ooc", app, story) == "default"


def test_writing_the_default_back_is_a_reset():
    """An edit identical to the default would stop following it when it changes."""
    found = edits(storyteller__rules="Mine.")
    set_text(found, RULES, DEFAULT_TEXTS[RULES])
    assert found == {}


def test_reset_clears_the_keys_given_or_everything():
    found = edits(storyteller__rules="A.", question__rules="B.")
    reset(found, [RULES])
    assert list(found) == ["question.rules"]
    reset(found)
    assert found == {}


def test_a_changed_default_is_noticed():
    found = edits(storyteller__rules="Mine.")
    assert not default_changed(RULES, found[RULES])
    stale = found[RULES].model_copy(update={"default_sha": "0" * 16})
    assert default_changed(RULES, stale)


def test_an_edit_to_a_text_the_program_no_longer_has_is_kept_but_never_sent():
    found = {"retired.text": PromptEdit(text="Old."), **edits(storyteller__rules="Mine.")}
    assert unknown_keys(found) == ["retired.text"]
    assert texts_in_force(found).overrides == {RULES: "Mine."}


def test_a_placeholder_left_out_is_named():
    assert missing_slots("turn.directives.held", "Do not write them.") == ["held"]
    assert missing_slots("turn.directives.held", "Never write {held}.") == []


def test_copying_another_storys_edits_makes_new_records():
    source = edits(storyteller__rules="A.", question__rules="B.")
    one = copied(source, [RULES])
    assert list(one) == [RULES] and one[RULES] is not source[RULES]
    assert set(copied(source)) == {RULES, "question.rules"}


# --- kept on disk, and carried with the story ------------------------------------


def test_edits_survive_saving_the_story_and_the_config(tmp_path: Path):
    story = Story(title="T", prompt_edits=edits(storyteller__rules="Story rules."))
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    config = Config(prompt_edits=edits(question__rules="Plainly."))
    save_config(config, root=tmp_path)

    assert load_story_bundle(story.id, root=tmp_path).story.prompt_edits == story.prompt_edits
    assert load_config(root=tmp_path).prompt_edits == config.prompt_edits


def test_a_scenario_and_a_restart_carry_the_storys_edits(tmp_path: Path):
    bundle = StoryBundle(story=Story(title="T", prompt_edits=edits(storyteller__rules="Mine.")))
    path = tmp_path / "t.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle))
    assert read_scenario(path).prompt_edits == bundle.story.prompt_edits
    assert fresh_playthrough(bundle, "Again").story.prompt_edits == bundle.story.prompt_edits


# --- what the session sends -----------------------------------------------------


def test_the_session_sends_the_edits_to_the_storyteller_and_the_side_calls(tmp_path: Path):
    story = Story(title="T", prompt_edits=edits(storyteller__rules="STORY RULES."))
    story.scene = SceneState(present_character_ids=["c1"], tracked=True)
    story.held_character_id = "c1"
    bundle = StoryBundle(story=story, cast=[Character(id="c1", name="Ana Reyes")])
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://example.com/v1", model="m")],
        active_provider_name="p",
        prompt_edits=edits(scene_read__system="SCENE READ. {focus}"),
    )
    provider = MockChatProvider(["She waits.", "{}"])
    session = StorySession(
        bundle,
        config,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    list(
        session.send(
            TurnRequest(speaker_id="c1", user_text="I wait.", controlled_character_id="c1")
        )
    )
    systems = [request.messages[0].text for request in provider.requests]
    assert any(text.startswith("STORY RULES.") for text in systems)
    assert any(text.startswith("SCENE READ. The scene is wherever Ana Reyes") for text in systems)


# --- the settings review --------------------------------------------------------


def review_session(tmp_path: Path, reply: dict) -> StorySession:
    story = Story(title="T")
    story.held_character_id = "c1"
    story.scene = SceneState(present_character_ids=["c1"])
    bundle = StoryBundle(story=story, cast=[Character(id="c1", name="Ana Reyes")])
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://example.com/v1", model="m")],
        active_provider_name="p",
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider([json.dumps(reply)]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


PRESENCE = "turn.remember.presence"


def test_a_review_may_change_the_storytellers_texts(tmp_path: Path):
    reply = {
        "summary": "Let people come.",
        "changes": [
            {"kind": "prompt", "target": PRESENCE, "value": "Let friends drop by.", "reason": "."}
        ],
    }
    session = review_session(tmp_path, reply)
    review = session.review_settings("Nobody ever visits.", replies=0, model="x")

    sent = session.provider.last_request.messages[1].text
    assert "prompt_texts:" in sent and f"## {PRESENCE}" in sent
    # Already in the tail the review reads: not sent twice.
    assert DEFAULT_TEXTS[PRESENCE] in sent
    assert sent.count(DEFAULT_TEXTS[PRESENCE]) == 1
    assert "## scene_read.system" not in sent

    [change] = review.changes
    assert change.before == DEFAULT_TEXTS[PRESENCE] and change.warning is None
    session.apply_review(review.changes)
    assert session.story.prompt_edits[PRESENCE].text == "Let friends drop by."
    assert session.undo_review()
    assert session.story.prompt_edits == {}


def test_the_side_calls_texts_only_when_the_author_includes_them(tmp_path: Path):
    reply = {"changes": [{"kind": "prompt", "target": "scene_read.system", "value": "Read."}]}
    session = review_session(tmp_path, reply)
    review = session.review_settings("x", replies=0, model="x")
    assert review.changes == [] and "wasn't shown" in review.cannot_fix[0]

    session.provider = MockChatProvider([json.dumps(reply)])
    review = session.review_settings("x", replies=0, model="x", side_texts=True)
    assert "## scene_read.system" in session.provider.last_request.messages[1].text
    assert [change.target for change in review.changes] == ["scene_read.system"]


def test_a_review_that_drops_a_placeholder_starts_with_a_warning(tmp_path: Path):
    reply = {"changes": [{"kind": "prompt", "target": "turn.directives.held", "value": "Hush."}]}
    review = review_session(tmp_path, reply).review_settings("x", replies=0, model="x")
    assert "{held}" in review.changes[0].warning


def test_a_review_can_put_a_default_back(tmp_path: Path):
    reply = {"changes": [{"kind": "prompt", "target": PRESENCE, "value": None}]}
    session = review_session(tmp_path, reply)
    set_text(session.story.prompt_edits, PRESENCE, "Mine.")
    review = session.review_settings("x", replies=0, model="x")
    [change] = review.changes
    assert change.before == "Mine." and change.value == DEFAULT_TEXTS[PRESENCE]
    session.apply_review(review.changes)
    assert session.story.prompt_edits == {}
