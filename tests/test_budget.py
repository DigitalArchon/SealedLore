"""Budget accounting and whole-message trimming. See §5."""

from __future__ import annotations

from sealedlore.engine.budget import BudgetReport, HistoryItem, select_history_items
from sealedlore.models.node import Node


def item(node_id: str, tokens: int) -> HistoryItem:
    node = Node(id=node_id, kind="user", speaker_id="s", content=node_id)
    return HistoryItem(node=node, text=node_id, tokens=tokens)


def test_report_utilization_and_thresholds():
    report = BudgetReport(budget=1000, total=899)
    assert report.over_budget is False
    assert report.warning is False

    warning = BudgetReport(budget=1000, total=900)
    assert warning.warning is True
    assert warning.over_budget is False

    over = BudgetReport(budget=1000, total=1001)
    assert over.over_budget is True
    assert over.utilization > 1.0


def test_report_handles_a_zero_budget():
    assert BudgetReport(budget=0, total=50).utilization == 0.0


def test_everything_fits_when_there_is_room():
    items = [item("a", 10), item("b", 10)]
    kept, dropped = select_history_items(items, 100)
    assert [i.node.id for i in kept] == ["a", "b"]
    assert dropped == []


def test_newest_turns_survive_and_oldest_are_dropped():
    items = [item("a", 40), item("b", 40), item("c", 40)]
    kept, dropped = select_history_items(items, 100)
    assert [i.node.id for i in kept] == ["b", "c"]
    assert [i.node.id for i in dropped] == ["a"]


def test_dropping_stops_at_the_first_node_that_does_not_fit():
    # "a" would fit in the 90 tokens left after "c", but reaching it means
    # skipping "b", and a hole mid-scene reads worse than a shorter window.
    items = [item("a", 5), item("b", 200), item("c", 10)]
    kept, dropped = select_history_items(items, 100)
    assert [i.node.id for i in kept] == ["c"]
    assert [i.node.id for i in dropped] == ["a", "b"]


def test_no_room_drops_everything_rather_than_splitting_a_message():
    items = [item("a", 10), item("b", 10)]
    kept, dropped = select_history_items(items, 0)
    assert kept == []
    assert [i.node.id for i in dropped] == ["a", "b"]

    kept, dropped = select_history_items(items, -50)
    assert kept == []
    assert len(dropped) == 2


def test_a_single_oversized_node_is_never_truncated():
    items = [item("huge", 500)]
    kept, dropped = select_history_items(items, 100)
    assert kept == []
    assert [i.node.id for i in dropped] == ["huge"]
