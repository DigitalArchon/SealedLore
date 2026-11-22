"""The engine's story state, looked over before 3.5.0b1 (no Qt). The scripted
mock answers; nothing here reaches a network."""

# ruff: noqa: F811 (the session fixture, imported, is each test's argument)

from __future__ import annotations

import json

import pytest

from sealedlore.engine import session as session_module
from sealedlore.engine import session_plot
from sealedlore.engine.archival import characters_in
from sealedlore.engine.characters import known_names, parse_suggestions
from sealedlore.engine.chronicle import clock_minutes
from sealedlore.engine.chronicle_read import parse_read
from sealedlore.engine.director import parse_gap
from sealedlore.engine.lore_pick import parse_pick
from sealedlore.engine.scene_state import same_person
from sealedlore.engine.scene_update import Resolved, _named_in_speech, parse_delta, resolve_name
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.story_suggestions import _card_named
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.validators import locate_quote, mentions, named_only
from sealedlore.models.character import Character
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.private import PrivateSpan
from sealedlore.providers.base import StreamCancelled, TextDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.tree import link_child
from tests.conftest import make_exchange
from tests.test_background_archival import Background, Storyteller
from tests.test_background_archival import turn as wait_turn
from tests.test_branches import names, rewrite
from tests.test_chronicle import PASSAGE, read
from tests.test_chronicle import make_session as plot_session
from tests.test_chronicle import turn as plot_turn
from tests.test_lore_pick import ACCORD, LOCKS, entries, large
from tests.test_phase7 import play, turn
from tests.test_phase7 import session as session  # noqa: F401 (a fixture)
from tests.test_plot_v2 import gap_session
from tests.test_plot_v2 import turn as gap_turn
from tests.test_session import FILLER, make_config

# --- E1, E10: deleting a branch's first message leaves the story on a line -------


def shown(session: StorySession) -> list[str]:
    return [node.content for node in session.full_path()]


MAIN = ["one", "Reply to one", "two", "Reply to two", "three", "Reply to three"]


def test_deleting_a_branch_that_starts_at_the_first_message_goes_to_the_line_left(
    session: StorySession,
):
    """The reviewer's sequence A: the story was left with no current message,
    an empty page over Main's six messages, and nothing in the window to
    reach them by (one branch: no branch bar)."""
    play(session, "one", "two", "three")
    rewrite(session, session.path()[0].id, "A different first message", name="Other start")
    assert names(session) == [("Main", 1, False), ("Other start", 1, True)]

    session.delete_from(session.full_path()[0].id)

    assert session.story.active_leaf_id is not None
    assert shown(session) == MAIN
    assert names(session) == [("Main", 1, True)]
    # The next turn carries Main on: no line appears beside it.
    play(session, "four")
    assert names(session) == [("Main", 1, True)]
    assert shown(session)[-2:] == ["four", "Reply to four"]


def test_a_first_message_taken_back_never_leaves_an_empty_page_over_a_story(
    session: StorySession,
):
    """Taken back, the first message of a branch from the very start has no
    message to stand on: the story is never nowhere while it has messages
    (an empty page offers Start the story…)."""
    play(session, "one", "two", "three")
    rewrite(session, session.path()[0].id, "A different first message", name="Other start")
    session.delete_from(session.full_path()[0].id, take_back=True)
    assert session.story.active_leaf_id is not None
    assert shown(session) == MAIN


def test_deleting_a_branch_another_was_made_from_takes_the_story_off_both(
    session: StorySession,
):
    """Sequence B: on C, made from B. Deleting B (the Branches map) deletes C
    with it; the story was left on the turn B split from, Main's other
    messages hidden, and the next turn made "Branch from message 4"."""
    play(session, "one", "two", "three")
    rewrite(session, session.path()[3].id, "Passage two, rewritten.", name="B")
    play(session, "b one", "b two")
    rewrite(session, session.full_path()[-2].id, "b two another way", name="C")
    assert [(b.name, b.is_current) for b in session.branch_list()] == [
        ("Main", False),
        ("B", False),
        ("C", True),
    ]
    b = next(branch for branch in session.branch_list() if branch.name == "B")

    session.delete_branch(b.start_id)

    assert names(session) == [("Main", 1, True)]
    assert shown(session) == MAIN
    play(session, "four")
    assert names(session) == [("Main", 1, True)]


