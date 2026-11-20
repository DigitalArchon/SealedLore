"""The turn's best matches (engine/retrieval.py `turn_ranking`): ranked on the
author's turn, by similarity fused with keyword scoring over each entry's
whole text, the top `lore_retrieval_k` (5) sent.

On a 320-entry lorebook, ranked by the last three messages and the turn
together, the entries a turn needed came 31st-62nd at the median, and Silent
Spellcasting 116th-226th for "without chanting"; ranked on the turn and
fused, 18 of 23 were in the top five (Nov 20 2026, offline)."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.engine.bm25 import stem, stemmed
from sealedlore.engine.lore_pick import choose_picked
from sealedlore.engine.retrieval import select_lore, turn_query, turn_ranking
from sealedlore.models.config import Config
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node
from sealedlore.storage.repository import load_config, load_config_or_recover


def tokens(text: str) -> int:
    return len(text.split())


SILENT = LoreEntry(
    id="silent",
    title="Silent Spellcasting",
    content="Casting with no incantation at all, shaping mana by understanding alone.",
    keywords=["Silent", "Incantationless", "Chantless"],
)
WATER = LoreEntry(
    id="water",
    title="Water Magic",
    content="Water, ice and mist; freezing water is taught at the second rank.",
    keywords=["Water"],
)
ROA = LoreEntry(
    id="roa",
    title="The Citadel of Roa",
    content="A walled town on the frontier, its lord a patron of tutors.",
    keywords=["Roa"],
)
LORE = [SILENT, WATER, ROA]


def test_endings_come_off_so_one_word_finds_another():
    assert stem("chanting") == stem("chantless") == stem("chants") == "chant"
    assert stem("ice") == "ice" and stem("roads") == "road"
    assert stemmed("Without chanting, I freeze it") == ["without", "chant", "freeze"]


def test_ranked_on_the_turn_whole_entries_and_all():
    # No keyword of Silent Spellcasting is in the turn; its words are.
    ranked = turn_ranking(LORE, "I make a water ball without chanting and try to freeze it")
    assert [entry_id for entry_id, _ in ranked][:2] == ["water", "silent"] or [
        entry_id for entry_id, _ in ranked
    ][:2] == ["silent", "water"]
    assert "roa" not in [entry_id for entry_id, _ in ranked]


def test_similarity_and_words_are_fused():
    similarity = {"roa": 0.9, "silent": 0.5, "water": 0.2}
    ranked = turn_ranking(LORE, "freezing water", similarity, threshold=0.45)
    ids = [entry_id for entry_id, _ in ranked]
    # Water by its words, Roa and Silent by their meaning; Water's similarity
    # is under the floor, so it comes by its words alone.
    assert set(ids) == {"water", "roa", "silent"}
    assert dict(ranked)["water"] == 0.2 and dict(ranked)["roa"] == 0.9
    # Nothing reaches an entry that neither matches nor is similar enough.
    assert turn_ranking(LORE, "a bird sings", {"roa": 0.1}, threshold=0.45) == []


def test_the_turn_alone_unless_it_says_too_little():
    passage = Node(
        id="p",
        kind="assistant",
        speaker_id="narrator",
        content="The innkeeper sets down a bowl of stew.",
    )
    assert turn_query("I ask about the treaty.", [passage]) == "I ask about the treaty."
    assert turn_query("I nod.", [passage]) == "The innkeeper sets down a bowl of stew.\n\nI nod."
    assert turn_query("I nod.", []) == "I nod."


def test_the_best_matches_go_in_their_order_and_the_next_are_near_misses():
    ranked = [("roa", 0.6), ("silent", None), ("water", 0.5)]
    report = select_lore(
        LORE, keyword_text="", ranked=ranked, k=2, token_cap=2_000, count_tokens=tokens
    )
    assert [(i.entry.id, i.reason, i.rank) for i in report.injected] == [
        ("roa", "matched", 1),
        ("silent", "matched", 2),
    ]
    assert report.injected[0].describe() == "the turn's match no. 1 (similarity 0.60)"
    assert report.injected[1].describe() == "the turn's match no. 2"
    assert [(e.id, s) for e, s in report.near_misses] == [("water", 0.5)]


def test_the_picker_takes_the_turns_matches_after_its_picks():
    report = choose_picked(
        LORE,
        ["roa"],
        turn_text="",
        token_cap=2_000,
        count_tokens=tokens,
        turn_hits=[("silent", 0.5), ("roa", 0.7)],
    )
    assert [(i.entry.id, i.reason) for i in report.injected] == [
        ("roa", "picked"),
        ("silent", "matched"),
    ]


def test_five_go_by_default_and_an_old_settings_file_moves_from_three(tmp_path: Path):
    assert Config().lore_retrieval_k == 5
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"lore_retrieval_k": 3}), encoding="utf-8")
    assert load_config(tmp_path).lore_retrieval_k == 5
    assert load_config_or_recover(tmp_path)[0].lore_retrieval_k == 5
    # A number chosen is kept; so is 3 in a file of this format.
    path.write_text(json.dumps({"lore_retrieval_k": 4}), encoding="utf-8")
    assert load_config(tmp_path).lore_retrieval_k == 4
    path.write_text(json.dumps({"format_version": 2, "lore_retrieval_k": 3}), encoding="utf-8")
    assert load_config(tmp_path).lore_retrieval_k == 3


def test_what_the_turn_names_comes_before_its_matches():
    """Live, five long matches filled the room ahead of "Saint", which the
    turn asking about Saint-rank magic named (Nov 20 2026)."""
    saint = LoreEntry(id="saint", title="Saint", content="The fourth rank.", keywords=["Saint"])
    big = [LoreEntry(id=f"b{i}", title=f"Rank {i}", content="rank magic " * 400) for i in range(3)]
    entries = [*big, saint]
    ranked = [(entry.id, None) for entry in big] + [("saint", None)]
    turn = "What is Saint-rank water magic?"
    report = select_lore(
        entries,
        keyword_text=turn,
        ranked=ranked,
        k=5,
        token_cap=1_200,
        count_tokens=tokens,
        named_first=True,
    )
    assert report.injected[0].entry.id == "saint" and report.injected[0].reason == "named"
    # Matched by reaching back over earlier exchanges, it waits its turn.
    report = select_lore(
        entries, keyword_text=turn, ranked=ranked, k=5, token_cap=1_200, count_tokens=tokens
    )
    assert report.injected[0].entry.id == "b0"
