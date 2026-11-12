"""A plot written by the model (engine/plot_draft.py, StorySession.draft_plot):
the request, reading the reply into the story's own parts, putting it right
once, and leaving out what still can't be used. Never the network: the
scripted mock answers, and the story is the tests' own harbour town."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.plot_draft import (
    PlotWishes,
    build_plot_messages,
    drop_broken,
    merge_reply,
    plot_problems,
    read_reply,
    render_wishes,
    story_text,
    tidy_reply,
)
from sealedlore.engine.plot_file import render_plot_markdown
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PLOT_EXAMPLE, TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.story_plot import document_from_story, without_plot
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import KIND_LABELS
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.private import PrivateSpan
from sealedlore.models.story import Story, StorySetup
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.tree import link_child
from tests.conftest import make_exchange
from tests.test_session import make_config


def harbour() -> StoryBundle:
    """A smuggling harbour town, with the lore the prompt's example names."""
    story = Story(
        id="story-harbour",
        title="Salt and Tide",
        world_bible="A harbour town on a cold coast, where half the town smuggles.",
        setup=StorySetup(opening_text="Fog on the quays at dawn.", suggested_character_id="c-mara"),
    )
    story.setup.starting_scene.location = "The quays"
    story.setup.starting_scene.present_character_ids = ["c-mara"]
    bundle = StoryBundle(
        story=story,
        cast=[
            Character(id="c-mara", name="Mara Quill", summary="A boatwright with debts."),
            Character(id="c-oswin", name="Oswin Hale", is_player_available=False),
        ],
        lore=[
            LoreEntry(id="l-market", title="The Salt Market", content="Stalls under the cliff."),
            LoreEntry(id="l-guild", title="The Boatwrights' Guild", content="An old guild."),
        ],
    )
    return bundle


def run(generator):
    """A generator's notices, and what it returned."""
    notices = []
    try:
        while True:
            notices.append(next(generator))
    except StopIteration as stop:
        return stop.value, notices


def session_for(bundle: StoryBundle, root: Path, replies: list[str]) -> StorySession:
    save_story_bundle(bundle, root=root)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider(replies),
        root=root,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


# --- the prompt's own example ---------------------------------------------------------


def test_the_prompt_s_example_reads_cleanly_into_a_story_that_has_its_market():
    """The example is what the model copies: it must import with nothing to
    say, and name no secret before its time."""
    assert f"<example>\n{PLOT_EXAMPLE}\n</example>" in TEXTS["authoring.plot"].default
    base = without_plot(document_from_story(harbour()))
    merged, notes = merge_reply(base, read_reply(PLOT_EXAMPLE).document)
    assert notes == []
    assert plot_problems(merged) == []
    parsed = parse_plot_markdown(render_plot_markdown(merged).text)
    assert parsed.ok
    assert [p.message for p in parsed.problems] == []
    plot = parsed.scenario.plot
    assert [e.title for e in plot.events] == [
        "The customs captain arrives",
        "The raid",
        "A crate nobody claims",
        "Vask calls in the favour",
        "The chapel at low tide",
    ]
    # The story's own entry made one of the plot's places, beside a new one.
    assert [place.name for place in merged.places] == ["The Salt Market", "The Drowned Chapel"]
    assert merged.places[0].story_id == "l-market"
    assert [e.name for e in merged.lore] == ["The Boatwrights' Guild"]


def test_the_rules_are_in_the_system_prompt_and_the_story_in_the_request():
    bundle = harbour()
    shown = without_plot(document_from_story(bundle, for_model=True))
    text = story_text(shown)
    assert text.startswith("---\ntitle: Salt and Tide")
    assert "# World" in text and "## Mara Quill" in text and "## The Salt Market" in text
    assert "agency" not in text, "a plot needn't know the story's settings"
    wishes = PlotWishes(
        premise="A rival crew wants the same wreck.\n\nSomeone talks.",
        events="several",
        spacing="close",
        scale="personal",
        disruption="upending",
        kinds=("mystery", "relationships"),
        newcomers="none",
    )
    messages = build_plot_messages(text, wishes, plays="The author plays Mara Quill.")
    system, user = messages[0].text, messages[1].text
    assert system == DEFAULT_TEXTS["authoring.plot"]
    assert "Who reads each part" in system and "<example>" in system
    assert "<story>\n---\ntitle: Salt and Tide" in user
    assert "The author plays Mara Quill." in user
    for line in (
        "A rival crew wants the same wreck. Someone talks.",
        DEFAULT_TEXTS["authoring.plot.events.several"],
        DEFAULT_TEXTS["authoring.plot.spacing.close"],
        DEFAULT_TEXTS["authoring.plot.scale.personal"],
        DEFAULT_TEXTS["authoring.plot.disruption.upending"],
        "mysteries, secrets and clues, bonds, loyalties and rivalries",
        DEFAULT_TEXTS["authoring.plot.no_newcomers"],
    ):
        assert line in user, line
    assert DEFAULT_TEXTS["authoring.plot.auto"] not in user
    assert user.rstrip().endswith("Write the plot.")
    # Left to the model: its own words say so, and nothing else is asked.
    assert render_wishes(PlotWishes()) == f"- {DEFAULT_TEXTS['authoring.plot.auto']}"