def test_deleting_a_branchs_first_passage_goes_back_to_the_line_it_split_from(
    session: StorySession,
):
    """Sequence C: New branch from a passage, then Delete from here… on it.
    The story stood on the turn before, which looked unanswered though
    Main's passage and everything after it were still under it, and
    Regenerate there wrote a take that hid them for good."""
    play(session, "one", "two", "three")
    passage_two = session.path()[3]
    rewrite(session, passage_two.id, "Passage two, rewritten.", name="B")
    assert shown(session)[-1] == "Passage two, rewritten."

    session.delete_from(session.full_path()[-1].id)

    assert shown(session) == MAIN
    assert names(session) == [("Main", 1, True)]
    # Regenerate is of Main's latest passage, and Main's line stays whole.
    session.provider = MockChatProvider(["Another reply to three."])
    list(session.regenerate())
    assert shown(session) == [*MAIN[:-1], "Another reply to three."]
    assert passage_two.id in {node.id for node in session.full_path()}
    assert names(session) == [("Main", 1, True)]


def test_deleting_a_branchs_first_turn_then_sending_stays_on_the_line_left(
    session: StorySession,
):
    """The fuzzer's seed 314: a branch from an author turn, Delete from here…
    on that turn, a new turn. The turn was attached at the fork, where it
    read as "Branch from message 3" beside Main."""
    play(session, "one", "two", "three")
    rewrite(session, session.path()[2].id, "a different two")
    session.delete_from(session.full_path()[2].id)
    assert shown(session) == MAIN
    play(session, "four")
    assert names(session) == [("Main", 1, True)]
    assert shown(session) == [*MAIN, "four", "Reply to four"]


def test_a_branchs_first_turn_taken_back_keeps_its_name_for_the_turn_sent_again(
    session: StorySession,
):
    """E10: Take back puts the turn in the box to be sent again where it
    stood. The story stays at the fork, and the turn sent again is the
    branch's first, by its name; it came back as "Branch from message 3"."""
    play(session, "one", "two", "three")
    rewrite(session, session.path()[2].id, "a different two", name="What if")
    fork = session.full_path()[1]

    session.delete_from(session.full_path()[2].id, take_back=True)

    assert session.story.active_leaf_id == fork.id
    assert session.pending_branch == (fork.id, "What if")
    assert names(session) == [("Main", 1, True)]
    play(session, "a different two, again")
    assert names(session) == [("Main", 1, False), ("What if", 3, True)]
    assert shown(session)[2:] == ["a different two, again", "Reply to a different two, again"]
    session.switch_branch(None)
    assert shown(session) == MAIN


def test_a_fork_from_before_names_taken_back_keeps_the_name_it_was_read_by(
    session: StorySession,
):
    play(session, "one", "two")
    session.branch_from(session.path()[1].id)  # no name: made before branches had them
    play(session, "another two")
    assert names(session)[-1] == ("Branch from message 3", 3, True)
    session.delete_from(session.full_path()[2].id, take_back=True)
    assert session.pending_branch == (session.full_path()[-1].id, "Branch from message 3")
    play(session, "another two, again")
    assert names(session) == [("Main", 1, False), ("Branch from message 3", 3, True)]


def test_a_reply_deleted_still_leaves_its_turn_whatever_other_takes_it_has(
    session: StorySession,
):
    """Unchanged: a take that ended is no line to go to. The turn stands
    unanswered, and Regenerate takes another run at it."""
    play(session, "one", "two")
    session.provider = MockChatProvider(["Another reply to two."])
    list(session.regenerate())
    turn_two = session.path()[2]
    session.delete_from(session.path()[-1].id)
    assert session.story.active_leaf_id == turn_two.id
    session.provider = MockChatProvider(["A third reply to two."])
    list(session.regenerate())
    assert session.take_position(session.path()[-1].id) == (2, 2)
    assert names(session) == [("Main", 1, True)]


def test_a_turn_deleted_beside_another_branchs_start_leaves_its_own_line_there(
    session: StorySession,
):
    """Unchanged: the message a branch split from is where Main ends once
    Main's own turn there is deleted. The branch is not Main's to go into."""
    play(session, "one", "two")
    main_two = session.path()[2]
    rewrite(session, main_two.id, "a different two", name="What if")
    session.switch_branch(None)
    session.delete_from(main_two.id)
    assert shown(session) == ["one", "Reply to one"]
    assert names(session) == [("Main", 1, True), ("What if", 3, False)]


