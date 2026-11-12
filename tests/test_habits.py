"""Habits of phrasing counted in code (engine/habits.py) and named on the
REMEMBER line, and the turn that leaves the next move to the storyteller
(engine/initiative.py)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sealedlore.engine.habits import echoes, feedback, found, habits_of, repeated_phrases
from sealedlore.engine.initiative import move_left
from sealedlore.engine.prompt import (
    SECTION_DIRECTIVES,
    SECTION_REMINDER,
    AssemblyOptions,
    TurnRequest,
    assemble_prompt,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS, PromptTexts
from sealedlore.models.node import DIRECTOR_SPEAKER_ID
from tests.conftest import make_exchange

WHO = "She looked at him with the stillness of someone who had stopped waiting."
PLAIN = "She set the cup down. “You're late,” she said, and went back to the ledger."
# Passages with no habit and nothing in common, as a story's are.
PLAINS = [
    "She set the cup down. “You're late,” she said.",
    "The gate stood open. Two goats had got into the turnips.",
    "“Bring the rope,” Marta called from the roof.",
    "Rain came in sideways off the lake before noon.",
    "He counted eleven coins onto the bar and pushed nine across.",
    "The dog would not cross the bridge, whatever Ben tried.",
    "“Wrong door,” the clerk said, without looking up.",
    "By the third bell the bread was gone and the queue had not moved.",
]


def test_a_passage_s_habits_are_found_by_family():
    assert "who" in found(WHO)
    assert "who" in found("He had the look of a man who counted exits.")
    assert "particular" in found("It had the particular weight of bad news.")
    assert "something" in found("Something shifted in her face.")
    assert "notbut" in found("It should have been an accusation. It wasn't.")
    assert "beat" in found("A beat. Then he laughed.")
    assert "opener" in found("Croom was quiet for a moment.\n\n“No,” he said.")
    assert "button" in found("“No,” he said, and left.\n\nThe word did a great deal of work.")
    assert found(PLAIN) == set()
    # A last paragraph that is someone speaking is the scene, not a flourish.
    assert "button" not in found("He left.\n\n“Well,” she said.")


def test_handing_the_author_s_words_back_is_an_echo():
    turn = "“I'll drink to a boring voyage,” I say."
    assert echoes("“Boring voyage,” he repeated. “Aye.”", turn)
    assert "echo" in found("“A boring voyage,” he said, tasting it.", turn)
    # A different line is not one, nor a single small word they both used.
    assert not echoes("“Aye,” he said. “To that.”", turn)
    assert not echoes("“To,” he said.", turn)
    # Nothing was said aloud, so nothing can be handed back.
    assert not echoes("“Boring,” he said.", "I think it is boring.")


def test_a_habit_is_named_when_half_the_recent_passages_share_it():
    recent = [("", WHO)] * 3 + [("", plain) for plain in PLAINS[:3]]
    assert habits_of(recent)[0] == ["who"]
    line = feedback(recent)
    assert line.startswith("Your recent passages have fallen into habits.")
    assert DEFAULT_TEXTS["habits.who"] in line
    # Two passages in six is not a habit, and three passages are too few to say.
    assert feedback([("", WHO)] * 2 + [("", plain) for plain in PLAINS[:4]]) == ""
    assert feedback([("", WHO)] * 3) == ""
    assert feedback([("", plain) for plain in PLAINS]) == ""


def test_only_the_latest_passages_count_and_at_most_four_habits_are_named():
    every = (
        "Croom was quiet for a moment, with the look of a man who knew. It had the particular "
        "weight of news. Something shifted in his face. Not anger, but worse.\n\nThat was all."
    )
    assert len(habits_of([("", every)] * 8)[0]) == 4
    # Eight plain passages since: the habit is behind it.
    assert feedback([("", WHO)] * 8 + [("", plain) for plain in PLAINS]) == ""


def test_phrases_repeated_across_passages_are_named_and_names_are_not():
    passages = [
        "He held the rail and watched the grey water slide past, and Captain Cull said so.",
        "She watched the grey water slide under the bow while Captain Cull said so again.",
        "They watched the grey water slide by, and Captain Cull said so a third time.",
        "Nothing of the kind here.",
    ]
    phrases = repeated_phrases(passages)
    # One phrase for the one habit ("watched the grey water" and "the grey
    # water slide" overlap), and never the captain's name.
    assert phrases == ["the grey water slide"]
    assert "the phrases “the grey water slide”" in feedback([("", p) for p in passages])
    # What the author's own turns are about is the scene, not a habit.
    assert repeated_phrases(passages, authors=["I watch the water from the rail."]) == []
    assert feedback([("I watch the water.", p) for p in passages]) == ""


def test_the_habit_texts_are_the_author_s_to_edit():
    recent = [("", WHO)] * 4
    texts = PromptTexts({"habits.who": "comparing people to types"})
    assert "comparing people to types" in feedback(recent, texts)
    for key in ("who", "particular", "something", "notbut", "beat", "opener", "echo", "button"):
        assert f"habits.{key}" in TEXTS


@pytest.mark.parametrize(
    ("text", "in_character", "move"),
    [
        ("I nod.", True, "idle"),
        ("Vane nodded slowly.", True, "idle"),
        ("I lean on the rail and watch the water.", True, "idle"),
        ("He said nothing, just sipped the coffee and waited.", True, "idle"),
        ("I keep to my berth.", True, "idle"),
        ("“Fine morning,” I say.", True, ""),
        ("Why?", True, ""),
        ("I put my shoulder to the door.", True, ""),
        (
            "I wait until the watch changes, then climb to the quarterdeck and ask the captain.",
            True,
            "",
        ),
        ("Three days pass at sea.", False, "skip"),
        ("The rest of the day passes.", False, "skip"),
        ("Later that night.", False, "skip"),
        (
            "The storm has passed, and the brig is now a wreck on the rocks: begin there, with the "
            "crew scattered along the beach and the captain missing since the mast came down.",
            False,
            "",
        ),
        ("Make the captain more suspicious.", False, ""),
        ("", True, ""),
    ],
)
def test_a_turn_that_leaves_the_next_move_to_the_storyteller(text, in_character, move):
    assert move_left(text, in_character=in_character) == move


# --- in the prompt -------------------------------------------------------------

ON = AssemblyOptions(
    min_cacheable_tokens=1,
    cache_exchanges_outside_prefix=1,
    prose_reminders=True,
    storyteller_moves=True,
)
OFF = replace(ON, prose_reminders=False, storyteller_moves=False)


def _turn(text: str, speaker: str = "char-serrik") -> TurnRequest:
    return TurnRequest(speaker_id=speaker, user_text=text, controlled_character_id="char-serrik")


def _reminder(story, cast, estimator, turn, options, history=()):
    prompt = assemble_prompt(
        story=story,
        cast=cast,
        estimator=estimator,
        turn=turn,
        history_nodes=list(history),
        options=options,
    )
    return prompt.section(SECTION_REMINDER).text, prompt.section(SECTION_DIRECTIVES).text


def test_switched_off_the_last_line_is_as_it_was(story, cast, estimator):
    line, _ = _reminder(story, cast, estimator, _turn("I nod."), OFF)
    assert DEFAULT_TEXTS["turn.remember.react"] in line
    assert "Prose:" not in line and "next move" not in line
    assert line.endswith(" Continue the scene now, in prose only.")


def test_the_prose_clause_follows_the_length_and_habits_follow_it(story, cast, estimator):
    history = make_exchange(6)
    for node in history:
        if node.kind == "assistant":
            node.content = WHO
    line, _ = _reminder(
        story, cast, estimator, _turn("I put my shoulder to the door."), ON, history
    )
    voice = DEFAULT_TEXTS["turn.remember.voice"]
    assert voice in line and DEFAULT_TEXTS["turn.remember.react"] in line
    assert line.index("Length:") < line.index(voice) < line.index("Your recent passages")
    assert line.endswith(" Continue the scene now, in prose only.")
    # With no habits there is nothing to name, and nothing is said.
    plain, _ = _reminder(story, cast, estimator, _turn("I put my shoulder to the door."), ON)
    assert voice in plain and "Your recent passages" not in plain


def test_a_waiting_turn_hands_the_move_over_with_room_for_it(story, cast, estimator):
    story.style.response_style = "adaptive"
    line, directives = _reminder(story, cast, estimator, _turn("I nod."), ON)
    assert "The author has left the next move to you" in line and "Serrik" in line
    assert DEFAULT_TEXTS["turn.remember.react"] not in line
    assert DEFAULT_TEXTS["length.your_move"] in line
    assert "for this passage only" in directives
    # A turn with something in it is answered as ever, at the story's length.
    line, directives = _reminder(story, cast, estimator, _turn("“Fine morning,” I say."), ON)
    assert DEFAULT_TEXTS["turn.remember.react"] in line
    assert (
        DEFAULT_TEXTS["length.your_move"] not in line and "for this passage only" not in directives
    )


def test_time_let_pass_lands_on_a_moment(story, cast, estimator):
    story.style.response_style = "adaptive"
    line, _ = _reminder(story, cast, estimator, _turn("Three days pass.", DIRECTOR_SPEAKER_ID), ON)
    assert "The author has let time pass" in line
    assert DEFAULT_TEXTS["length.time_passed"] in line


def test_the_move_keeps_a_length_the_author_or_the_story_chose(story, cast, estimator):
    story.style.response_style = "descriptive"
    line, directives = _reminder(story, cast, estimator, _turn("I nod."), ON)
    assert "next move" in line and DEFAULT_TEXTS["length.your_move"] not in line
    assert "for this passage only" not in directives
    story.style.response_style = "adaptive"
    brief = replace(_turn("I nod."), response_style="brief")
    line, _ = _reminder(story, cast, estimator, brief, ON)
    assert "next move" in line and DEFAULT_TEXTS["length.your_move"] not in line


def test_no_move_in_a_quiet_world_or_when_the_plot_begins_something(story, cast, estimator):
    story.defaults.world_activity = "quiet"
    line, _ = _reminder(story, cast, estimator, _turn("I nod."), ON)
    assert DEFAULT_TEXTS["turn.remember.react"] in line
    story.defaults.world_activity = "normal"
    directed = replace(
        _turn("I nod."), direction="# STORY DIRECTION\n\nBegin.", direction_begins=True
    )
    line, _ = _reminder(story, cast, estimator, directed, ON)
    assert DEFAULT_TEXTS["turn.remember.react"] in line
    # Nobody held: there is no one for a move to come to.
    nobody = TurnRequest(speaker_id=DIRECTOR_SPEAKER_ID, user_text="Three days pass.")
    line, _ = _reminder(story, cast, estimator, nobody, ON)
    assert "let time pass" not in line and DEFAULT_TEXTS["turn.remember.voice"] in line


def test_the_system_block_never_changes_with_any_of_it(story, cast, estimator):
    def system(turn, options):
        prompt = assemble_prompt(
            story=story,
            cast=cast,
            estimator=estimator,
            turn=turn,
            history_nodes=[],
            options=options,
        )
        return prompt.messages[0]

    assert system(_turn("I nod."), ON) == system(_turn("“Fine morning.”"), OFF)


# --- Settings and the session ---------------------------------------------------


def test_settings_keep_both_choices():
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import Config

    QApplication.instance() or QApplication([])
    config = Config()
    assert config.prose_reminders and config.storyteller_moves  # on unless turned off
    dialog = SettingsDialog(config, None)
    assert dialog.prose_reminders.isChecked() and dialog.storyteller_moves.isChecked()
    dialog.prose_reminders.setChecked(False)
    dialog.storyteller_moves.setChecked(False)
    dialog._save()
    assert not config.prose_reminders and not config.storyteller_moves
    dialog.deleteLater()


def test_a_session_s_turns_carry_them_as_settings_say(tmp_path, story, cast):
    from sealedlore.engine.session import StorySession
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import StoryBundle, save_story_bundle
    from tests.test_session import make_config

    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    session = StorySession(
        bundle, config, MockChatProvider([]), root=tmp_path, learn_corrections=False
    )
    turn = _turn("I nod.")
    line = session.assemble(turn).section(SECTION_REMINDER).text
    assert "next move" in line and DEFAULT_TEXTS["turn.remember.voice"] in line
    config.prose_reminders = config.storyteller_moves = False
    line = session.assemble(turn).section(SECTION_REMINDER).text
    assert "next move" not in line and "Prose:" not in line
    # A private scene's turns are assembled from the same settings.
    assert session.private_options().prose_reminders is False
