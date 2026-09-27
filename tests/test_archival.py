"""Chunked archival and per-branch summaries."""

from __future__ import annotations

from sealedlore.engine.archival import (
    build_summary_messages,
    chapter_number,
    mark_stale,
    plan_chunk,
    split_path,
    stale_summaries,
    turns_in,
)
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.summary import Summary
from sealedlore.tree import link_child
from tests.conftest import make_exchange


def summary_for(nodes, content: str = "It happened.", **kwargs) -> Summary:
    return Summary(covered_node_ids=[node.id for node in nodes], content=content, **kwargs)


# --- chunk planning -------------------------------------------------------


def test_a_chunk_is_ten_exchanges_not_ten_messages():
    nodes = make_exchange(14)
    chunk = plan_chunk(nodes, turns=10, keep_turns=2)

    assert turns_in(chunk) == 10
    assert len(chunk) == 20


def test_a_chunk_always_ends_on_the_model_passage():
    """So the verbatim window opens on an author turn, never mid-exchange."""
    chunk = plan_chunk(make_exchange(14), turns=10, keep_turns=2)
    assert chunk[-1].kind == "assistant"
    assert chunk[0].kind == "user"


def test_the_recent_turns_are_never_archived():
    nodes = make_exchange(6)
    chunk = plan_chunk(nodes, turns=4, keep_turns=2)

    assert turns_in(chunk) == 4
    assert {node.id for node in chunk}.isdisjoint({"u4", "a4", "u5", "a5"})


def test_a_partial_chunk_is_not_archived_automatically():
    """Live finding: partial chunks rewrite the summary block and lose the cache."""
    nodes = make_exchange(6)
    assert plan_chunk(nodes, turns=10, keep_turns=2) == []


def test_the_author_may_archive_a_short_chapter_deliberately():
    nodes = make_exchange(6)
    chunk = plan_chunk(nodes, turns=10, keep_turns=2, allow_partial=True)

    assert turns_in(chunk) == 4


def test_even_a_deliberate_chunk_needs_two_turns():
    assert plan_chunk(make_exchange(3), turns=10, keep_turns=2, allow_partial=True) == []


def test_nothing_to_archive_when_the_story_is_short():
    assert plan_chunk(make_exchange(2), turns=10, keep_turns=2) == []
    assert plan_chunk([], turns=10, keep_turns=2) == []
    assert plan_chunk(make_exchange(2), turns=10, keep_turns=2, allow_partial=True) == []


def test_consecutive_author_turns_travel_with_the_passage_that_answers_them():
    """A director aside is not an exchange of its own (§3.1)."""
    nodes = make_exchange(4)
    aside = Node(id="aside", kind="user", speaker_id="__director__", content="Nils is lying.")
    link_child(nodes[1], aside)
    link_child(aside, nodes[2])
    path = [nodes[0], nodes[1], aside, *nodes[2:]]

    chunk = plan_chunk(path, turns=2, keep_turns=1)

    assert turns_in(chunk) == 2
    assert "aside" in {node.id for node in chunk}
    assert chunk[-1].id == "a1"


# --- which summaries apply to which path ----------------------------------


def test_a_summary_replaces_the_run_of_nodes_it_covers():
    nodes = make_exchange(6)
    summary = summary_for(nodes[:4])

    split = split_path([summary], nodes)

    assert split.summaries == (summary,)
    assert [node.id for node in split.archived] == ["u0", "a0", "u1", "a1"]
    assert [node.id for node in split.verbatim] == ["u2", "a2", "u3", "a3", "u4", "a4", "u5", "a5"]


def test_summaries_chain_head_to_tail():
    nodes = make_exchange(8)
    first = summary_for(nodes[:4], "Chapter one.")
    second = summary_for(nodes[4:8], "Chapter two.")

    split = split_path([first, second], nodes)

    assert split.summaries == (first, second)
    assert len(split.archived) == 8
    assert [node.id for node in split.verbatim] == ["u4", "a4", "u5", "a5", "u6", "a6", "u7", "a7"]


def test_a_gap_in_the_chain_stops_it():
    """A summary that doesn't start where the last one ended is not applied."""
    nodes = make_exchange(8)
    first = summary_for(nodes[:2])
    detached = summary_for(nodes[4:6])

    split = split_path([first, detached], nodes)

    assert split.summaries == (first,)
    assert len(split.archived) == 2


