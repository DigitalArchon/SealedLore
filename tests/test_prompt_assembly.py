"""Prompt assembly: section order, cache breakpoints, budget. See §6.

The invariant that matters most here is that the system block is byte-identical
across turns — that is what makes the cache prefix survive, and it is why
nothing turn-specific may leak into it.
"""

from __future__ import annotations

from sealedlore.engine.prompt import (
    MAX_CACHE_BREAKPOINTS,
    SECTION_AUTHOR_TURN,
    SECTION_CAST,
    SECTION_DIRECTIVES,
    SECTION_ENGINE_RULES,
    SECTION_HISTORY,
    SECTION_LORE,
    SECTION_PERSPECTIVE,
    SECTION_QUESTION,
    SECTION_REMINDER,
    SECTION_SCENE,
    SECTION_STYLE,
    SECTION_SUMMARIES,
    SECTION_WORLD,
    SECTION_WORLD_ACTIVITY,
    AssemblyOptions,
    TurnRequest,
    assemble_prompt,
    assemble_question,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node, NodeMeta, Roll
from sealedlore.models.scene import OffstageCharacter, SceneLogEntry
from sealedlore.models.story import StyleDirectives
from sealedlore.models.summary import Summary
from sealedlore.tree import link_child
from tests.conftest import make_exchange

TURN = TurnRequest(
    speaker_id="char-serrik",
    user_text="I put my shoulder to the door.",
    controlled_character_id="char-serrik",
)

LOOSE = AssemblyOptions(min_cacheable_tokens=1, cache_exchanges_outside_prefix=1)


def long_exchange(count: int) -> list[Node]:
    """Exchanges long enough to clear a realistic minimum cacheable length."""
    nodes = make_exchange(count)
    for index, node in enumerate(nodes):
        node.content = f"Turn {index}. " + ("prose " * 100)
    return nodes


def assemble(story, cast, estimator, **kwargs):
    kwargs.setdefault("turn", TURN)
    kwargs.setdefault("history_nodes", [])
    kwargs.setdefault("options", LOOSE)
    return assemble_prompt(story=story, cast=cast, estimator=estimator, **kwargs)


# --- ordering -------------------------------------------------------------


def test_sections_run_from_stable_to_volatile(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=make_exchange(2),
        summaries=[Summary(content="Serrik reached the keep.")],
        lore=[LoreEntry(title="The Accord", content="Signed in blood.")],
    )
    assert [section.name for section in prompt.sections] == [
        SECTION_ENGINE_RULES,
        SECTION_STYLE,
        SECTION_PERSPECTIVE,
        SECTION_WORLD_ACTIVITY,
        SECTION_WORLD,
        SECTION_CAST,
        SECTION_SUMMARIES,
        SECTION_HISTORY,
        SECTION_LORE,
        SECTION_SCENE,
        SECTION_DIRECTIVES,
        SECTION_AUTHOR_TURN,
        SECTION_REMINDER,
    ]


def test_an_empty_style_still_carries_a_length_instruction(story, cast, estimator):
    """Human testing: with no length instruction at all, one line got 363 words."""
    story.style = StyleDirectives()
    prompt = assemble(story, cast, estimator)

    style = prompt.section(SECTION_STYLE)
    assert style is not None
    assert "Scale your passage to the author's turn" in style.text
    assert prompt.section(SECTION_ENGINE_RULES) is not None


# --- response length (§8.2) ------------------------------------------------


def test_the_story_default_sits_in_the_system_block_and_the_tail(story, cast, estimator):
    story.style = StyleDirectives(response_style="brief")
    prompt = assemble(story, cast, estimator)

    assert "One to three sentences" in prompt.section(SECTION_STYLE).text
    directives = prompt.section(SECTION_DIRECTIVES).text
    assert "Length: One to three sentences" in directives
    assert "for this passage only" not in directives


def test_the_reminder_ends_on_the_length(story, cast, estimator):
    """Recency is the lever: the length rides on the very last line."""
    story.style = StyleDirectives(response_style="brief")
    turn = TurnRequest(
        speaker_id="char-serrik", user_text="Onward.", controlled_character_id="char-serrik"
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    reminder = prompt.section(SECTION_REMINDER).text

    assert "Length: one to three sentences" in reminder
    assert reminder.endswith("Continue the scene now, in prose only.")


def test_the_reminder_carries_the_length_with_no_character_held(story, cast, estimator):
    story.style = StyleDirectives(response_style="standard")
    turn = TurnRequest(speaker_id="__narrator__", user_text="Dawn comes.")
    reminder = assemble(story, cast, estimator, turn=turn).section(SECTION_REMINDER).text

    assert "holds no character" in reminder
    assert "about one paragraph" in reminder


def test_a_per_turn_preset_is_an_override_in_the_tail_only(story, cast, estimator):
    story.style = StyleDirectives(response_style="adaptive")
    turn = TurnRequest(speaker_id="char-serrik", user_text="Onward.", response_style="literary")
    prompt = assemble(story, cast, estimator, turn=turn)

    directives = prompt.section(SECTION_DIRECTIVES).text
    assert "for this passage only, instead of the usual" in directives
    assert "300 to 600 words" in directives
    assert "300 to 600 words" in prompt.section(SECTION_REMINDER).text
    # The standing default is untouched.
    assert "300 to 600 words" not in prompt.messages[0].text
    assert "Scale your passage" in prompt.section(SECTION_STYLE).text


def test_choosing_the_default_again_is_not_an_override(story, cast, estimator):
    story.style = StyleDirectives(response_style="brief")
    turn = TurnRequest(speaker_id="char-serrik", user_text="Onward.", response_style="brief")
    directives = assemble(story, cast, estimator, turn=turn).section(SECTION_DIRECTIVES).text

    assert "for this passage only" not in directives


def test_a_per_turn_override_never_touches_the_system_block(story, cast, estimator):
    """The cache invariant, with length overrides in play."""
    story.style = StyleDirectives(response_style="adaptive")
    history = make_exchange(2)
    plain = assemble(story, cast, estimator, history_nodes=history)
    overridden = assemble(
        story,
        cast,
        estimator,
        history_nodes=history,
        turn=TurnRequest(speaker_id="char-serrik", user_text="x", response_style="literary"),
    )

    assert plain.messages[0].text == overridden.messages[0].text


def test_a_blank_custom_falls_back_rather_than_sending_nothing(story, cast, estimator):
    story.style = StyleDirectives(response_style="custom", length_target="   ")
    prompt = assemble(story, cast, estimator)

    assert "Scale your passage to the author's turn" in prompt.section(SECTION_STYLE).text


def test_custom_wording_is_used_verbatim(story, cast, estimator):
    story.style = StyleDirectives(response_style="custom", length_target="exactly two haiku")
    prompt = assemble(story, cast, estimator)

    assert "exactly two haiku." in prompt.section(SECTION_STYLE).text
    assert "Length: exactly two haiku." in prompt.section(SECTION_REMINDER).text


def test_a_detached_style_block_still_gets_length_in_the_tail(story, cast, estimator):
    """Hand-editing the style text mustn't switch length control off."""
    story.style = StyleDirectives(
        response_style="brief", detached=True, custom_text="Write like Le Guin."
    )
    prompt = assemble(story, cast, estimator)

    assert "Length: One to three sentences" in prompt.section(SECTION_DIRECTIVES).text
    assert "one to three sentences" in prompt.section(SECTION_REMINDER).text


def test_a_hand_edited_style_block_follows_the_length_setting(story, cast, estimator):
    """Detaching used to copy the length line in, and it stayed: change Length
    afterwards and the cached block said 150-300 words while the turn's
    directives said one to three sentences."""
    from sealedlore.engine.rules import render_style_block, without_length_line

    story.style = StyleDirectives(response_style="descriptive")
    story.style.custom_text = without_length_line(render_style_block(story.style)) + "\nLe Guin."
    story.style.detached = True
    story.style.response_style = "brief"
    style = assemble(story, cast, estimator).section(SECTION_STYLE).text
    assert "One to three sentences" in style and "150 to 300" not in style
    assert style.count("Length of your passages:") == 1 and style.endswith("Le Guin.")
    # One detached before the fix still carries its old line: it is dropped.
    story.style.custom_text = "Length of your passages: Two to four paragraphs.\nLe Guin."
    style = assemble(story, cast, estimator).section(SECTION_STYLE).text
    assert "Two to four" not in style and "One to three sentences" in style


def test_a_field_that_ends_a_sentence_gets_no_second_full_stop(story, cast, estimator):
    story.style = StyleDirectives(content_limits="No graphic gore.", pacing="brisk")
    style = assemble(story, cast, estimator).section(SECTION_STYLE).text
    assert style.endswith("Content limits: No graphic gore.") and ".." not in style
    assert "Pacing: brisk." in style


def test_system_block_is_identical_across_different_turns(story, cast, estimator):
    history = make_exchange(2)
    first = assemble(story, cast, estimator, history_nodes=history)

    story.scene.situation = "Everything has changed."
    story.scene.present_character_ids = ["char-maela"]
    # The scene read changes these every turn; they are tail material only.
    story.scene.present_others = ["Constable Hollis", "Gus"]
    story.scene.privacy = "private"
    story.scene.tracked = True
    story.scene.source = "story"
    other_turn = TurnRequest(
        speaker_id="char-maela",
        user_text="Entirely different text.",
        controlled_character_id="char-maela",
        agency_mode="dice",
        roll=Roll(die=100, value=7, band="critical_failure"),
        npc_scope="model_choice",
        length_hint="two paragraphs",
    )
    second = assemble(
        story,
        cast,
        estimator,
        turn=other_turn,
        history_nodes=[*history, *make_exchange(1)],
        on_file=[Character(name="Dr. Elena Ruiz")],
        scene_log=[
            SceneLogEntry(location="The radio room", gist="A quarrel.", closed_at_node_id="a0")
        ],
    )
    assert "Dr. Elena Ruiz" in second.section("tail.scene").text
    assert "A quarrel." in second.section("tail.scene_log").text

    assert [part.text for part in first.messages[0].parts] == [
        part.text for part in second.messages[0].parts
    ]


def test_nothing_turn_specific_reaches_the_system_block(story, cast, estimator):
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="A unique sentence: xyzzy-plugh.",
        controlled_character_id="char-serrik",
        ooc="A unique addendum: quuxly.",
        agency_mode="fiat",
        length_hint="exactly four paragraphs",
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    system_text = prompt.messages[0].text

    assert "xyzzy-plugh" not in system_text
    assert "quuxly" not in system_text
    assert "exactly four paragraphs" not in system_text
    assert "FIAT. What the author's character attempted" not in system_text


def test_lore_sits_in_the_tail_not_before_the_history(story, cast, estimator):
    lore = [LoreEntry(title="The Accord", content="Signed in blood at Thorn Gate.")]
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(2), lore=lore)

    assert "Signed in blood" not in prompt.messages[0].text
    assert "Signed in blood" in prompt.messages[-1].text


# --- the tail -------------------------------------------------------------


def test_tail_is_appended_to_the_final_user_message(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(2))
    final = prompt.messages[-1]

    assert final.role == "user"
    assert "THE AUTHOR'S TURN" in final.text
    assert "I put my shoulder to the door." in final.text
    assert all(message.role != "system" for message in prompt.messages[1:])


def test_tail_order_within_the_final_message(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        lore=[LoreEntry(title="The Accord", content="Signed in blood.")],
    )
    text = prompt.messages[-1].text
    positions = [
        text.index("# REFERENCE"),
        text.index("THIS SCENE"),
        text.index("TURN DIRECTIVES"),
        text.index("THE AUTHOR'S TURN"),
        text.index("REMEMBER:"),
    ]
    assert positions == sorted(positions)


def test_controlled_character_is_restated_last(story, cast, estimator):
    prompt = assemble(story, cast, estimator)
    reminder = prompt.section(SECTION_REMINDER)
    assert reminder is not None
    assert "Serrik Vaun belongs to the author" in reminder.text
    assert prompt.messages[-1].text.rstrip().endswith(reminder.text)


def test_person_and_tense_ride_the_reminder_with_who_you_are(story, cast, estimator):
    # Live on Sonnet 4.6, "Narrative person: second." in the style block got
    # 0/12 second-person passages; the same on the REMEMBER line got 15/15.
    story.style.person = "second"
    story.style.tense = "present"
    reminder = assemble(story, cast, estimator).section(SECTION_REMINDER).text

    assert reminder.startswith(
        'REMEMBER: Write in the second person and the present tense: Serrik Vaun is "you" '
        '("As you step inside, everyone turns to stare.").'
    )
    assert "Narrative person: second." in assemble(story, cast, estimator).messages[0].text


def test_the_reminder_names_whoever_is_held_this_turn(story, cast, estimator):
    story.style.person = "first"
    turn = TurnRequest(
        speaker_id="char-maela",
        user_text="I wait.",
        controlled_character_id="char-maela",
    )
    reminder = assemble(story, cast, estimator, turn=turn).section(SECTION_REMINDER).text
    assert reminder.startswith(
        'REMEMBER: Write in the first person and the past tense: Maela Orr is "I" '
        '("As I stepped inside, everyone turned to stare.").'
    )


def test_no_person_or_tense_set_adds_nothing(story, cast, estimator):
    story.style.person = None
    story.style.tense = None
    reminder = assemble(story, cast, estimator).section(SECTION_REMINDER).text
    assert reminder.startswith("REMEMBER: Serrik Vaun belongs to the author.")


def test_roster_separates_present_from_elsewhere(story, cast, estimator):
    # Once a passage has been written: before it, the opening places them.
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(1))
    scene = prompt.section(SECTION_SCENE)
    assert scene is not None

    present_block, _, elsewhere_block = scene.text.partition("NOT IN THE SCENE")
    assert "Serrik Vaun (held by the author this turn" in present_block
    assert "Maela Orr" in present_block
    assert "Captain Idris" in elsewhere_block
    assert "Captain Idris" not in present_block


