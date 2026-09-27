"""Branches (Sept 2026): a take swaps in place and is never a branch; a
branch is made by rewriting an earlier message, and has a name."""

# ruff: noqa: F811 (the session fixture, imported, is each test's argument)

from __future__ import annotations

from sealedlore.engine.branches import branches
from sealedlore.engine.session import StorySession
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.private import PrivateSpan
from sealedlore.providers.mock import MockChatProvider
from sealedlore.tree import link_child
from tests.test_phase7 import play
from tests.test_phase7 import session as session  # noqa: F401 (a fixture)


def names(session: StorySession) -> list[tuple[str, int, bool]]:
    return [(b.name, b.start_at, b.is_current) for b in session.branch_list()]


def rewrite(session: StorySession, node_id: str, text: str, reply="A new reply.", name=None):
    session.provider = MockChatProvider([reply])
    return list(session.rewrite_from(node_id, text, name=name))


def test_a_story_is_one_branch_however_often_it_is_retaken(session: StorySession):
    play(session, "one", "two", "three")
    for position in (5, 1, 3):
        session.provider = MockChatProvider(["Another take."])
        list(session.regenerate(session.path()[position].id))
    assert names(session) == [("Main", 1, True)]


def test_rewriting_an_author_turn_answers_it_on_a_new_branch(session: StorySession):
    play(session, "one", "two", "three")
    main_end = session.path()[-1]
    rewrite(session, session.path()[2].id, "a different two")

    path = session.path()
    assert [n.content for n in path] == ["one", "Reply to one", "a different two", "A new reply."]
    assert path[2].meta.branch_name == "Branch 2"
    assert names(session) == [("Main", 1, False), ("Branch 2", 3, True)]
    main = session.branch_list()[0]
    assert main.end_id == main_end.id and main.length == 6, "the old line is untouched"


def test_rewriting_a_passage_waits_for_the_author(session: StorySession):
    play(session, "one", "two")
    rewrite(session, session.path()[1].id, "The author's own reply.", name="What if")
    assert session.provider.requests == [], "the author's prose is not answered"
    assert names(session) == [("Main", 1, False), ("What if", 2, True)]
    assert session.path()[-1].content == "The author's own reply."


def test_a_branch_of_a_branch_is_listed_under_it(session: StorySession):
    play(session, "one", "two", "three")
    rewrite(session, session.path()[2].id, "b two")
    play(session, "b three")
    rewrite(session, session.path()[4].id, "c three")
    listed = session.branch_list()
    assert [(b.name, b.depth) for b in listed] == [("Main", 0), ("Branch 2", 1), ("Branch 3", 2)]
    assert listed[2].parent_start_id == listed[1].start_id


def test_switching_renaming_and_deleting(session: StorySession):
    play(session, "one", "two")
    rewrite(session, session.path()[2].id, "b two")
    branch = session.current_branch()
    session.switch_branch(None)
    assert [n.content for n in session.path()][-1] == "Reply to two"
    session.rename_branch(None, "The canon")
    session.rename_branch(branch.start_id, "  What   if ")
    assert names(session) == [("The canon", 1, True), ("What if", 3, False)]

    session.switch_branch(branch.start_id)
    session.delete_branch(branch.start_id)  # the one being read: back to its parent
    assert names(session) == [("The canon", 1, True)]
    assert len(session.path()) == 4


def test_a_rewritten_passage_keeps_its_takes_on_its_branch(session: StorySession):
    play(session, "one")
    rewrite(session, session.path()[1].id, "The author's reply.")
    session.provider = MockChatProvider(["A take of it."])
    list(session.regenerate(session.path()[1].id))
    assert names(session) == [("Main", 1, False), ("Branch 2", 2, True)]
    assert session.take_position(session.path()[1].id) == (2, 2)


def test_renaming_a_branch_renames_its_first_passages_takes(session: StorySession):
    """A rewritten passage's takes share its name: renamed alone, the others
    would be read as a branch of their own."""
    play(session, "one")
    rewrite(session, session.path()[1].id, "The author's reply.")
    session.provider = MockChatProvider(["A take of it."])
    list(session.regenerate(session.path()[1].id))
    session.rename_branch(session.current_branch().start_id, "Stay aboard")
    assert names(session) == [("Main", 1, False), ("Stay aboard", 2, True)]
    assert session.take_position(session.path()[1].id) == (2, 2)


