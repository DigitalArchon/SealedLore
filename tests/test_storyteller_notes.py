"""The storyteller's notes: the truth behind what a story has not shown yet,
given to the storyteller alone.

They stand in the system block right after the setting, the same every turn,
and travel with the story's setup (scenario files, Restart, Duplicate
settings). What keeps them from every other model, and from the author's eyes
while a model wrote them, is in tests/test_storyteller_notes_secret.py.
"""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.prompt import (
    SECTION_CAST,
    SECTION_NOTES,
    SECTION_STANDING_LORE,
    SECTION_WORLD,
    AssemblyOptions,
    TurnRequest,
    as_shown,
    assemble_chat,
    assemble_prompt,
    assemble_question,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, STORYTELLER, TEXTS, PromptTexts
from sealedlore.engine.rules import notes_kept_line, render_storyteller_notes
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Roll
from sealedlore.models.story import Story
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import (
    SCENARIO_SUFFIX,
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from tests.conftest import make_exchange

NOTES = (
    "Under the false floor of the Hellsville granary is the town's missing seed corn.\n"
    "Marcus hid it there; he wants to leave before the thaw and hides that he can't read a map."
)
TURN = TurnRequest(
    speaker_id="char-serrik",
    user_text="I put my shoulder to the door.",
    controlled_character_id="char-serrik",
)
LOOSE = AssemblyOptions(min_cacheable_tokens=1, cache_exchanges_outside_prefix=1)

# The wording measured live; a change to it wants a measurement of its own.
MEASURED = """\
# WHAT ONLY YOU KNOW

The truth behind what the story has not yet shown, and what the people in it want and hide. \
The author has not been told any of it, and neither has their character.

- Hold to it. When the author's character finds something out, by looking, by asking the \
right person or by being there, what they find is what is written here. Never put something \
else in its place.
- People act on what they want and what they hide. Let it show in what they do and what they \
avoid; the narration never states it.
- Give it up as the story earns it. Nobody volunteers a secret unprompted, and nothing here \
is kept back once the author's character has fairly got to it.

{notes}"""


def assemble(story, cast, estimator, **kwargs):
    kwargs.setdefault("turn", TURN)
    kwargs.setdefault("history_nodes", [])
    kwargs.setdefault("options", LOOSE)
    return assemble_prompt(story=story, cast=cast, estimator=estimator, **kwargs)


def system_of(prompt) -> str:
    return next(message.text for message in prompt.messages if message.role == "system")


# --- the text ----------------------------------------------------------------------


def test_the_notes_text_is_the_measured_wording_among_the_standing_rules():
    text = TEXTS["storyteller.notes"]
    assert text.default == MEASURED
    assert text.group == STORYTELLER
    assert text.slots == ("notes",)


def test_the_notes_are_rendered_under_their_rules_and_blank_ones_not_at_all():
    rendered = render_storyteller_notes(f"  {NOTES}\n\n")
    assert rendered == MEASURED.replace("{notes}", NOTES)
    assert render_storyteller_notes(None) == ""
    assert render_storyteller_notes(" \n ") == ""
    # An edit to the text is what is sent, as with every other prompt text.
    edited = PromptTexts({"storyteller.notes": "# SECRETS\n\n{notes}"})
    assert render_storyteller_notes(NOTES, edited) == f"# SECRETS\n\n{NOTES}"


def test_the_kept_line_says_how_many_words_and_none_of_them():
    assert notes_kept_line("one two three") == (
        "(the storyteller's notes: 3 words, kept from the author)"
    )
    assert notes_kept_line("alone") == "(the storyteller's notes: 1 word, kept from the author)"
    kept = render_storyteller_notes(NOTES, kept=True)
    assert kept.startswith("# WHAT ONLY YOU KNOW") and kept.endswith(notes_kept_line(NOTES))
    assert "granary" not in kept and "Marcus" not in kept


# --- the prompt --------------------------------------------------------------------


def test_the_notes_stand_in_the_system_block_right_after_the_setting(story, cast, estimator):
    story.storyteller_notes = NOTES
    prompt = assemble(
        story,
        cast,
        estimator,
        standing_lore=[LoreEntry(title="The Accord", content="Signed in blood.")],
    )
    names = [section.name for section in prompt.sections]
    at = names.index(SECTION_NOTES)
    assert names[at - 1] == SECTION_WORLD
    assert names[at + 1 : at + 3] == [SECTION_STANDING_LORE, SECTION_CAST]
    assert prompt.section(SECTION_NOTES).text == MEASURED.replace("{notes}", NOTES)
    system = system_of(prompt)
    assert system.index("# SETTING") < system.index("# WHAT ONLY YOU KNOW") < system.index("# LORE")
    assert system.index("# LORE") < system.index("# CAST")
    # Nothing of it in the tail: the last message is the turn's alone.
    assert "granary" not in prompt.messages[-1].text


def test_a_story_without_notes_has_no_such_section(story, cast, estimator):
    before = assemble(story, cast, estimator)
    assert before.section(SECTION_NOTES) is None
    assert "WHAT ONLY YOU KNOW" not in system_of(before)
    story.storyteller_notes = "   "
    assert system_of(assemble(story, cast, estimator)) == system_of(before)


def test_the_notes_never_change_the_system_block_between_turns(story, cast, estimator):
    story.storyteller_notes = NOTES
    history = make_exchange(2)
    first = assemble(story, cast, estimator, history_nodes=history)
    story.scene.situation = "Everything has changed."
    story.scene.present_character_ids = ["char-maela"]
    story.scene.privacy = "private"
    other = TurnRequest(
        speaker_id="char-maela",
        user_text="I lift the false floor of the granary.",
        controlled_character_id="char-maela",
        agency_mode="dice",
        roll=Roll(die=100, value=7, band="critical_failure"),
        length_hint="two paragraphs",
    )
    second = assemble(
        story, cast, estimator, turn=other, history_nodes=[*history, *make_exchange(1)]
    )
    assert first.messages[0] == second.messages[0]
    assert "granary" in system_of(first)


def test_a_question_rides_the_same_system_block(story, cast, estimator):
    story.storyteller_notes = NOTES
    turn = assemble(story, cast, estimator, history_nodes=make_exchange(1))
    question = assemble_question(
        story=story,
        cast=cast,
        history_nodes=make_exchange(1),
        question="Why did Marcus look away?",
        estimator=estimator,
        options=LOOSE,
    )
    assert system_of(question) == system_of(turn)
    assert question.section(SECTION_NOTES) is not None


def test_a_private_scenes_rules_replace_only_the_rules(story, cast, estimator):
    story.storyteller_notes = NOTES
    private = AssemblyOptions(
        min_cacheable_tokens=1, rules_override=(("system.private_rules", "Write quietly."),)
    )
    prompt = assemble(story, cast, estimator, options=private)
    names = [section.name for section in prompt.sections]
    assert names[:3] == ["system.private_rules", SECTION_WORLD, SECTION_NOTES]
    assert "granary" in system_of(prompt)


def test_a_simple_chat_is_never_sent_notes(estimator):
    chat = Story(title="A chat", mode="chat", chat_prompt="Be brief.", storyteller_notes=NOTES)
    prompt = assemble_chat(
        story=chat, history_nodes=[], tail="Hello.", estimator=estimator, options=LOOSE
    )
    sent = "\n".join(message.text for message in prompt.messages)
    assert "granary" not in sent and "WHAT ONLY YOU KNOW" not in sent


def test_the_notes_count_toward_the_budget(story, cast, estimator):
    without = assemble(story, cast, estimator)
    story.storyteller_notes = NOTES
    with_notes = assemble(story, cast, estimator)
    section = with_notes.section(SECTION_NOTES)
    assert section.tokens > 0
    assert with_notes.budget.by_section["system"] == (
        without.budget.by_section["system"] + section.tokens
    )


# --- the prompt as it is shown -----------------------------------------------------


def test_shown_the_notes_stand_as_a_line_and_everything_else_is_as_sent(story, cast, estimator):
    story.storyteller_notes = NOTES
    sent = assemble(story, cast, estimator, history_nodes=make_exchange(2))
    shown = as_shown(sent)
    assert "granary" in system_of(sent)
    for text in (system_of(shown), shown.section(SECTION_NOTES).text):
        assert "granary" not in text and "Marcus" not in text
        assert notes_kept_line(NOTES) in text and "# WHAT ONLY YOU KNOW" in text
    # Only that part differs: every other part, message, mark and count stays.
    assert [len(m.parts) for m in shown.messages] == [len(m.parts) for m in sent.messages]
    differing = [
        (a.text, b.text)
        for sent_message, shown_message in zip(sent.messages, shown.messages, strict=True)
        for a, b in zip(sent_message.parts, shown_message.parts, strict=True)
        if a != b
    ]
    assert differing == [(sent.section(SECTION_NOTES).text, shown.section(SECTION_NOTES).text)]
    assert shown.breakpoints == sent.breakpoints and shown.budget == sent.budget
    assert [s.tokens for s in shown.sections] == [s.tokens for s in sent.sections]
    assert as_shown(shown) == shown


def test_a_prompt_with_no_notes_is_shown_as_it_is(story, cast, estimator):
    prompt = assemble(story, cast, estimator)
    assert as_shown(prompt) is prompt


def test_the_default_texts_are_what_an_unedited_story_sends(story, cast, estimator):
    story.storyteller_notes = NOTES
    prompt = assemble(story, cast, estimator)
    filled = DEFAULT_TEXTS.fill("storyteller.notes", notes=NOTES)
    assert prompt.section(SECTION_NOTES).text == filled


# --- travelling with the setup -----------------------------------------------------


def bundle_with_notes(story, cast) -> StoryBundle:
    story.storyteller_notes = NOTES
    story.storyteller_notes_by = "model"
    return StoryBundle(story=story, cast=cast)


def test_a_scenario_file_carries_the_notes_and_who_wrote_them(story, cast, tmp_path: Path):
    bundle = bundle_with_notes(story, cast)
    path = tmp_path / f"story{SCENARIO_SUFFIX}"
    write_scenario(path, scenario_from_bundle(bundle))
    made = bundle_from_scenario(read_scenario(path))
    assert made.story.storyteller_notes == NOTES
    assert made.story.storyteller_notes_by == "model"


def test_restart_and_duplicate_settings_keep_the_notes(story, cast):
    fresh = fresh_playthrough(bundle_with_notes(story, cast), "Again")
    assert fresh.story.id != story.id
    assert fresh.story.storyteller_notes == NOTES
    assert fresh.story.storyteller_notes_by == "model"


def test_a_saved_story_keeps_the_notes_and_an_old_one_loads_without(story, cast, tmp_path: Path):
    bundle = bundle_with_notes(story, cast)
    save_story_bundle(bundle, root=tmp_path)
    loaded = load_story_bundle(story.id, root=tmp_path).story
    assert (loaded.storyteller_notes, loaded.storyteller_notes_by) == (NOTES, "model")
    # A story.json, or a scenario, from before there were notes.
    old = Story.model_validate({"title": "Old"})
    assert (old.storyteller_notes, old.storyteller_notes_by) == (None, "author")
    scenario = scenario_from_bundle(StoryBundle(story=old)).model_dump()
    del scenario["storyteller_notes"], scenario["storyteller_notes_by"]
    made = bundle_from_scenario(type(scenario_from_bundle(bundle)).model_validate(scenario))
    assert (made.story.storyteller_notes, made.story.storyteller_notes_by) == (None, "author")


def test_a_story_backup_carries_the_notes(story, cast, tmp_path: Path):
    from sealedlore.storage.archive import read_archive, write_archive

    path = tmp_path / "story.sealedlore-archive.json"
    write_archive(path, bundle_with_notes(story, cast), [])
    restored, _log = read_archive(path)
    assert restored.story.storyteller_notes == NOTES
    assert restored.story.storyteller_notes_by == "model"