def test_offstage_notes_are_rendered(story, cast, estimator):
    from sealedlore.models.scene import OffstageCharacter

    story.scene.offstage_but_nearby = [
        OffstageCharacter(character_id="char-idris", note="at Thorn Gate, three days north")
    ]
    prompt = assemble(story, cast, estimator)
    scene = prompt.section(SECTION_SCENE)
    assert scene is not None
    assert "Captain Idris — at Thorn Gate, three days north" in scene.text


def test_ooc_is_marked_as_authoritative(story, cast, estimator):
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="I listen at the door.",
        controlled_character_id="char-serrik",
        ooc="There is a giant elephant in the next room.",
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    author_turn = prompt.section(SECTION_AUTHOR_TURN)
    assert author_turn is not None
    assert "authoritative fact or instruction" in author_turn.text
    assert "giant elephant" in author_turn.text


def test_selected_npc_scope_names_only_those_characters(story, cast, estimator):
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="I wait.",
        controlled_character_id="char-serrik",
        npc_scope="selected",
        npc_scope_ids=("char-maela",),
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    directives = prompt.section(SECTION_DIRECTIVES)
    assert directives is not None
    assert "voice only: Maela Orr" in directives.text


def test_dice_outcome_is_handed_over_as_decided(story, cast, estimator):
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="I force the lock.",
        controlled_character_id="char-serrik",
        agency_mode="dice",
        roll=Roll(die=100, value=73, band="success_at_a_cost"),
        roll_explanation="Hard difficulty against expert competence.",
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    directives = prompt.section(SECTION_DIRECTIVES)
    assert directives is not None
    assert "SUCCESS AT A COST" in directives.text
    assert "Rolled 73 on a d100" in directives.text
    assert "do not re-adjudicate" in directives.text.lower()
    assert "Hard difficulty against expert competence." in directives.text