def test_a_name_already_used_is_numbered(session: StorySession):
    """Two branches from one message with one name would be read as takes of
    one: the second is numbered."""
    play(session, "one", "two")
    rewrite(session, session.path()[2].id, "b two", name="Ending")
    session.switch_branch(None)
    rewrite(session, session.path()[2].id, "c two", name="ending")
    assert [b.name for b in session.branch_list()] == ["Main", "Ending", "ending (2)"]
    assert session.rename_branch(None, "Ending") == "Ending (3)"
    assert session.rename_branch(None, "Main again") == "Main again"


def test_a_fork_from_before_names_is_read_as_one(session: StorySession):
    play(session, "one", "two")
    session.branch_from(session.path()[1].id)  # no name: made before branches had them
    play(session, "another two")
    assert names(session) == [("Main", 1, False), ("Branch from message 3", 3, True)]


def test_the_reviews_branch_is_named_before_its_first_turn(session: StorySession):
    play(session, "one", "two")
    session.branch_from(session.path()[1].id, "Before Rosa appeared")
    play(session, "another two")
    assert names(session)[-1] == ("Before Rosa appeared", 3, True)


def test_rewriting_the_opening_turn_starts_a_branch_from_the_start(session: StorySession):
    play(session, "one")
    rewrite(session, session.path()[0].id, "a different one")
    assert names(session) == [("Main", 1, False), ("Branch 2", 1, True)]
    assert [n.content for n in session.path()] == ["a different one", "A new reply."]


def test_a_discarded_private_scene_is_no_branch(session: StorySession):
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
    assert names(session) == [("Main", 1, True)]
    assert len(branches(session.nodes, [n.id for n in session.path()])) == 2, "without it"


def test_the_map_has_a_lane_per_branch_joined_where_it_split(session: StorySession):
    from sealedlore.models.summary import Summary

    play(session, "one", "two", "three")
    covered = [n.id for n in session.path()[:4]]
    session.bundle.summaries.append(Summary(covered_node_ids=covered, content="Early days."))
    rewrite(session, session.path()[4].id, "b three")
    play(session, "b four")
    rewrite(session, session.path()[6].id, "c four")

    lanes = session.story_map()
    assert [(lane.branch.name, lane.column, lane.parent_column) for lane in lanes] == [
        ("Main", 0, None),
        ("Branch 2", 1, 0),
        ("Branch 3", 2, 1),
    ]
    main, second, third = lanes
    assert (second.first, second.branch.length) == (5, 8)
    assert (third.first, third.branch.length) == (7, 8)
    assert main.chapters == second.chapters == ((1, 4),), "the chapter every line shares"
    assert second.node_at(5) == second.branch.start_id and second.node_at(99) is None


def test_a_take_before_a_private_scene_waits_for_it_to_end(session: StorySession):
    import pytest

    play(session, "one", "two")
    session.provider = MockChatProvider(["Retake."])
    list(session.regenerate(session.path()[1].id))
    retake = session.path()[1]
    span = PrivateSpan(model="m", keep="memory", start_parent_id=session.path()[-1].id)
    session.story.private_spans.append(span)
    assert session.open_span is span
    with pytest.raises(ValueError, match="private scene"):
        session.switch_take(retake.id, -1)
    assert session.path()[1].id == retake.id


def test_a_map_column_is_reused_once_its_lane_has_ended(session: StorySession):
    """A branch late in the story used to get a column of its own and cross
    the empty columns of branches that had ended long before."""
    play(session, "one", "two", "three", "four", "five")
    rewrite(session, session.path()[2].id, "an early branch")  # ends at message 4
    session.switch_branch(None)
    rewrite(session, session.path()[8].id, "a late branch")  # from message 9
    lanes = session.story_map()
    assert [(lane.branch.name, lane.column, lane.parent_column) for lane in lanes] == [
        ("Main", 0, None),
        ("Branch 2", 1, 0),
        ("Branch 3", 1, 0),
    ]