def test_wishes_refuse_choices_there_are_no_words_for():
    with pytest.raises(ValueError):
        PlotWishes(events="dozens")
    with pytest.raises(ValueError):
        PlotWishes(kinds=("romance",))
    with pytest.raises(ValueError):
        PlotWishes(newcomers="crowds")
    with pytest.raises(ValueError):
        PlotWishes(depth="exhaustive")
    # The yes or no newcomers once was is refused, never read as "the model decides".
    for old in (False, True):
        with pytest.raises(ValueError):
            PlotWishes(newcomers=old)


def test_how_many_newcomers_and_how_fully_are_said_only_when_chosen():
    """The author (Nov 6 2026): a handful of people or lots of them, each with
    lots about them, for plots as for drafts. Nothing chosen asks for nothing:
    the request is as it was."""
    auto = f"- {DEFAULT_TEXTS['authoring.plot.auto']}"
    assert render_wishes(PlotWishes(newcomers="", depth="")) == auto
    for count in ("few", "several", "many"):
        line = DEFAULT_TEXTS[f"authoring.plot.newcomers.{count}"]
        assert render_wishes(PlotWishes(newcomers=count)) == f"{auto}\n- {line}"
    for depth in ("sketch", "deep"):
        line = DEFAULT_TEXTS[f"authoring.plot.depth.{depth}"]
        assert render_wishes(PlotWishes(depth=depth)) == f"{auto}\n- {line}"
    both = render_wishes(PlotWishes(newcomers="many", depth="deep", events="few")).splitlines()
    assert both == [
        auto,
        f"- {DEFAULT_TEXTS['authoring.plot.events.few']}",
        f"- {DEFAULT_TEXTS['authoring.plot.newcomers.many']}",
        f"- {DEFAULT_TEXTS['authoring.plot.depth.deep']}",
    ]
    # Nobody new: as the tick box's "no" was, and nothing about how fully.
    assert render_wishes(PlotWishes(newcomers="none", depth="deep")) == (
        f"{auto}\n- {DEFAULT_TEXTS['authoring.plot.no_newcomers']}"
    )


def test_a_reply_in_a_fence_with_bold_settings_is_read_as_written():
    reply = (
        "Here is your plot.\n\n```markdown\n# Facts\n\n- alarm: no — whether the town is roused\n\n"
        "# Events\n\n## **The bell**\n- **pacing**: any time\n- **trigger:** Someone rings it.\n\n"
        "The bell rings.\n```\n"
    )
    tidied = tidy_reply(reply)
    assert "```" not in tidied and "**" not in tidied
    document = read_reply(reply).document
    assert [e.title for e in document.events] == ["The bell"]
    assert document.events[0].pacing == "any" and document.events[0].trigger == "Someone rings it."
    # Bold in a list in the text is the text's own.
    assert tidy_reply("- **Three** crates went missing.") == "- **Three** crates went missing."