def test_length_hint_overrides_the_style_target(story, cast, estimator):
    story.style.length_target = "roughly 150-250 words"
    turn = TurnRequest(
        speaker_id="char-serrik", user_text="Onward.", length_hint="a single tight paragraph"
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    directives = prompt.section(SECTION_DIRECTIVES)
    assert directives is not None
    assert "a single tight paragraph" in directives.text
    assert "roughly 150-250 words" in prompt.section(SECTION_STYLE).text


def test_turn_without_a_controlled_character(story, cast, estimator):
    turn = TurnRequest(speaker_id="__director__", user_text="Skip ahead to dawn.")
    prompt = assemble(story, cast, estimator, turn=turn)
    tail = prompt.messages[-1].text
    assert "The author holds no character this turn" in tail
    assert "no character hears this" in tail


# --- history --------------------------------------------------------------


def test_consecutive_author_turns_are_merged_into_one_message(story, cast, estimator):
    first = Node(id="u0", kind="user", speaker_id="char-serrik", content="I step inside.")
    second = Node(id="u1", kind="user", speaker_id="__director__", content="Make it colder.")
    link_child(first, second)

    prompt = assemble(story, cast, estimator, history_nodes=[first, second])
    roles = [message.role for message in prompt.messages]
    assert roles == ["system", "user"]
    assert "I step inside." in prompt.messages[-1].text
    assert "Make it colder." in prompt.messages[-1].text


def test_history_alternates_and_ends_before_the_tail(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(2))
    assert [message.role for message in prompt.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]


def test_a_story_opening_with_an_assistant_turn_gets_a_leading_user_message(story, cast, estimator):
    opening = Node(id="a0", kind="assistant", speaker_id="__narrator__", content="Rain fell.")
    prompt = assemble(story, cast, estimator, history_nodes=[opening])

    assert prompt.messages[1].role == "user"
    assert prompt.messages[1].text == DEFAULT_TEXTS["storyteller.opening_turn"]
    assert prompt.messages[2].role == "assistant"


def test_reasoning_is_never_fed_back(story, cast, estimator):
    node = Node(
        id="a0",
        kind="assistant",
        speaker_id="__narrator__",
        content="The door opens.",
        meta=NodeMeta(reasoning="I considered a trap and rejected it."),
    )
    prompt = assemble(story, cast, estimator, history_nodes=[node])
    whole = "\n".join(message.text for message in prompt.messages)
    assert "considered a trap" not in whole
    assert "The door opens." in whole


def test_author_turns_carry_their_speaker_into_the_history(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(1))
    assert "[Serrik Vaun]" in prompt.messages[1].text


# --- budget ---------------------------------------------------------------


def test_budget_totals_match_the_sections(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=make_exchange(2),
        summaries=[Summary(content="Earlier events.")],
    )
    assert prompt.budget.total == sum(section.tokens for section in prompt.sections)
    assert prompt.budget.total == sum(prompt.budget.by_section.values())


def test_oldest_turns_are_excluded_when_the_budget_is_tight(story, cast, estimator):
    history = make_exchange(6)
    roomy = assemble(story, cast, estimator, history_nodes=history)
    fixed_cost = roomy.budget.total - roomy.section(SECTION_HISTORY).tokens

    options = AssemblyOptions(token_budget=fixed_cost + 40, min_cacheable_tokens=1)
    prompt = assemble(story, cast, estimator, history_nodes=history, options=options)

    assert prompt.needs_archival is True
    assert "u0" in prompt.excluded_node_ids
    assert "a0" in prompt.excluded_node_ids

    # The newest turns are the ones that survive, and none is cut mid-message.
    kept_text = prompt.section(SECTION_HISTORY).text
    assert "Author turn 5." in kept_text
    assert "Author turn 0." not in kept_text
    assert prompt.section(SECTION_HISTORY).tokens <= 40


def test_a_roomy_budget_excludes_nothing(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(3))
    assert prompt.excluded_node_ids == ()
    assert prompt.needs_archival is False


def test_cast_full_descriptions_are_dropped_when_the_cast_is_huge(story, estimator):
    big_cast = [
        Character(id=f"c{index}", name=f"Character {index}", full_description="x" * 4000)
        for index in range(4)
    ]
    options = AssemblyOptions(cast_token_cap=500, min_cacheable_tokens=1)
    prompt = assemble_prompt(
        story=story,
        cast=big_cast,
        history_nodes=[],
        turn=TurnRequest(speaker_id="c0", user_text="hello"),
        estimator=estimator,
        options=options,
    )
    assert prompt.include_cast_full_descriptions is False
    assert "xxxx" not in prompt.section(SECTION_CAST).text


# --- cache breakpoints ----------------------------------------------------


def test_breakpoints_mark_system_summaries_and_history(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=make_exchange(4),
        summaries=[Summary(content="Earlier events, at length." * 20)],
    )
    labels = [marker.label for marker in prompt.breakpoints]
    assert labels == ["system", "summaries", "history"]
    assert len(prompt.breakpoints) <= MAX_CACHE_BREAKPOINTS


def test_breakpoint_parts_are_flagged_on_the_messages(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(4))
    for marker in prompt.breakpoints:
        part = prompt.messages[marker.message_index].parts[marker.part_index]
        assert part.cache_breakpoint is True

    flagged = sum(
        1 for message in prompt.messages for part in message.parts if part.cache_breakpoint
    )
    assert flagged == len(prompt.breakpoints)


def test_system_breakpoint_sits_on_the_last_system_part_not_the_summaries(story, cast, estimator):
    prompt = assemble(story, cast, estimator, summaries=[Summary(content="Earlier events." * 50)])
    system_marker = next(m for m in prompt.breakpoints if m.label == "system")
    summaries_marker = next(m for m in prompt.breakpoints if m.label == "summaries")

    assert system_marker.message_index == 0
    assert summaries_marker.message_index == 0
    assert summaries_marker.part_index == len(prompt.messages[0].parts) - 1
    assert system_marker.part_index == summaries_marker.part_index - 1


def test_history_breakpoint_leaves_one_exchange_outside_the_prefix(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(3))
    marker = next(m for m in prompt.breakpoints if m.label == "history")

    # messages: 0 system, 1..6 history (u,a,u,a,u,a), 7 tail
    assert [message.role for message in prompt.messages][-1] == "user"
    assert marker.message_index == 4
    assert prompt.messages[marker.message_index].role == "assistant"
    assert "The world answers, turn 1." in prompt.messages[marker.message_index].text


def test_more_exchanges_outside_moves_the_breakpoint_earlier(story, cast, estimator):
    history = make_exchange(4)
    one = assemble(
        story,
        cast,
        estimator,
        history_nodes=history,
        options=AssemblyOptions(min_cacheable_tokens=1, cache_exchanges_outside_prefix=1),
    )
    two = assemble(
        story,
        cast,
        estimator,
        history_nodes=history,
        options=AssemblyOptions(min_cacheable_tokens=1, cache_exchanges_outside_prefix=2),
    )
    first = next(m for m in one.breakpoints if m.label == "history")
    second = next(m for m in two.breakpoints if m.label == "history")
    assert second.message_index == first.message_index - 2


def test_no_history_breakpoint_when_there_is_only_one_exchange(story, cast, estimator):
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(1))
    assert [m.label for m in prompt.breakpoints if m.label == "history"] == []