def test_a_summary_from_another_branch_does_not_apply(cast):
    """§5.2: summaries are per-branch, keyed by the path they summarise."""
    trunk = make_exchange(3)
    fork = Node(
        id="fork",
        kind="assistant",
        speaker_id="__narrator__",
        content="A different answer entirely.",
        meta=NodeMeta(model="m"),
    )
    link_child(trunk[2], fork)  # sibling of a1, so the branch diverges at u1

    summarised_branch = summary_for(trunk[:4])
    other_branch = [trunk[0], trunk[1], trunk[2], fork]

    assert split_path([summarised_branch], trunk).summaries == (summarised_branch,)
    other = split_path([summarised_branch], other_branch)
    assert other.summaries == ()
    assert len(other.verbatim) == 4


def test_the_longest_matching_summary_wins():
    nodes = make_exchange(6)
    short = summary_for(nodes[:2], "Just the opening.")
    long = summary_for(nodes[:4], "The first two exchanges.")

    split = split_path([short, long], nodes)

    assert split.summaries == (long,)


def test_an_empty_summary_covers_nothing():
    nodes = make_exchange(2)
    split = split_path([Summary(content="orphaned")], nodes)
    assert split.summaries == ()
    assert len(split.verbatim) == 4


# --- staleness ------------------------------------------------------------


def test_editing_a_covered_node_marks_its_summary_stale():
    nodes = make_exchange(4)
    first = summary_for(nodes[:4])
    second = summary_for(nodes[4:])

    changed = mark_stale([first, second], "a1")

    assert changed == [first]
    assert first.stale is True
    assert second.stale is False
    assert stale_summaries([first, second]) == [first]


def test_a_summary_only_goes_stale_once():
    nodes = make_exchange(2)
    summary = summary_for(nodes)

    assert mark_stale([summary], "u0") == [summary]
    assert mark_stale([summary], "a0") == []
    assert summary.stale is True


def test_editing_verbatim_prose_stales_nothing():
    nodes = make_exchange(4)
    summary = summary_for(nodes[:4])
    assert mark_stale([summary], "u3") == []


def test_chapters_are_numbered_by_position():
    nodes = make_exchange(4)
    first, second = summary_for(nodes[:4]), summary_for(nodes[4:])
    assert chapter_number([first, second], second) == 2


# --- the summariser prompt ------------------------------------------------


def test_the_summariser_hears_only_about_characters_who_appear(cast):
    """Live finding: given the whole cast it reports who *didn't* turn up."""
    nodes = make_exchange(2, speaker_id="char-serrik")
    nodes[1].content = "Maela Orr lifted the lantern. Serrik said nothing."

    body = build_summary_messages(nodes, cast)[1].text

    assert "Maela Orr" in body
    # Captain Idris is three days' ride north and never named in the prose.
    assert "Idris" not in body
    assert "did not" not in body.lower()


def test_an_alias_counts_as_an_appearance(cast):
    nodes = make_exchange(1)
    nodes[1].content = "They still called him the Grey Wolf in the low town."

    assert "Serrik Vaun" in build_summary_messages(nodes, cast)[1].text


def test_the_summariser_is_told_the_prose_and_nothing_turn_specific(cast):
    nodes = make_exchange(2)
    messages = build_summary_messages(nodes, cast, target_words=120)

    assert [message.role for message in messages] == ["system", "user"]
    system = messages[0].text
    assert "never mention summarising" in system.lower()

    body = messages[1].text
    assert "Author turn 0." in body
    assert "The world answers, turn 1." in body
    assert "120 words" in body
    assert "Serrik Vaun" in body
    # No cache markers on a one-shot call.
    assert not any(part.cache_breakpoint for message in messages for part in message.parts)


def test_the_previous_chapter_travels_along_for_continuity(cast):
    nodes = make_exchange(2)
    previous = Summary(content="Serrik took the coin and said nothing.")

    messages = build_summary_messages(nodes, cast, previous=previous)

    assert "Serrik took the coin" in messages[1].text
    assert "do not repeat it" in messages[1].text


def test_the_summariser_keeps_who_was_there_for_anything_private():
    """The scene log covers recent scenes; once prose is archived, the chapter
    record is all that says who heard a private word."""
    messages = build_summary_messages(make_exchange(1), [])
    assert "in private, who was there" in " ".join(messages[0].text.split())
