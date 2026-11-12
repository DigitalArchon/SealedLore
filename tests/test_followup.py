"""Follow-ups (engine/followup.py, engine/session_followup.py): the author
asking for changes to a story or a plot set up for them, and undoing them.
The author (Nov 6 2026): "I like the world, but I'd prefer the characters
were more badass", "the world isn't hostile enough", and undo. Never the
network: the scripted mock answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.followup import STORY, FollowUpTurn, render_earlier, setup_fingerprint
from sealedlore.engine.plot_draft import PlotWishes, merge_reply, plot_reply_text, read_reply
from sealedlore.engine.prompt_texts import PLOT_EXAMPLE
from sealedlore.engine.story_plot import document_from_story, without_plot
from sealedlore.storage.repository import load_story_bundle, read_api_log
from tests.conftest import make_exchange
from tests.test_plot_draft import harbour, run, session_for


def changes(*items: dict, summary: str = "Done.", cannot_fix: tuple[str, ...] = ()) -> str:
    return json.dumps({"summary": summary, "changes": list(items), "cannot_fix": list(cannot_fix)})


def state(session) -> dict:
    """Everything a follow-up can change, to compare before and after."""
    bundle = session.bundle
    return {
        "story": bundle.story.model_dump(
            include={
                "title",
                "world_bible",
                "setup",
                "defaults",
                "style",
                "picture_style",
                "image_style",
                "plot",
                "scene",
            }
        ),
        "cast": [c.model_dump() for c in bundle.cast],
        "supporting": [c.model_dump() for c in bundle.supporting.characters],
        "lore": [e.model_dump() for e in bundle.lore],
    }


HARSHER = changes(
    {"kind": "world", "value": "A harbour town where the sea itself is the enemy."},
    {
        "kind": "character",
        "target": "Mara Quill",
        "field": "description",
        "value": "Scarred, fearless, a knife in each boot.",
    },
    {"kind": "character_remove", "target": "Oswin Hale"},
    {"kind": "scene", "field": "location", "value": "The docks at night"},
    {"kind": "story", "field": "picture_style", "value": "comic book"},
    {"kind": "lore_remove", "target": "The Boatwrights' Guild"},
    summary="A crueller sea and a harder Mara; Oswin is gone.",
)


def test_a_story_s_follow_up_changes_what_it_asks_and_undo_puts_it_back(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [HARSHER])
    kept = state(session)
    turn, notices = run(session.follow_up_story("Harsher, please, and lose Oswin."))
    assert turn.summary == "A crueller sea and a harder Mara; Oswin is gone."
    assert turn.changed == [
        "World",
        "Mara Quill › description",
        "Starting scene › location",
        "Story › picture style",
        "Oswin Hale › out of the story",
        "Lore › The Boatwrights' Guild › out of the story",
    ]
    assert turn.refused == []
    story = session.story
    assert story.world_bible == "A harbour town where the sea itself is the enemy."
    assert session.bundle.cast[0].full_description == "Scarred, fearless, a knife in each boot."
    assert [c.name for c in session.bundle.cast] == ["Mara Quill"]
    assert story.setup.starting_scene.location == "The docks at night"
    assert story.scene.location == "The docks at night", "the live scene follows, not begun"
    assert story.picture_style == "comic"
    assert [e.title for e in session.bundle.lore] == ["The Salt Market"]
    saved = load_story_bundle(story.id, root=tmp_path)
    assert saved.story.world_bible == story.world_bible

    assert session.follow_up_history(STORY) == [turn]
    assert session.follow_up_to_undo(STORY) is turn
    assert not session.follow_up_changed_since()
    assert session.undo_follow_up(STORY)
    assert state(session) == kept
    assert turn.undone and session.follow_up_to_undo(STORY) is None
    assert (
        load_story_bundle(story.id, root=tmp_path).story.world_bible == kept["story"]["world_bible"]
    )


def test_the_request_holds_the_setup_and_what_was_asked_before(tmp_path: Path):
    session = session_for(
        harbour(),
        tmp_path,
        [
            changes({"kind": "world", "value": "Colder."}, summary="The world is colder."),
            changes({"kind": "world", "value": "Colder still."}),
        ],
    )
    run(session.follow_up_story("Make it colder."))
    run(session.follow_up_story("Colder again."))
    system, user = session.provider.requests[-1].messages
    asked = user.parts[0].text
    assert '"title": "Salt and Tide"' in asked and '"world": "Colder."' in asked
    assert "“Make it colder.”: The world is colder." in asked
    assert asked.rstrip().endswith("The author asks: Colder again.")
    assert "character_remove" in system.parts[0].text
    requests = [
        e for e in read_api_log(session.story.id, root=tmp_path) if e["kind"] == "followup_request"
    ]
    assert [e["request"] for e in requests] == ["Make it colder.", "Colder again."]


def test_what_can_t_be_done_is_said_and_the_rest_is_done(tmp_path: Path):
    reply = changes(
        {"kind": "character", "target": "Nobody", "field": "voice", "value": "Gruff."},
        {"kind": "model", "field": "main_model", "value": "some/model"},
        {"kind": "world", "value": "New."},
        cannot_fix=["Dragons need a world with dragons in it."],
    )
    session = session_for(harbour(), tmp_path, [reply])
    turn, _ = run(session.follow_up_story("Dragons, and a gruff voice for Nobody."))
    assert turn.changed == ["World"]
    assert turn.refused[0] == "Dragons need a world with dragons in it."
    assert any("Nobody" in line for line in turn.refused)
    assert any("can't be changed here" in line for line in turn.refused)


def test_nothing_changed_is_nothing_to_undo(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [changes(cannot_fix=["Not with settings."])])
    turn, _ = run(session.follow_up_story("Make it rain."))
    assert turn.changed == [] and session.follow_up_to_undo(STORY) is None


def test_a_follow_up_is_for_a_story_not_begun_and_never_a_chat(tmp_path: Path):
    bundle = harbour()
    bundle.nodes = make_exchange(1, speaker_id="c-mara")
    bundle.story.active_leaf_id = "a0"
    session = session_for(bundle, tmp_path, [HARSHER])
    with pytest.raises(ValueError, match="has begun"):
        run(session.follow_up_story("Harsher."))
    chat = harbour()
    chat.story.mode = "chat"
    session = session_for(chat, tmp_path / "chat", [HARSHER])
    with pytest.raises(ValueError, match="simple chat"):
        run(session.follow_up_story("Harsher."))


def test_undo_takes_back_only_the_newest_and_knows_a_change_since(tmp_path: Path):
    session = session_for(
        harbour(),
        tmp_path,
        [
            changes({"kind": "world", "value": "First."}),
            changes({"kind": "world", "value": "Second."}),
        ],
    )
    first, _ = run(session.follow_up_story("One."))
    second, _ = run(session.follow_up_story("Two."))
    assert session.follow_up_to_undo("plot") is None and not session.undo_follow_up("plot")
    assert session.follow_up_to_undo(STORY) is second
    session.story.setup.description = "Changed by hand in Setup."
    assert session.follow_up_changed_since()
    assert session.undo_follow_up(STORY)
    assert session.story.world_bible == "First."
    assert session.follow_up_to_undo(STORY) is first
    assert "“One.”" in render_earlier(session.follow_up_history(STORY))
    assert "“Two.”" not in render_earlier(session.follow_up_history(STORY)), "undone"


def test_who_is_in_the_starting_scene_and_supporting_names_there(tmp_path: Path):
    bundle = harbour()
    bundle.supporting.characters.append(
        bundle.cast[1].model_copy(update={"id": "c-nell", "name": "Nell Dray"})
    )
    reply = changes({"kind": "scene", "field": "present", "value": ["Oswin Hale", "Nell Dray"]})
    session = session_for(bundle, tmp_path, [reply])
    turn, _ = run(session.follow_up_story("Oswin and Nell are there too."))
    scene = session.story.setup.starting_scene
    assert scene.present_character_ids == ["c-mara", "c-oswin"], "the author's own stays"
    assert scene.present_others == ["Nell Dray"]
    assert turn.changed == ["Starting scene › present"]


def test_a_card_or_a_place_the_plot_uses_can_t_be_taken_out(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE])
    run(session.draft_plot(PlotWishes()))
    session.provider.responses.append(
        changes(
            {"kind": "character_remove", "target": "Teodor Vask"},
            {"kind": "lore_remove", "target": "The Salt Market"},
        )
    )
    turn, _ = run(session.follow_up_story("Lose Vask and the market."))
    assert turn.changed == []
    assert any("change the plot first" in line for line in turn.refused)


def test_the_cast_boards_remove_keeps_a_card_the_plot_brings_in(tmp_path: Path):
    """The board's Remove (Setup and the guide too) goes through
    `remove_character`, which took Vask out though the plot's events bring
    him in: the follow-up's removal had always refused that."""
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE])
    run(session.draft_plot(PlotWishes()))
    vask = next(c for c in (*session.cast, *session.supporting) if c.name == "Teodor Vask")
    with pytest.raises(ValueError, match="change the plot first"):
        session.remove_character(vask.id)
    assert any(c.id == vask.id for c in (*session.cast, *session.supporting))