def test_short_segments_do_not_get_a_breakpoint(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=make_exchange(3),
        options=AssemblyOptions(min_cacheable_tokens=1_000_000),
    )
    assert prompt.breakpoints == ()


def test_skipped_segment_tokens_roll_into_the_next_breakpoint(story, cast, estimator):
    """A summaries block too short to cache should not waste a marker, and its
    tokens belong to whichever breakpoint does get placed after it."""
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=long_exchange(4),
        summaries=[Summary(content="Short.")],
        options=AssemblyOptions(min_cacheable_tokens=200, cache_exchanges_outside_prefix=1),
    )
    labels = [marker.label for marker in prompt.breakpoints]
    assert "summaries" not in labels
    history_marker = next(m for m in prompt.breakpoints if m.label == "history")
    system_marker = next(m for m in prompt.breakpoints if m.label == "system")
    assert history_marker.prefix_tokens > system_marker.prefix_tokens


def test_breakpoints_can_be_disabled(story, cast, estimator):
    prompt = assemble(
        story,
        cast,
        estimator,
        history_nodes=make_exchange(3),
        options=AssemblyOptions(min_cacheable_tokens=1, enable_cache_breakpoints=False),
    )
    assert prompt.breakpoints == ()
    assert not any(part.cache_breakpoint for m in prompt.messages for part in m.parts)