def test_a_turn_taken_back_on_a_plain_line_is_sent_again_where_it_stood(
    session: StorySession,
):
    play(session, "one", "two")
    session.delete_from(session.path()[2].id, take_back=True)
    assert shown(session) == ["one", "Reply to one"]
    assert session.pending_branch is None
    play(session, "two again")
    assert names(session) == [("Main", 1, True)]


def test_a_delete_never_goes_into_a_discarded_private_scene(session: StorySession):
    """A discarded scene's messages are kept under the passage it began at, a
    side line nobody is sent or shown: never the line that remains."""
    play(session, "one")
    anchor = session.path()[-1]
    span = PrivateSpan(model="m", keep="disk", start_parent_id=anchor.id, status="discarded")
    session.story.private_spans.append(span)
    hidden = Node(
        kind="user", speaker_id="char-serrik", content="x", meta=NodeMeta(private_span=span.id)
    )
    link_child(anchor, hidden)
    session.nodes.append(hidden)
    play(session, "two")
    session.delete_from(session.path()[2].id)
    assert session.story.active_leaf_id == anchor.id


def test_in_an_open_private_scene_a_delete_stays_where_the_scene_is(session: StorySession):
    """The scene began at a passage the story had gone on from: its first
    message deleted leaves the scene open at that passage, not at the end
    of the line beside it."""
    play(session, "one", "two")
    anchor = session.path()[1]
    session.story.active_leaf_id = anchor.id
    private = MockChatProvider(["A private reply."])
    session.enter_private(keep="disk", provider=private, model="private-model")
    list(session.send(turn("a private turn")))
    first = next(node for node in session.full_path() if node.meta.private_span)
    session.delete_from(first.id)
    assert session.in_private
    assert session.story.active_leaf_id == anchor.id


# --- E2: a take stopped partway is a take of its branch, as a whole one is -------


def stopped_partway(session: StorySession, job, how: str) -> None:
    """Run `job` as the worker does and stop it at its first piece of text:
    "cancel" is Stop (the stream raises), "close" the worker letting go of
    the job where it stands."""
    for event in job:
        if isinstance(event, TextDelta):
            break
    if how == "close":
        job.close()
        return
    session.provider.cancel()
    with pytest.raises(StreamCancelled):
        list(job)


@pytest.mark.parametrize("how", ["cancel", "close"])
def test_a_take_of_a_branchs_first_passage_stopped_partway_stays_on_the_branch(
    session: StorySession, how: str
):
    """The take was named for its branch only after the job had run to its
    end. Stopped partway it was kept, as any stopped passage, with no name:
    an unnamed take beside Main's passage, so the story was back on Main
    with the branch's own first passage out of the takes' reach."""
    play(session, "one", "two")
    rewrite(session, session.path()[3].id, "The author's reply.", name="Other way")
    assert names(session) == [("Main", 1, False), ("Other way", 4, True)]

    session.provider = MockChatProvider(["A take of it, long enough to be stopped partway. " * 3])
    stopped_partway(session, session.regenerate(), how)

    take = session.full_path()[-1]
    assert take.content and take.content != "The author's reply."
    assert take.meta.branch_name == "Other way"
    assert names(session) == [("Main", 1, False), ("Other way", 4, True)]
    assert session.take_position(take.id) == (2, 2)
    # And it is saved so.
    saved = {node.id: node for node in session.bundle_to_save().nodes}
    assert saved[take.id].meta.branch_name == "Other way"


@pytest.mark.parametrize("how", ["cancel", "close"])
def test_a_take_stopped_partway_keeps_the_authors_plot_correction(tmp_path, how: str):
    """As a whole take does: the clock the author set is their statement of
    where the plot stands after the turn, whatever prose stands there."""
    session = plot_session(
        tmp_path, [PASSAGE, read(10), "Another take, long enough to be stopped partway. " * 3]
    )
    list(session.send(plot_turn(session)))
    session.set_clock(clock_minutes(3, 8, 0))
    stopped_partway(session, session.regenerate(), how)
    assert session.path()[-1].content.startswith("Another take")
    assert session.path()[-1].meta.chronicle.source == "author"
    assert session.chronicle.minutes == clock_minutes(3, 8, 0)


# --- E3: no reply of a side call's model costs the turn ---------------------------