# --- the plot ----------------------------------------------------------------------------

CRUELLER = PLOT_EXAMPLE.replace(
    "Patient, polite and very\nhard to lie to; he listens more than he talks.",
    "Cold, cruel and very\nhard to lie to; he enjoys a confession.",
)
WITHOUT_VASK = PLOT_EXAMPLE[PLOT_EXAMPLE.index("# Places") :].split("# Timeline")[0] + (
    "# Events\n\n"
    "## The chapel at low tide\n"
    "- pacing: any time\n"
    "- reveals: The Drowned Chapel\n"
    "- trigger: The player's character goes down to the shore below the cliffs at low tide.\n"
    "- tell: The shore at low tide shows them the way into the sea cave below the cliffs.\n"
)


def test_the_plot_as_it_is_is_the_model_s_last_reply(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE, CRUELLER])
    run(session.draft_plot(PlotWishes()))
    previous = plot_reply_text(document_from_story(session.bundle))
    assert previous.startswith("# Characters\n\n## Teodor Vask")
    assert "## The Salt Market\n\n## The Drowned Chapel" in previous, "the story's own: a title"
    assert "Stalls under the cliff" not in previous and "# World" not in previous
    run(session.follow_up_plot("Make Vask crueller."))
    messages = session.provider.requests[-1].messages
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[2].parts[0].text == previous
    assert "The author asks: Make Vask crueller." in messages[3].parts[0].text