# --- presence is about the cast; reactions are never cut ---------------------


def test_the_roster_constrains_the_cast_not_the_world(story, cast, estimator):
    """Human testing: "only characters listed as present may appear" with one
    cast member present left the model working against a scene full of the
    world's own people. The roster says how people come to be here; whoever is
    here, listed or not, acts freely."""
    prompt = assemble(story, cast, estimator, history_nodes=make_exchange(1))
    scene = prompt.section(SECTION_SCENE).text
    reminder = prompt.section(SECTION_REMINDER).text
    rules = prompt.section(SECTION_ENGINE_RULES).text

    assert "IN THE SCENE (here as this passage opens)" in scene
    assert "NOT IN THE SCENE" in scene
    assert "show them arriving" in reminder
    assert "listed or not, named or not, acts freely" in " ".join(rules.split())


def test_the_people_not_here_are_listed_without_a_ban(story, cast, estimator):
    """Sept 26 2026: "until then they cannot see, hear, or answer" beside the
    names kept exactly those people away (Tess and Rosa never joined the author
    in a public place; Wade, with no line, did). The ban is stated once, in
    the rules and the REMEMBER line, never beside who may come."""
    for perspective in ("with_character", "whole_story"):
        story.style.perspective = perspective
        scene = (
            assemble(story, cast, estimator, history_nodes=make_exchange(1))
            .section(SECTION_SCENE)
            .text
        )
        elsewhere = scene.partition("NOT IN THE SCENE YET")[2]
        assert "Captain Idris" in elsewhere
        assert "cannot" not in elsewhere
        assert "may come in" in elsewhere