def test_the_story_s_own_cards_stand_and_newcomers_join_them():
    base = without_plot(document_from_story(harbour()))
    reply = read_reply(
        "# Characters\n\n## Mara Quill\n- role: storyteller\n\nRewritten by the model.\n\n"
        "## Vell\n- role: storyteller\n- present: yes\n- hidden: yes\n\nA rival.\n\n"
        "# Places\n\n## The Boatwrights' Guild\n\n"
        "## The Wreck\n- hidden: yes\n\nA hulk on the rocks.\n\n"
        "# Lore\n\n## The Salt Market\n\nWritten again.\n\n"
        "# Events\n\n## Vell comes\n- brings: Vell\n\nVell walks into the guild.\n"
    ).document
    merged, notes = merge_reply(base, reply)
    mara = next(c for c in merged.characters if c.name == "Mara Quill")
    assert mara.story_id == "c-mara" and "Rewritten" not in mara.description
    vell = next(c for c in merged.characters if c.name == "Vell")
    assert not vell.story_id and not vell.present, "the opening scene is the story's"
    assert [p.name for p in merged.places] == ["The Boatwrights' Guild", "The Wreck"]
    assert merged.places[0].story_id == "l-guild"
    market = next(e for e in merged.lore if e.name == "The Salt Market")
    assert market.story_id == "l-market" and "Written again" not in market.description
    assert notes == []
    # Nobody new wanted: the newcomers are left out, and said.
    merged, notes = merge_reply(base, reply, newcomers="none")
    assert "Vell" not in [c.name for c in merged.characters]
    assert "The Wreck" not in [p.name for p in merged.places]
    assert any("Vell" in note for note in notes) and any("The Wreck" in note for note in notes)
    # How many were asked for is the model's to keep to: a count leaves out
    # nobody, since a newcomer left out takes the events that bring them.
    for count in ("few", "several", "many"):
        merged, notes = merge_reply(base, reply, newcomers=count)
        assert "Vell" in [c.name for c in merged.characters] and notes == []
    with pytest.raises(ValueError):
        merge_reply(base, reply, newcomers=False)


def test_problems_are_the_importer_s_then_a_shared_name_and_a_secret_told_early():
    base = without_plot(document_from_story(harbour()))
    broken = read_reply("# Events\n\n## Odd\n- requires: tide = high\n\nSomething.\n").document
    merged, _ = merge_reply(base, broken)
    assert plot_problems(merged) == [
        "Event “Odd”: No fact called `tide` (the Facts section defines them)."
    ]
    reply = read_reply(
        "# Characters\n\n## Mara Vell\n- hidden: yes\n\nA cousin.\n\n"
        "## Dax\n- hidden: yes\n\nA spy.\n\n"
        "# Events\n\n## Cousin comes\n- brings: Mara Vell\n\nMara Vell arrives, with Dax.\n"
    ).document
    merged, _ = merge_reply(base, reply)
    problems = plot_problems(merged)
    assert any("Mara Vell" in p and "Mara Quill" in p and "“Mara”" in p for p in problems)
    assert any("names Dax" in p for p in problems)


def test_every_problem_goes_back_at_once(tmp_path: Path):
    """Live (GLM 5.3): sent back with its one importer error, a plot came
    back with that put right and seven secrets still named early, none of
    which it had been told of."""
    base = without_plot(document_from_story(harbour()))
    reply = read_reply(
        "# Places\n\n## The Calf\n- hidden: yes\n\nA tidal island.\n\n"
        "# Events\n\n## Low tide\n- lead-in: yes\n- reveals: The Calf\n\n"
        "The causeway to The Calf opens at low tide.\n\n"
        "## Odd\n- requires: tide = high\n\nNothing.\n"
    ).document
    merged, _ = merge_reply(base, reply)
    problems = plot_problems(merged)
    assert problems[0].startswith("Event “Odd”: No fact called `tide`")
    assert (
        "Event “Low tide”: its lead-in names The Calf, which is hidden and not certainly "
        "revealed by then." in problems
    )


def test_what_the_importer_refuses_is_left_out_with_what_needed_it():
    base = without_plot(document_from_story(harbour()))
    reply = read_reply(
        "# Facts\n\n- tide: high (high, low)\n\n"
        "# Events\n\n## Good\n\nThe tide turns.\n\n"
        "## Broken\n- requires: nowhere = yes\n\nNothing.\n\n"
        "## After\n- requires: Broken happened\n\nLater.\n"
    ).document
    merged, _ = merge_reply(base, reply)
    document, notes = drop_broken(merged)
    assert [e.title for e in document.events] == ["Good"]
    assert any("Broken" in note for note in notes) and any("After" in note for note in notes)
    assert document.facts[0].name == "tide"


# --- the session ------------------------------------------------------------------------


