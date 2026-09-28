"""Lore selection: semantic, keyword, always-on, and the order they land in. See §7."""

from __future__ import annotations

import pytest

from sealedlore.engine.retrieval import (
    content_hash,
    cosine_scores,
    embedding_text,
    held_back,
    keyword_matches,
    keyword_window,
    query_text,
    render_entry,
    select_lore,
)
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node


def entry(title: str, content: str = "Some lore.", **kwargs) -> LoreEntry:
    slug = title.lower().replace(" ", "-")
    return LoreEntry(id=f"lore-{slug}", title=title, content=content, **kwargs)


def words(text: str) -> int:
    """A stand-in token counter: one token per word, so caps are easy to reason about."""
    return len(text.split())


# --- similarity -----------------------------------------------------------


def test_cosine_scores_rank_by_direction_not_length():
    query = [1.0, 0.0]
    scores = cosine_scores(query, [[2.0, 0.0], [0.0, 3.0], [-1.0, 0.0]])
    assert scores == pytest.approx([1.0, 0.0, -1.0])


def test_a_zero_vector_scores_zero_rather_than_nan():
    assert cosine_scores([1.0, 0.0], [[0.0, 0.0]]) == [0.0]


def test_mismatched_dimensions_are_an_error_not_a_wrong_answer():
    with pytest.raises(ValueError, match="different dimensions"):
        cosine_scores([1.0, 0.0], [[1.0, 0.0, 0.0]])


def test_no_entries_means_no_scores():
    assert cosine_scores([1.0], []) == []


# --- keywords -------------------------------------------------------------


def test_keywords_match_whole_words_only():
    accord = entry("The Accord", keywords=["accord"])
    assert keyword_matches("they signed the Accord at dawn", accord)
    assert not keyword_matches("according to Maela, nobody signed", accord)


def test_keyword_matching_ignores_case_and_handles_phrases():
    keep = entry("Calder Keep", keywords=["Calder Keep"])
    assert keyword_matches("the road to calder keep was flooded", keep)


def test_punctuation_does_not_hide_a_keyword():
    assert keyword_matches('"Accord!" he shouted.', entry("A", keywords=["accord"]))


# --- the query ------------------------------------------------------------


def test_the_query_is_the_turn_plus_the_last_k_turns():
    history = [
        Node(id=f"n{index}", kind="user", speaker_id="s", content=f"turn {index}")
        for index in range(5)
    ]
    query = query_text("what I say now", history, turns=2)

    assert "turn 3" in query and "turn 4" in query
    assert "turn 2" not in query
    assert query.endswith("what I say now")


def test_the_query_survives_an_empty_history():
    assert query_text("alone", [], turns=3) == "alone"


# --- selection ------------------------------------------------------------


def test_semantic_hits_above_the_threshold_are_injected():
    entries = [entry("Accord"), entry("Bridge"), entry("Cellar")]
    scores = {"lore-accord": 0.9, "lore-bridge": 0.5, "lore-cellar": 0.1}

    report = select_lore(
        entries, scores=scores, k=3, threshold=0.4, token_cap=1000, count_tokens=words
    )

    assert [i.entry.title for i in report.injected] == ["Accord", "Bridge"]
    assert report.injected[0].reason == "semantic"
    assert report.injected[0].score == pytest.approx(0.9)


def test_top_k_caps_the_semantic_hits():
    entries = [entry("A"), entry("B"), entry("C")]
    scores = {"lore-a": 0.9, "lore-b": 0.8, "lore-c": 0.7}

    report = select_lore(
        entries, scores=scores, k=2, threshold=0.0, token_cap=1000, count_tokens=words
    )

    assert len(report.injected) == 2


def test_always_on_entries_come_in_regardless_of_the_query():
    entries = [entry("Standing Order", always_on=True), entry("Other")]

    report = select_lore(entries, query="nothing relevant", token_cap=1000, count_tokens=words)

    assert [i.entry.title for i in report.injected] == ["Standing Order"]
    assert report.injected[0].reason == "always_on"


def test_keyword_hits_merge_with_semantic_ones():
    entries = [entry("Accord"), entry("Bridge", keywords=["bridge"])]
    scores = {"lore-accord": 0.9, "lore-bridge": 0.0}

    report = select_lore(
        entries,
        query="we burned the bridge",
        scores=scores,
        threshold=0.4,
        token_cap=1000,
        count_tokens=words,
    )

    reasons = {i.entry.title: i.reason for i in report.injected}
    assert reasons == {"Accord": "semantic", "Bridge": "keyword"}


def test_an_entry_qualifying_twice_appears_once():
    both = entry("Accord", keywords=["accord"], always_on=True)
    report = select_lore(
        [both],
        query="the accord",
        scores={"lore-accord": 0.99},
        token_cap=1000,
        count_tokens=words,
    )

    assert len(report.injected) == 1
    # always_on is the honest label: it would have been injected anyway.
    assert report.injected[0].reason == "always_on"


def test_disabled_entries_never_appear():
    entries = [
        entry("Off", always_on=True, enabled=False),
        entry("Also off", keywords=["bridge"], enabled=False),
    ]
    report = select_lore(
        entries,
        query="the bridge",
        scores={"lore-also-off": 0.99},
        token_cap=1000,
        count_tokens=words,
    )
    assert report.injected == ()