def test_a_new_place_comes_with_its_people(story, cast, estimator):
    """Arriving at Hellsville for the first time brought only Hale: the rules said
    "nobody is simply there" and nothing about the people a place has."""
    rules = " ".join(assemble(story, cast, estimator).section(SECTION_ENGINE_RULES).text.split())
    assert "goes somewhere new, the people who would be there are there" in rules
    assert "Several may come at once" in rules
    assert "shortcut into a scene already under way" in rules


def test_the_reminder_carries_both_halves_of_the_arrival_rule(story, cast, estimator):
    for perspective in ("with_character", "whole_story"):
        story.style.perspective = perspective
        reminder = assemble(story, cast, estimator).section(SECTION_REMINDER).text
        assert "Others may join the scene when they have a reason" in reminder
        assert "Nobody is suddenly just there mid-scene" in reminder
        assert "nobody overhears it from outside" in reminder


def test_the_reaction_to_the_authors_action_is_asked_for_last(story, cast, estimator):
    """Replayed live, a feat done in front of others went unwitnessed in 3 of 3 takes until the
    rules and the final line both asked for the reaction."""
    prompt = assemble(story, cast, estimator)

    assert "never cut for length" in prompt.section(SECTION_ENGINE_RULES).text
    assert "react to what they just did" in prompt.section(SECTION_REMINDER).text


# --- asides: questions out of character --------------------------------------


def ask(story, cast, estimator, history, **kwargs):
    return assemble_question(
        story=story,
        cast=cast,
        history_nodes=history,
        question="Why did nobody react?",
        estimator=estimator,
        options=AssemblyOptions(min_cacheable_tokens=1),
        **kwargs,
    )


def test_a_question_shares_the_story_turns_system_block(story, cast, estimator):
    history = make_exchange(2)
    turn = assemble(story, cast, estimator, history_nodes=history)
    question = ask(story, cast, estimator, history)

    assert [part.text for part in turn.messages[0].parts] == [
        part.text for part in question.messages[0].parts
    ]


def test_a_question_is_not_a_story_turn(story, cast, estimator):
    history = make_exchange(2)
    prompt = ask(story, cast, estimator, history)
    final = prompt.messages[-1]

    assert final.role == "user"
    assert "OUT OF CHARACTER" in final.text
    assert "do not continue the scene" in final.text
    assert "Why did nobody react?" in final.text
    # None of the story turn's machinery comes along.
    assert prompt.section(SECTION_DIRECTIVES) is None
    assert prompt.section(SECTION_REMINDER) is None
    # The latest passage is history, since nothing is being composed.
    assert history[-1].content in "\n".join(m.text for m in prompt.messages)


def test_a_follow_up_question_sees_the_earlier_answers(story, cast, estimator):
    prompt = ask(
        story, cast, estimator, make_exchange(1), earlier=[("Who is Nils?", "The quartermaster.")]
    )
    text = prompt.section(SECTION_QUESTION).text

    assert "The author asked: Who is Nils?" in text
    assert "You answered: The quartermaster." in text
    assert text.index("Who is Nils?") < text.index("Why did nobody react?")


# --- perspective --------------------------------------------------------------


def test_perspective_has_its_own_system_section(story, cast, estimator):
    whole = assemble(story, cast, estimator).section(SECTION_PERSPECTIVE).text
    story.style.perspective = "with_character"
    limited = assemble(story, cast, estimator).section(SECTION_PERSPECTIVE).text

    assert "FOLLOW THE WHOLE STORY" in whole and "cutaway" in whole
    assert "STAY WITH THE AUTHOR'S CHARACTER" in limited
    assert "Never cut away" in limited


