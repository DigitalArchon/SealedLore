"""The engine's story state, looked over before 3.5.0b1 (no Qt). The scripted
mock answers; nothing here reaches a network."""

# ruff: noqa: F811 (the session fixture, imported, is each test's argument)

from __future__ import annotations

import pytest

from sealedlore.engine.chronicle import clock_minutes
from sealedlore.engine.session import StorySession
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.private import PrivateSpan
from sealedlore.providers.base import StreamCancelled, TextDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.tree import link_child
from tests.test_branches import names, rewrite
from tests.test_chronicle import PASSAGE, read
from tests.test_chronicle import make_session as plot_session
from tests.test_chronicle import turn as plot_turn
from tests.test_phase7 import play, turn
from tests.test_phase7 import session as session  # noqa: F401 (a fixture)

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