def test_priority_sorts_ahead_of_score():
    entries = [entry("Low", priority=0), entry("High", priority=5)]
    scores = {"lore-low": 0.9, "lore-high": 0.5}

    report = select_lore(entries, scores=scores, threshold=0.0, token_cap=1000, count_tokens=words)

    assert [i.entry.title for i in report.injected] == ["High", "Low"]


def test_the_token_cap_drops_entries_and_says_which():
    entries = [
        entry("Short", "one two three", priority=2),
        entry("Fat", " ".join(["word"] * 50), priority=1),
        entry("Also short", "four five six", priority=0),
    ]
    scores = {"lore-short": 0.9, "lore-fat": 0.9, "lore-also-short": 0.9}

    report = select_lore(entries, scores=scores, threshold=0.0, token_cap=20, count_tokens=words)

    # The fat entry is skipped, not allowed to shut out what follows it.
    assert [i.entry.title for i in report.injected] == ["Short", "Also short"]
    assert [i.entry.title for i in report.dropped] == ["Fat"]
    assert report.tokens <= 20


def test_near_misses_are_reported_so_the_threshold_can_be_tuned():
    """bge-m3's scores cluster, so the panel has to show what sat just under."""
    entries = [entry("In"), entry("Just out"), entry("Far out")]
    scores = {"lore-in": 0.62, "lore-just-out": 0.58, "lore-far-out": 0.31}

    report = select_lore(entries, scores=scores, threshold=0.60, token_cap=1000, count_tokens=words)

    assert [i.entry.title for i in report.injected] == ["In"]
    assert [(e.title, round(s, 2)) for e, s in report.near_misses] == [
        ("Just out", 0.58),
        ("Far out", 0.31),
    ]


def test_an_injected_entry_is_not_also_a_near_miss():
    both = entry("Accord", keywords=["accord"])
    report = select_lore(
        [both], query="the accord", scores={"lore-accord": 0.1}, token_cap=1000, count_tokens=words
    )

    assert [i.entry.title for i in report.injected] == ["Accord"]
    assert report.near_misses == ()


def test_a_keyword_only_report_says_embeddings_were_not_used():
    report = select_lore(
        [entry("Bridge", keywords=["bridge"])],
        query="the bridge",
        token_cap=1000,
        count_tokens=words,
        fallback_reason="no endpoint",
    )

    assert report.used_embeddings is False
    assert report.fallback_reason == "no endpoint"
    assert report.semantic_count == 0


def test_the_rendered_entry_matches_what_the_prompt_sends():
    from sealedlore.engine.rules import render_lore_block

    one = entry("Accord", "They signed it at dawn.")
    assert render_entry(one) in render_lore_block([one])


# --- cache keys -----------------------------------------------------------


def test_the_hash_covers_title_and_content():
    first = entry("Accord", "They signed it at dawn.")
    renamed = entry("Accord", "They signed it at dusk.")
    assert content_hash(embedding_text(first)) != content_hash(embedding_text(renamed))


def test_whitespace_alone_does_not_invalidate_a_vector():
    assert content_hash("the accord\n") == content_hash("  the accord  ")


def test_keywords_match_the_keyword_text_not_the_whole_query():
    """The session passes the author's turn: keywords over the recent prose
    fired on a third of a 100-entry lorebook every turn."""
    entries = [entry("Bridge", keywords=["bridge"]), entry("Accord", keywords=["accord"])]

    report = select_lore(
        entries,
        query="Earlier passage about the bridge.\n\nI ask about the accord.",
        keyword_text="I ask about the accord.",
        token_cap=1000,
        count_tokens=words,
    )

    assert [i.entry.title for i in report.injected] == ["Accord"]


def test_keyword_hits_are_kept_by_similarity_under_the_cap():
    """By title, the cap kept whichever keyword hits sorted first."""
    entries = [
        entry("Aardvark", "one two three", keywords=["aardvark"]),
        entry("Zeppelin", "four five six", keywords=["zeppelin"]),
    ]
    scores = {"lore-aardvark": 0.2, "lore-zeppelin": 0.4}

    report = select_lore(
        entries,
        keyword_text="an aardvark on a zeppelin",
        scores=scores,
        k=0,
        token_cap=6,
        count_tokens=words,
    )

    assert [i.entry.title for i in report.injected] == ["Zeppelin"]
    assert [i.entry.title for i in report.dropped] == ["Aardvark"]
    assert report.injected[0].describe() == "keyword match"


def test_the_keyword_window_is_the_turn_while_the_turn_names_something():
    history = [
        Node(id="u1", kind="user", speaker_id="a", content="I try the bridge."),
        Node(id="a1", kind="assistant", speaker_id="n", content="It holds."),
    ]
    entries = [entry("Bridge", keywords=["bridge"]), entry("Accord", keywords=["accord"])]

    assert keyword_window(entries, "The accord.", history) == ("The accord.", 0)
    text, reach = keyword_window(entries, "I wait.", history)
    assert reach == 1 and "bridge" in text and text.endswith("I wait.")
    assert keyword_window(entries, "I wait.", history, exchanges=0) == ("I wait.", 0)


def test_held_back_counts_only_the_authors_messages():
    planned = entry("The Harrow envoy", keywords=["envoy"], until_mentioned=True)
    storyteller = Node(id="a1", kind="assistant", speaker_id="n", content="The envoy comes.")
    author = Node(id="u1", kind="user", speaker_id="a", content="Where is the envoy?")

    assert held_back([planned], [storyteller]) == {"lore-the-harrow-envoy"}
    assert held_back([planned], [storyteller, author]) == set()
    assert held_back([entry("Plain")], []) == set()