def test_perspective_survives_a_hand_edited_style_block(story, cast, estimator):
    """Detaching replaces the style block wholesale; the presence rules must stay."""
    story.style.perspective = "with_character"
    story.style.detached = True
    story.style.custom_text = "Write like Le Guin."
    prompt = assemble(story, cast, estimator)

    assert "Le Guin" in prompt.section(SECTION_STYLE).text
    assert "Never cut away" in prompt.section(SECTION_PERSPECTIVE).text


def test_the_roster_and_reminder_follow_the_perspective(story, cast, estimator):
    history = make_exchange(1)
    story.style.perspective = "with_character"
    limited = assemble(story, cast, estimator, history_nodes=history)
    story.style.perspective = "whole_story"
    whole = assemble(story, cast, estimator, history_nodes=history)

    assert "brief cutaway" not in limited.section(SECTION_SCENE).text
    assert "Stay with Serrik Vaun" in limited.section(SECTION_REMINDER).text
    assert "brief cutaway" in whole.section(SECTION_SCENE).text
    assert "show them arriving" in whole.section(SECTION_REMINDER).text


def test_a_question_sees_the_perspective_too(story, cast, estimator):
    story.style.perspective = "with_character"
    prompt = ask(story, cast, estimator, make_exchange(1))

    scene = prompt.section(SECTION_SCENE).text
    assert "NOT IN THE SCENE" in scene
    assert "brief cutaway" not in scene


# --- agency (§4) -------------------------------------------------------------------


def test_fiat_forbids_an_added_catch(story, cast, estimator):
    prompt = assemble(
        story, cast, estimator, turn=TurnRequest(**{**TURN.__dict__, "agency_mode": "fiat"})
    )
    assert "add no cost, complication, or catch" in prompt.section(SECTION_DIRECTIVES).text
    assert "never\n  attach a catch" in prompt.section(SECTION_ENGINE_RULES).text


def test_dice_with_nothing_rolled_falls_back_to_judgement(story, cast, estimator):
    turn = TurnRequest(**{**TURN.__dict__, "agency_mode": "dice"})
    directives = assemble(story, cast, estimator, turn=turn).section(SECTION_DIRECTIVES).text
    assert "nothing was rolled this turn" in directives
    assert "already decided" not in directives


def test_a_roll_reaches_the_tail_as_an_outcome_not_a_question(story, cast, estimator):
    roll = Roll(die=100, value=38, band="success", target=45)
    turn = TurnRequest(
        **{**TURN.__dict__, "agency_mode": "dice", "roll": roll, "roll_explanation": "Why."}
    )
    prompt = assemble(story, cast, estimator, turn=turn)
    directives = prompt.section(SECTION_DIRECTIVES).text
    assert "Resolved outcome: SUCCESS" in directives and "Rolled 38 on a d100" in directives
    assert "Why." in directives
    assert "38" not in prompt.messages[0].text


def test_a_failed_roll_names_the_attempt_and_overrides_a_stated_result(story, cast, estimator):
    """Live: 'it fails' let the model narrate the attempt succeeding and fail
    something downstream instead, 3/3. Naming the attempt by its skill and
    saying the stated result doesn't happen got 4/4."""
    roll = Roll(die=100, value=99, band="critical_failure", target=50, domain="lockpicking")
    turn = TurnRequest(**{**TURN.__dict__, "agency_mode": "dice", "roll": roll})
    prompt = assemble(story, cast, estimator, turn=turn)
    directives = prompt.section(SECTION_DIRECTIVES).text
    reminder = prompt.section(SECTION_REMINDER).text

    assert "What Serrik Vaun attempted this turn using lockpicking does not work" in directives
    assert "that result does not happen" in directives
    assert "even for a moment" in directives
    assert (
        "Dice: CRITICAL FAILURE — what they attempted does not work, and makes things worse."
        in reminder
    )


def test_a_cost_must_be_shown(story, cast, estimator):
    roll = Roll(die=100, value=47, band="success_at_a_cost", target=50)
    turn = TurnRequest(**{**TURN.__dict__, "agency_mode": "dice", "roll": roll})
    directives = assemble(story, cast, estimator, turn=turn).section(SECTION_DIRECTIVES).text
    assert "costs them something real that this passage shows" in directives


def test_outside_fiat_a_stated_result_is_only_an_attempt(story, cast, estimator):
    rules = assemble(story, cast, estimator).section(SECTION_ENGINE_RULES).text
    assert "that is what they\ntried, not yet what happened" in rules


# --- who the model may voice ----------------------------------------------