def test_the_session_writes_the_plot_logs_it_and_gives_it_to_the_story(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE])
    draft, notices = run(session.draft_plot(PlotWishes(events="few")))
    assert draft.notes == [] and not draft.repaired and draft.events == 5
    plot = session.story.plot
    vask = next(c for c in session.cast if c.name == "Teodor Vask")
    chapel = next(e for e in session.bundle.lore if e.title == "The Drowned Chapel")
    assert vask.plot_hidden and chapel.plot_hidden
    assert plot.events[0].brings == [vask.id]
    assert [(p.name, p.lore_id) for p in plot.places] == [
        ("The Salt Market", "l-market"),
        ("The Drowned Chapel", chapel.id),
    ]
    request = session.provider.requests[-1]
    assert request.model == session.authoring_model
    assert request.params.max_tokens == 32_000
    user = request.messages[-1].text
    assert (
        "The author plays Mara Quill." in user
        and DEFAULT_TEXTS["authoring.plot.events.few"] in user
    )
    log = read_api_log(session.story.id, root=tmp_path)
    kinds = [entry["kind"] for entry in log]
    assert kinds == ["plot_draft_request", "plot_draft_response"]
    assert log[0]["wishes"]["events"] == "few"
    assert "plot_draft_response" in KIND_LABELS, "its cost counts toward the story"
    assert session.unreported_usage
    # The document it returns is the story's, with the story's ids.
    assert next(c for c in draft.document.characters if c.name == "Teodor Vask").story_id


def test_nobody_new_leaves_the_plot_s_newcomers_out_and_logs_the_choice(tmp_path: Path):
    reply = (
        "# Characters\n\n## Vell\n- role: storyteller\n- hidden: yes\n\nA rival.\n\n"
        "# Facts\n\n- alarm: no — whether the harbour is roused; set by events\n\n"
        "# Events\n\n## The bell\n- pacing: any time\n- sets: alarm = yes\n"
        "- trigger: Someone rings the harbour bell.\n\nThe harbour bell rings in the fog.\n"
    )
    session = session_for(harbour(), tmp_path, [reply])
    draft, _ = run(session.draft_plot(PlotWishes(newcomers="none", events="few")))
    assert [e.title for e in session.story.plot.events] == ["The bell"]
    assert "Vell" not in [c.name for c in (*session.cast, *session.bundle.supporting.characters)]
    assert draft.notes == ["Left out Vell: nobody new was wanted."]
    user = session.provider.requests[0].messages[-1].text
    assert DEFAULT_TEXTS["authoring.plot.no_newcomers"] in user
    log = read_api_log(session.story.id, root=tmp_path)
    assert (log[0]["wishes"]["newcomers"], log[0]["wishes"]["depth"]) == ("none", "")


def test_a_long_reply_says_how_far_it_has_got_while_it_thinks_too(tmp_path: Path, monkeypatch):
    """Live (GLM 5.3), a first reply reasoned 20,000 tokens: five minutes in
    which nothing said it was working."""
    from sealedlore.engine import session_plot_draft

    monkeypatch.setattr(session_plot_draft, "PROGRESS_SECONDS", 0.0)
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE])
    session.provider.reasoning = "Weighing the harbour's people. " * 40
    _, notices = run(session.draft_plot(PlotWishes()))
    words = [notice.text for notice in notices]
    assert words[0].startswith("Thinking the plot through… about ")
    assert any(text.startswith("Writing the plot… about ") for text in words)


def test_a_reply_with_problems_goes_back_once_and_its_answer_is_used(tmp_path: Path):
    broken = PLOT_EXAMPLE.replace("requires: suspicion = calm", "requires: suspicion = sleepy")
    session = session_for(harbour(), tmp_path, [broken, PLOT_EXAMPLE])
    draft, notices = run(session.draft_plot(PlotWishes()))
    assert draft.repaired and draft.notes == []
    assert any("putting right" in notice.text for notice in notices)
    first, second = session.provider.requests
    assert [m.role for m in second.messages] == ["system", "user", "assistant", "user"]
    assert second.messages[2].text == broken
    assert "`suspicion` can't be “sleepy”" in second.messages[3].text
    assert [e["step"] for e in read_api_log(session.story.id, root=tmp_path)[::2]] == [
        "first",
        "repair",
    ]


def test_what_is_still_wrong_after_that_is_left_out_and_said(tmp_path: Path):
    broken = PLOT_EXAMPLE.replace("- requires: The raid happened", "- requires: tide = low")
    session = session_for(harbour(), tmp_path, [broken, broken])
    draft, _ = run(session.draft_plot(PlotWishes()))
    titles = [event.title for event in session.story.plot.events]
    assert titles == [
        "The customs captain arrives",
        "The raid",
        "A crate nobody claims",
        "Vask calls in the favour",
    ]
    assert any("Left out Event “The chapel at low tide”" in note for note in draft.notes)
    # Its hidden chapel stays a card no event reveals: the importer's note says so.
    assert "The Drowned Chapel" in [e.title for e in session.bundle.lore]