@pytest.mark.parametrize("reply", ['{"pick": 2}', '{"pick": true}', '{"pick": "12"}'])
@pytest.mark.parametrize("more", ["", ', "why": {"1": "the treaty"}'])
def test_picks_that_are_no_list_are_no_picks(reply: str, more: str):
    """A number raised TypeError out of the parser, and so out of the job,
    after the passage was written; a string was read a character at a time
    ("12" picked the first and the second)."""
    with pytest.raises(ValueError, match="list"):
        parse_pick(reply[:-1] + more + "}", entries())


def test_a_pick_that_is_no_whole_number_is_passed_over():
    reply = '{"pick": [Infinity, NaN, true, 1e400, [1], {"n": 1}, 2.0, "1"]}'
    assert parse_pick(reply, entries()) == [LOCKS, ACCORD]
    assert parse_pick('{"pick": null}', entries()) == []
    assert parse_pick('{"other": 1}', entries()) == []


@pytest.mark.parametrize(
    "elapsed",
    [
        '{"amount": Infinity}',
        '{"amount": NaN}',
        '{"amount": "1e999"}',
        '{"amount": 1e308, "unit": "days"}',
        '{"amount": true, "unit": "days"}',
        '{"amount": 1' + "0" * 400 + ', "unit": "weeks"}',
    ],
    ids=["infinity", "nan", "a string past any float", "past any float in minutes", "true", "huge"],
)
@pytest.mark.parametrize("day", ["true", "1" + "0" * 400, "2.5"])
def test_an_elapsed_time_that_is_no_amount_is_no_time(elapsed: str, day: str):
    """Infinity was an OverflowError out of the read (a float JSON allows),
    and so out of the job after the passage."""
    delta = parse_read('{"elapsed": ' + elapsed + ', "day": ' + day + "}")
    assert delta.elapsed == 0 and delta.day is None


def test_an_elapsed_time_is_still_read_in_its_unit():
    assert parse_read('{"elapsed": {"amount": "2.5", "unit": "hours"}, "day": "4"}').elapsed == 150
    assert parse_read('{"elapsed": {"amount": 3, "unit": "weeks"}, "day": 4}').day == 4


@pytest.mark.parametrize("accounts", ["3", "true", "NaN", '"the raid"', '{"event": "the_raid"}'])
def test_accounts_that_are_no_list_are_no_accounts(accounts: str):
    assert parse_gap('{"accounts": ' + accounts + "}", ["the_raid"]) == {}


def notices(events) -> list[str]:
    return [event.text for event in events if isinstance(event, SessionNotice)]


def errors_logged(session: StorySession) -> list[dict]:
    log = read_api_log(session.story.id, root=session.root)
    return [entry for entry in log if entry["kind"] == "side_call_error"]