def test_a_character_the_author_never_plays_is_handed_to_the_model(story, cast, estimator):
    """Human testing: "not available to play" reached no prompt, so the model,

    taught by a hundred turns of the author writing her, left her silent.
    """
    maela = next(character for character in cast if character.id == "char-maela")
    maela.is_player_available = False

    scene = assemble(story, cast, estimator).section(SECTION_SCENE)
    assert scene is not None
    present_block, _, _ = scene.text.partition("NOT IN THE SCENE")
    assert "Maela Orr (yours — write their words" in present_block
    # ...and that the history showing the author writing her doesn't count.
    assert "roster is current and replaces that" in scene.text


def test_an_unmarked_character_is_left_unannotated(story, cast, estimator):
    scene = assemble(story, cast, estimator).section(SECTION_SCENE)
    assert scene is not None
    assert "  - Maela Orr\n" in scene.text + "\n"
    assert "roster is current and replaces that" not in scene.text


def test_an_author_only_character_is_off_limits_even_when_not_held(story, cast, estimator):
    maela = next(character for character in cast if character.id == "char-maela")
    maela.author_only = True

    prompt = assemble(story, cast, estimator)
    scene = prompt.section(SECTION_SCENE)
    directives = prompt.section(SECTION_DIRECTIVES)
    reminder = prompt.section(SECTION_REMINDER)
    assert scene is not None and directives is not None and reminder is not None
    assert "Maela Orr (the author's, on every turn" in scene.text
    assert "Maela Orr is the author's on every turn" in directives.text
    assert "Maela Orr is the author's too" in reminder.text
    # The scope directive can't tell the model to voice everyone present.
    assert "except Serrik Vaun and Maela Orr" in directives.text


# --- the world's own momentum ---------------------------------------------


def test_the_world_block_says_the_world_does_not_wait(story, cast, estimator):
    """Human testing: nothing in the prompt said the world acts on its own, so

    the model wrote well-behaved reactions and the story stopped moving.
    """
    prompt = assemble(story, cast, estimator)
    world = prompt.section(SECTION_WORLD_ACTIVITY)
    assert world is not None
    assert "does not wait" in world.text
    # It is story-level and stable, so it belongs in the cached system block.
    assert prompt.messages[0].role == "system"
    assert world.text in prompt.messages[0].text


def test_each_world_activity_reads_differently(story, cast, estimator):
    texts = {}
    for level in ("quiet", "normal", "eventful"):
        story.defaults.world_activity = level
        texts[level] = assemble(story, cast, estimator).section(SECTION_WORLD_ACTIVITY).text
    assert len(set(texts.values())) == 3
    assert "rarely intrudes" in texts["quiet"]
    assert "presses in" in texts["eventful"]


# --- the scene stops claiming to be current -------------------------------


def test_the_scene_is_offered_as_the_authors_last_setting(story, cast, estimator):
    """A 216-turn story was still asserting its opening situation every turn."""
    scene = assemble(story, cast, estimator).section(SECTION_SCENE)
    assert scene is not None
    assert "Where the author last set the scene" in scene.text
    assert "the recent prose is what is true now" in scene.text


def test_a_present_character_is_not_also_listed_as_away(story, cast, estimator):
    """Live, a character was listed present and carrying "not reachable"."""
    story.scene.offstage_but_nearby = [
        OffstageCharacter(character_id="char-maela", note="still at the Northern Camp")
    ]
    scene = assemble(story, cast, estimator).section(SECTION_SCENE)
    assert scene is not None
    assert "still at the Northern Camp" not in scene.text


def test_an_absent_character_may_arrive(story, cast, estimator):
    scene = assemble(story, cast, estimator, history_nodes=make_exchange(1)).section(SECTION_SCENE)
    assert scene is not None
    assert "any of them may come in" in scene.text
    assert "show them coming" in scene.text


def test_the_length_for_a_turn_never_touches_the_system_block(story, cast, estimator):
    """The author asked (Sept 2026) that choosing a length not cost the cache:
    the story default is in the system block, a turn's own only in the tail."""
    from sealedlore.engine.response_style import PRESET_ORDER

    history = make_exchange(2)
    base = assemble(story, cast, estimator, history_nodes=history).messages[0]
    for style in PRESET_ORDER:
        turn = TurnRequest(
            speaker_id="char-serrik",
            user_text="Onward.",
            controlled_character_id="char-serrik",
            response_style=style,
        )
        prompt = assemble(story, cast, estimator, turn=turn, history_nodes=history)
        assert prompt.messages[0] == base, style
        if style != "custom" and style != story.style.response_style:
            assert DEFAULT_TEXTS[f"length.{style}.short"] in prompt.section(SECTION_REMINDER).text


def test_the_setting_and_the_worlds_momentum_have_distinct_headings(story, cast, estimator):
    story.world_bible = "A drowned city."
    system = assemble(story, cast, estimator).messages[0].text
    assert "# SETTING\n\nA drowned city." in system
    assert "# THE WORLD ACTS ON ITS OWN" in system
    assert "# WORLD\n" not in system and "# THE WORLD\n" not in system
    # The cast block says the supporting cards are the same kind of people.
    assert "The main cast. Other people the story has met" in system