def test_a_broken_event_takes_what_needed_it_and_nothing_left_is_an_error(tmp_path: Path):
    # Without the example's one event that needs nothing else.
    start = PLOT_EXAMPLE.index("## A crate nobody claims")
    chained = PLOT_EXAMPLE[:start] + PLOT_EXAMPLE[PLOT_EXAMPLE.index("## Vask calls in") :]
    broken = chained.replace("requires: suspicion = calm", "requires: suspicion = sleepy")
    session = session_for(harbour(), tmp_path, [broken, broken])
    with pytest.raises(ValueError, match="no event"):
        run(session.draft_plot(PlotWishes()))
    assert session.story.plot is None


def test_a_reply_with_nothing_usable_leaves_the_story_as_it_was(tmp_path: Path):
    session = session_for(harbour(), tmp_path, ["I'm not sure what you mean.", "Still no."])
    with pytest.raises(ValueError, match="no event"):
        run(session.draft_plot(PlotWishes()))
    assert session.story.plot is None
    assert len(session.cast) == 2


def test_the_story_s_cards_are_never_the_model_s_to_change(tmp_path: Path):
    reply = (
        "# Characters\n\n## Mara Quill\n- role: supporting\n\nRewritten.\n\n"
        + PLOT_EXAMPLE.replace("# Characters\n\n", "", 1)
    )
    bundle = harbour()
    before = bundle.cast[0].model_dump()
    session = session_for(bundle, tmp_path, [reply])
    run(session.draft_plot(PlotWishes()))
    assert session.cast[0].model_dump() == before


def test_a_story_under_way_is_shown_where_it_stands_and_never_a_private_scene(tmp_path: Path):
    bundle = harbour()
    nodes = make_exchange(2, speaker_id="c-mara")
    secret = Node(
        id="p1",
        kind="assistant",
        speaker_id="__narrator__",
        content="SECRET-MARKER in the private scene.",
        meta=NodeMeta(private_span="span-1"),
    )
    link_child(nodes[-1], secret)
    nodes.append(secret)
    bundle.nodes = nodes
    bundle.story.active_leaf_id = "p1"
    bundle.story.held_character_id = "c-mara"
    bundle.story.private_spans = [
        PrivateSpan(
            id="span-1",
            start_parent_id=nodes[-2].id,
            model="private/test",
            keep="disk",
            status="closed",
        )
    ]
    session = session_for(bundle, tmp_path, [PLOT_EXAMPLE])
    run(session.draft_plot(PlotWishes()))
    user = session.provider.requests[-1].messages[-1].text
    assert "The story has begun" in user and "The world answers, turn 1." in user
    assert "SECRET-MARKER" not in json.dumps(session.provider.payloads)
    assert "Location:" in user


def test_a_chat_and_a_private_scene_have_no_plot_written(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE])
    session.story.mode = "chat"
    with pytest.raises(ValueError, match="simple chat"):
        run(session.draft_plot(PlotWishes()))
    assert session.provider.requests == []


def test_a_plot_with_nothing_for_the_first_hours_is_sent_back_for_it():
    """A player may play a whole evening before the clock passes its first
    day: a plot whose every event waits for a later day gave them nothing."""
    from sealedlore.engine.plot_draft import NOTHING_AT_FIRST, nothing_at_first
    from sealedlore.models.plot import Condition, EventDef, Plot

    later = EventDef(id="later", title="Later", kind="timed", window=(3, 5), text="Later.")
    chained = EventDef(
        id="after",
        title="After",
        kind="optional",
        text="After.",
        requires=[Condition(event_id="later")],
    )
    assert nothing_at_first(Plot(events=[later, chained])) == NOTHING_AT_FIRST.format(day=1)
    # An event with no days of its own, or one whose days begin where the story does.
    free = EventDef(id="free", title="Free", kind="optional", text="Free.")
    assert nothing_at_first(Plot(events=[later, free])) == ""
    first = EventDef(id="first", title="First", kind="timed", window=(1, 2), text="First.")
    assert nothing_at_first(Plot(events=[later, first])) == ""
    # A story that opens on its third day counts from there.
    assert nothing_at_first(Plot(start_minutes=2 * 1440 + 600, events=[later])) == ""
