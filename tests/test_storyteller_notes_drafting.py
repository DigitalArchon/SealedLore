"""The storyteller's notes as a model writes them: a draft from a premise puts
the truth behind what it sets up into them, and a follow-up on a story not
yet begun keeps them true to what it changes. The author is told that there
are notes, never what they say."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.authoring import (
    FOLLOW_UP_KINDS,
    NOTES_NOT_SHOWN,
    REVIEW_KINDS,
    build_generation_messages,
    current_value,
    describe,
    draft_system,
    parse_generated,
    parse_review,
    reply_without_notes,
)
from sealedlore.engine.drafting import StoryDraft
from sealedlore.engine.followup import (
    NOTES_REWRITTEN,
    STORY,
    followup_system,
    setup_fingerprint,
    setup_view,
)
from sealedlore.engine.plot_draft import story_text
from sealedlore.engine.story_plot import document_from_story
from sealedlore.models.authoring import SettingsChange
from sealedlore.models.generation import ReasoningConfig
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import load_story_bundle, read_api_log
from tests.conftest import make_exchange
from tests.test_plot_draft import harbour, run, session_for

MARKER = "ZQX-SECRET"
SECRET = (
    f"The first mate sold the Guild's charts to the revenue men; his ledger is marked {MARKER}.\n"
    "Mara wants her debts gone before winter, and hides that the missing boat is hers."
)
DRAFT = {
    "title": "Salt and Tide",
    "description": "A boatwright with debts, and a boat nobody will speak of.",
    "world": "A harbour town on a cold coast, where half the town smuggles.",
    "cast": [
        {"name": "Mara Quill", "summary": "A boatwright with debts.", "playable": True},
        {"name": "Oswin Hale", "summary": "The first mate, hiding something."},
    ],
    "lore": [{"title": "The Salt Market", "content": "Stalls under the cliff."}],
    "play_as": "Mara Quill",
    "opening": "Fog on the quays at dawn.",
    "starting_scene": {"location": "The quays", "present": ["Mara Quill"]},
    "storyteller_notes": SECRET,
}


def draft_reply(**changes) -> str:
    return json.dumps({**DRAFT, **changes})


def changes(*items: dict, summary: str = "Done.") -> str:
    return json.dumps({"summary": summary, "changes": list(items), "cannot_fix": []})


# --- drafting from a premise ----------------------------------------------------------


def test_the_drafter_is_asked_for_the_notes_and_told_where_a_secret_goes():
    system = draft_system()
    assert '- "storyteller_notes": what only the storyteller is told.' in system
    assert "60 to 250 words" in system and "null when the story has no secrets" in system
    assert "what each wants and what each hides" in system
    assert '  "storyteller_notes": "...",\n' in system, "in the shape it must reply in"
    # A secret no longer "goes nowhere": lore still never states it.
    assert "a secret still to come out" not in system
    assert (
        "A secret still to come out is never stated in lore either:\n"
        '  its truth goes in "storyteller_notes".'
    ) in system
    assert 'set "until_mentioned"\n  true' in system, "plans the author may hear named still wait"
    # The premise's request is as it was: only the system prompt asks.
    user = build_generation_messages("A harbour town.")[1].text
    assert user == "The author's premise:\n\nA harbour town."


def test_a_drafts_notes_are_the_storys_and_a_models():
    scenario, warnings = parse_generated(draft_reply())
    assert scenario.storyteller_notes == SECRET
    assert scenario.storyteller_notes_by == "model"
    assert warnings == []
    assert MARKER not in json.dumps([e.model_dump() for e in scenario.lore])


@pytest.mark.parametrize("value", [None, "", "  ", "null"])
def test_a_draft_with_no_secrets_has_no_notes(value):
    scenario, warnings = parse_generated(draft_reply(storyteller_notes=value))
    assert scenario.storyteller_notes is None and scenario.storyteller_notes_by == "author"
    assert warnings == []


@pytest.mark.parametrize("value", [[SECRET], {"truth": SECRET}, 7, True])
def test_notes_that_are_not_text_are_dropped_and_said_without_their_words(value):
    scenario, warnings = parse_generated(draft_reply(storyteller_notes=value))
    assert scenario.storyteller_notes is None and scenario.storyteller_notes_by == "author"
    assert warnings == ["the storyteller's notes weren't text, so the story has none"]


def test_an_older_drafts_reply_reads_as_before():
    old = {key: value for key, value in DRAFT.items() if key != "storyteller_notes"}
    scenario, warnings = parse_generated(json.dumps(old))
    assert scenario.storyteller_notes is None and warnings == []


def test_a_drafted_story_is_saved_with_its_notes_unread(tmp_path: Path):
    draft = StoryDraft(
        MockChatProvider([draft_reply()]), "A harbour town.", "m", reasoning=ReasoningConfig()
    )
    list(draft.stream())
    bundle, warnings = draft.build(root=tmp_path)
    story = load_story_bundle(bundle.story.id, root=tmp_path).story
    assert (story.storyteller_notes, story.storyteller_notes_by) == (SECRET, "model")
    # What the author is told of the draft says nothing of them.
    assert MARKER not in json.dumps(warnings)
    logged = read_api_log(bundle.story.id, root=tmp_path)[-1]
    assert MARKER not in json.dumps(logged["warnings"])
    assert MARKER in logged["text"], "the API log keeps the reply as it came"


@pytest.mark.parametrize(
    "reply",
    [
        draft_reply(),
        draft_reply()[: draft_reply().index(MARKER) + 4],  # cut off inside the notes
        draft_reply(storyteller_notes=[SECRET, {"more": SECRET}]),
        "Here it is:\n" + json.dumps(DRAFT, indent=2) + "\nI hope it suits.",
    ],
)
def test_a_reply_shown_to_the_author_is_shown_without_the_notes(reply: str):
    shown = reply_without_notes(reply)
    assert MARKER not in shown and "revenue men" not in shown
    assert NOTES_NOT_SHOWN in shown
    assert "Salt and Tide" in shown and "A harbour town on a cold coast" in shown


def test_a_reply_with_no_notes_is_shown_as_it_came():
    for reply in ("not a draft at all", draft_reply(storyteller_notes=None)):
        assert reply_without_notes(reply) == reply


# --- follow-ups -----------------------------------------------------------------------


def with_notes(by: str = "model"):
    bundle = harbour()
    bundle.story.storyteller_notes = SECRET
    bundle.story.storyteller_notes_by = by
    return bundle


def test_the_follow_up_is_shown_the_notes_and_may_write_them_again():
    view = setup_view(with_notes())
    assert view["storyteller_notes"] == SECRET
    assert setup_view(harbour())["storyteller_notes"] is None
    system = followup_system()
    assert '- kind "storyteller_notes": the value is the complete new text' in system
    assert 'The author may not have read "storyteller_notes"' in system
    assert 'nothing of them in\n  "summary", a "reason" or "cannot_fix"' in system
    assert "storyteller_notes" in FOLLOW_UP_KINDS and "storyteller_notes" not in REVIEW_KINDS


def test_a_follow_up_rewrites_the_notes_and_tells_the_author_only_that(tmp_path: Path):
    rewritten = f"The harbour master sold the charts; her ledger is marked {MARKER}-TWO."
    reply = changes(
        {"kind": "character_remove", "target": "Oswin Hale", "reason": "asked"},
        {"kind": "storyteller_notes", "value": rewritten, "reason": "Oswin is gone"},
        {"kind": "world", "value": "A harbour town where the tide keeps its own books."},
        summary="Oswin is gone, and the world is stranger.",
    )
    session = session_for(with_notes("author"), tmp_path, [reply])
    turn, _ = run(session.follow_up_story("Lose Oswin, and make it stranger."))

    story = session.story
    assert (story.storyteller_notes, story.storyteller_notes_by) == (rewritten, "model")
    # One line, last, and none of the words.
    assert turn.changed == ["World", "Oswin Hale › out of the story", NOTES_REWRITTEN]
    assert NOTES_REWRITTEN == "The storyteller's notes were rewritten."
    assert MARKER not in json.dumps([turn.changed, turn.refused, turn.summary])
    # The model was shown them, in the setup, to keep them true to it.
    request = json.dumps(session.provider.payloads[-1], ensure_ascii=False)
    assert MARKER in request
    saved = load_story_bundle(story.id, root=tmp_path).story
    assert (saved.storyteller_notes, saved.storyteller_notes_by) == (rewritten, "model")

    # Undone with the rest: the author's own text, and theirs again.
    assert session.undo_follow_up(STORY)
    assert (story.storyteller_notes, story.storyteller_notes_by) == (SECRET, "author")
    assert [c.name for c in session.cast] == ["Mara Quill", "Oswin Hale"]
    assert not session.follow_up_changed_since()


def test_notes_sent_back_as_they_were_are_not_a_change(tmp_path: Path):
    reply = changes({"kind": "storyteller_notes", "value": SECRET})
    session = session_for(with_notes("author"), tmp_path, [reply])
    turn, _ = run(session.follow_up_story("Tidy it."))
    assert turn.changed == []
    assert session.story.storyteller_notes_by == "author", "still the author's own text"


def test_a_follow_up_may_leave_a_story_with_no_secrets(tmp_path: Path):
    session = session_for(with_notes(), tmp_path, [changes({"kind": "storyteller_notes"})])
    turn, _ = run(session.follow_up_story("No mysteries, please."))
    assert turn.changed == [NOTES_REWRITTEN]
    assert session.story.storyteller_notes is None
    assert session.story.storyteller_notes_by == "author"


def test_notes_that_are_not_text_are_refused_without_their_words(tmp_path: Path):
    reply = changes({"kind": "storyteller_notes", "value": [f"a list with {MARKER}-THREE"]})
    session = session_for(with_notes(), tmp_path, [reply])
    turn, _ = run(session.follow_up_story("Stranger."))
    assert turn.changed == [] and session.story.storyteller_notes == SECRET
    assert turn.refused == ["Proposed The storyteller's notes, but the notes must be text."]


def test_the_fingerprint_follows_the_notes(tmp_path: Path):
    bundle = with_notes()
    before = setup_fingerprint(bundle)
    bundle.story.storyteller_notes = SECRET + " And the tide knows."
    assert setup_fingerprint(bundle) != before


def test_a_change_to_the_notes_never_carries_their_words_where_they_are_listed():
    bundle = with_notes()
    change = SettingsChange(kind="storyteller_notes", value="New secrets.")
    assert describe(change) == "The storyteller's notes"
    assert current_value(bundle, change) is None, "no “before” to show or keep"


def test_a_settings_review_cannot_change_the_notes():
    bundle = with_notes()
    bundle.nodes = make_exchange(1)
    review = parse_review(changes({"kind": "storyteller_notes", "value": "Other secrets."}), bundle)
    assert review.changes == []
    assert review.cannot_fix == ["Proposed The storyteller's notes, which can't be changed here."]
    assert bundle.story.storyteller_notes == SECRET


# --- what never sees them ---------------------------------------------------------------


def test_a_plot_is_drafted_from_the_story_without_its_notes():
    bundle = with_notes()
    for for_model in (False, True):
        document = document_from_story(bundle, for_model=for_model)
        assert MARKER not in story_text(document)
        assert MARKER not in document.model_dump_json()