def test_a_plot_written_again_keeps_its_people_on_their_cards_and_undo_brings_it_back(
    tmp_path: Path,
):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE, CRUELLER])
    run(session.draft_plot(PlotWishes()))
    vask = next(c for c in session.bundle.cast if c.name == "Teodor Vask")
    kept = state(session)
    (turn, draft), _ = run(session.follow_up_plot("Make Vask crueller."))
    again = next(c for c in session.bundle.cast if c.name == "Teodor Vask")
    assert again.id == vask.id and again.plot_hidden
    assert "he enjoys a confession" in again.full_description
    assert turn.changed == ["The plot: 5 events"] and not turn.undone
    assert draft.events == 5
    assert session.undo_follow_up("plot")
    assert state(session) == kept


def test_who_a_plot_written_again_no_longer_has_goes_with_the_old_one(tmp_path: Path):
    session = session_for(harbour(), tmp_path, [PLOT_EXAMPLE, WITHOUT_VASK])
    run(session.draft_plot(PlotWishes()))
    kept = state(session)
    run(session.follow_up_plot("No customs captain."))
    assert "Teodor Vask" not in [c.name for c in session.bundle.cast]
    assert [e.title for e in session.story.plot.events] == ["The chapel at low tide"]
    assert session.undo_follow_up("plot")
    assert state(session) == kept


def test_merging_a_plot_written_again_takes_its_words_for_the_plot_s_own():
    session_bundle = harbour()
    story = without_plot(document_from_story(session_bundle))
    reply = read_reply(CRUELLER).document
    first, _ = merge_reply(story, read_reply(PLOT_EXAMPLE).document)
    assert not first.characters[-1].story_id
    first.characters[-1].story_id = "c-vask"
    merged, _ = merge_reply(first, reply, revise=True)
    vask = next(c for c in merged.characters if c.name == "Teodor Vask")
    assert vask.story_id == "c-vask" and "he enjoys a confession" in vask.description
    gone, _ = merge_reply(first, read_reply(WITHOUT_VASK).document, revise=True)
    assert "Teodor Vask" not in [c.name for c in gone.characters]


def test_the_fingerprint_ignores_what_the_story_writes_as_it_goes():
    bundle = harbour()
    before = setup_fingerprint(bundle)
    bundle.cast[0].condensed = "Mara, short."
    bundle.story.scene.situation = "Later."
    assert setup_fingerprint(bundle) == before
    bundle.cast[0].summary = "Changed."
    assert setup_fingerprint(bundle) != before


def test_a_turn_reads_as_what_was_done():
    turns = [
        FollowUpTurn(target=STORY, request="More  teeth.", summary="Sharper teeth."),
        FollowUpTurn(target=STORY, request="Undo me.", changed=["World"], undone=True),
        FollowUpTurn(target=STORY, request="Rain.", refused=["Not with settings."]),
    ]
    assert render_earlier(turns) == (
        "- “More teeth.”: Sharper teeth.\n- “Rain.”: nothing could be changed"
    )