@pytest.fixture
def picker(tmp_path, story, cast) -> StorySession:
    """A story whose lorebook is chosen from by the lore model."""
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider(["The door gives with a groan."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_a_pick_that_is_a_number_is_a_notice_and_the_next_turn_uses_similarity(picker):
    large(picker, "picker")
    picker.provider = MockChatProvider(["They talk of the treaty.", '{"pick": 2}', "Later."])
    events = list(picker.send(turn("I ask about the accord.")))
    assert any("Lore pick failed" in text for text in notices(events))
    assert picker.path()[-1].meta.lore_picks is None
    list(picker.send(turn("Mind the lock.")))
    assert "no picks" in (picker.last_retrieval.selector_note or "")
    # The parser's own refusal is no fault of the program's to put on record.
    assert errors_logged(picker) == []


def boom(*args, **kwargs):
    raise TypeError("'int' object is not iterable")


def test_a_fault_reading_the_pick_costs_no_turn_and_is_put_on_record(picker, monkeypatch):
    """The boundary, whatever the parser lets through: the passage is
    written and paid for, so the job goes on; and since the fault is the
    program's, it is in the API log to be found, not only in a notice."""
    large(picker, "picker")
    picker.provider = MockChatProvider(["They talk of the treaty.", '{"pick": [1]}'])
    monkeypatch.setattr(session_module, "parse_pick", boom)
    events = list(picker.send(turn("I ask about the accord.")))
    assert any("Lore pick failed" in t and "TypeError" in t for t in notices(events))
    assert picker.path()[-1].content == "They talk of the treaty."
    (logged,) = errors_logged(picker)
    assert logged["what"] == "lore pick" and logged["error"].startswith("TypeError: ")
    assert "boom" in logged["traceback"]


def test_a_fault_reading_the_clock_costs_no_turn_and_is_put_on_record(tmp_path, monkeypatch):
    session = plot_session(tmp_path, [PASSAGE, read(10)])
    monkeypatch.setattr(session_plot, "parse_chronicle", boom)
    events = list(session.send(plot_turn(session)))
    assert any("Couldn't read the clock" in t and "TypeError" in t for t in notices(events))
    assert session.path()[-1].content == PASSAGE
    (logged,) = errors_logged(session)
    assert logged["what"] == "clock and facts read"


def test_a_clock_read_of_no_amount_is_read_as_no_time(tmp_path):
    session = plot_session(tmp_path, [PASSAGE, '{"elapsed": {"amount": Infinity}}'])
    before = session.chronicle.minutes
    events = list(session.send(plot_turn(session)))
    assert not any("Couldn't read the clock" in text for text in notices(events))
    assert session.chronicle.minutes == before


@pytest.mark.parametrize("fault", ["reply", "program"])
def test_accounts_of_a_skip_that_cant_be_read_still_settle_the_event(
    tmp_path, monkeypatch, fault: str
):
    """A scalar under "accounts" was a TypeError before the passage: the
    turn was attached and never answered."""
    session = gap_session(tmp_path, '{"accounts": 3}')
    if fault == "program":
        monkeypatch.setattr(session_plot, "parse_gap", boom)
    events = list(session.send(gap_turn(session, "I sweep the floor.")))
    status = session.chronicle.events["the_raid"]
    assert status.state == "happened"
    assert status.account.startswith("Raiders come; Mara the trader limps in")
    assert session.path()[-1].kind == "assistant"
    if fault == "program":
        assert any("Couldn't ask how the skipped-over events went" in t for t in notices(events))
        (logged,) = errors_logged(session)
        assert logged["what"] == "skipped-over events"
    else:
        assert errors_logged(session) == []


# --- E4: a name's apostrophe is whichever one the prose writes -------------------

APOSTROPHES = ["'", "’", "‘", "ʼ"]


@pytest.mark.parametrize("on_card", ["'", "’"])
@pytest.mark.parametrize("in_prose", APOSTROPHES)
def test_a_name_with_an_apostrophe_is_found_whichever_the_prose_writes(on_card: str, in_prose: str):
    """A card's "T'Kara" was nobody in prose that wrote "T’Kara", as every
    model that sets its own apostrophes does: off the roster, unknown to the
    summariser, an arrival never matched to her card. Only a second word
    ("Sean" of Sean O'Brien) hid it for longer names."""
    card = Character(id="c1", name=f"T{on_card}Kara")
    name = f"T{in_prose}Kara"
    passage = f"The door opened and {name} came in from the yard, shaking off the rain."
    assert mentions(passage, card)
    assert same_person(name, card)
    assert named_only(name, card)
    assert characters_in(passage, [card]) == [card]
    # The scene read's arrival is hers, by her card.
    delta = parse_delta(
        json.dumps({"arrived": [{"name": name, "how": "came in from the yard"}]}), cast=[card]
    )
    assert [(person.name, person.character_id) for person in delta.arrived] == [(card.name, "c1")]
    assert delta.unnamed_arrivals((passage, ""), people=[card]) == []
    # And on a supporting card, or kept as written with no card at all.
    assert resolve_name(name, cast=[], supporting=[card]) == Resolved(card.name)
    assert same_person(card.name, Character(id="", name=name))


@pytest.mark.parametrize("apostrophe", APOSTROPHES[:3])
def test_possessives_still_count_and_parts_of_words_still_do_not(apostrophe: str):
    card = Character(id="c1", name="T'Kara")
    for possessive in ("'", "’"):
        assert mentions(f"It was T{apostrophe}Kara{possessive}s knife on the table.", card)
    brien = Character(id="c2", name="Sean O'Brien")
    assert mentions(f"O{apostrophe}Brien nodded.", brien)
    assert not mentions("The O and the Brien of it.", brien)
    ann = Character(id="c3", name="Ann")
    assert mentions("Ann’s coat hung by the door.", ann)
    assert not mentions("The announcer cleared his throat.", ann)
    assert not mentions("T Kara, said the sign.", card)


def test_a_person_already_on_file_is_not_suggested_under_the_other_apostrophe():
    known = known_names([Character(id="c1", name="T'Kara", aliases=["Sean O'Brien"])])
    reply = json.dumps(
        [
            {"name": "T’Kara", "description": "A smith."},
            {"name": "O’Brien", "description": "A carter."},
            {"name": "D’Arcy", "description": "A clerk."},
            {"name": "D'Arcy", "description": "The same clerk."},
        ]
    )
    assert [s.name for s in parse_suggestions(reply, known=known)] == ["D’Arcy"]
    # A change suggested for her card finds it by either spelling.
    card = Character(id="c1", name="T'Kara")
    assert _card_named("T’Kara", [card, Character(id="c2", name="Maela Orr")]) is card


def test_a_sentence_is_found_whichever_apostrophe_its_quote_was_retyped_with():
    """The reads quote the passage back, and a quote is believed only when
    it is found there: retyped with the other apostrophe in a name, the
    sentence that showed her arriving was no sentence of the passage."""
    passage = "Rain on the roof. Then T’Kara’s lantern showed at the gate, and she came in."
    found = locate_quote(passage, "Then T'Kara's lantern showed at the gate, and she came in.")
    assert found is not None
    assert passage[found[0] : found[1]].startswith("Then T’Kara’s lantern")
    assert locate_quote(passage, "Then T'Kara's horse showed at the gate") is None


def test_a_name_only_ever_spoken_is_told_so_whichever_apostrophe():
    card = Character(id="c1", name="T'Kara")
    passage = "“Where is T’Kara?” the smith asked."
    assert _named_in_speech(passage, 0, len(passage), card) == "talked about"


# --- E6: a Stop on the notice that adopts background chapters loses nothing ------


@pytest.fixture
def background(tmp_path, story, cast) -> StorySession:
    """A long story on a client that writes its chapters in the background
    (tests/test_background_archival.py's), a turn short of its budget."""
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = make_exchange(24)
    for node in nodes:
        node.content = f"{node.content} {FILLER * 12}".strip()
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    story.active_leaf_id = nodes[-1].id
    story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    made = StorySession(
        bundle,
        make_config(),
        Storyteller(["A passage."], Background()),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    full = made.assemble(wait_turn()).full_total
    made.story.defaults.context_token_budget = int(full / 0.95)
    return made


BACKGROUND_KINDS = {
    "summary": "summary_response",
    "scan": "characters_response",
    "suggest": "story_suggestions_response",
}


def answered_in_the_log(session: StorySession) -> dict[str, int]:
    """How many background replies of each kind the API log holds."""
    log = read_api_log(session.story.id, root=session.root)
    asked = {e["id"] for e in log if e["kind"].endswith("_request") and e.get("background")}
    return {
        kind: sum(1 for e in log if e["kind"] == logged and e.get("request_log_ref") in asked)
        for kind, logged in BACKGROUND_KINDS.items()
    }


@pytest.mark.parametrize("edited", [False, True])
def test_a_stop_on_the_adoption_notice_leaves_every_paid_call_in_the_log(
    background: StorySession, edited: bool
):
    """The worker lets go of the job at the event Stop lands on. That was
    the first notice of the adoption, ahead of the rest of it: the scan and
    the suggestions read in the background were never taken in, and their
    calls, and those of chapters not used, never reached the log or the
    cost."""
    session = background
    list(session.send(wait_turn()))
    job = session._archive_job
    assert job is not None and job.done.wait(5)
    calls = session.provider.background.calls
    paid = {kind: calls.count(kind) for kind in BACKGROUND_KINDS}
    assert paid["summary"] and paid["scan"] == 1 and paid["suggest"] == 1
    if edited:
        # The author corrects an old message meanwhile: nothing is adopted.
        session.edit_node(session.path()[0].id, "An old turn, corrected.")
    session.story.defaults.context_token_budget = int(
        session.assemble(wait_turn()).full_total * 0.9
    )

    sending = session.send(wait_turn())
    first = next(sending)
    assert isinstance(first, SessionNotice) and "written in the background" in first.text
    sending.close()

    assert session._archive_job is None
    assert answered_in_the_log(session) == paid
    suggested = [s.title for s in session.story.story_suggestions]
    assert bool(session.summaries) is not edited
    assert suggested == ([] if edited else ["The Undercroft"])
