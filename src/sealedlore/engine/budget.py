"""Budget accounting for the assembled prompt.

Trimming only ever drops whole nodes from the oldest end of the verbatim
window — never part of a message. Dropped nodes are reported rather than
discarded quietly: Phase 4's chunked archival turns them into summaries, and
until then the caller can see that archival is overdue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sealedlore.models.node import Node

SOFT_WARNING_RATIO = 0.9


@dataclass(frozen=True)
class HistoryItem:
    node: Node
    text: str
    tokens: int


@dataclass(frozen=True)
class BudgetReport:
    budget: int
    total: int
    by_section: dict[str, int] = field(default_factory=dict)

    @property
    def utilization(self) -> float:
        if self.budget <= 0:
            return 0.0
        return self.total / self.budget

    @property
    def over_budget(self) -> bool:
        return self.total > self.budget

    @property
    def warning(self) -> bool:
        return self.utilization >= SOFT_WARNING_RATIO


def select_history_items(
    items: list[HistoryItem], available_tokens: int
) -> tuple[list[HistoryItem], list[HistoryItem]]:
    """Keep the newest whole nodes that fit; return (kept, dropped-oldest-first).

    Walks from the newest backwards so recency survives, and stops at the first
    node that would not fit rather than skipping it to squeeze in an older one —
    a gap in the middle of a scene reads worse than a shorter window.
    """
    if available_tokens <= 0:
        return [], list(items)

    kept_reversed: list[HistoryItem] = []
    used = 0
    cutoff = 0
    for position in range(len(items) - 1, -1, -1):
        item = items[position]
        if used + item.tokens > available_tokens:
            cutoff = position + 1
            break
        kept_reversed.append(item)
        used += item.tokens
    else:
        cutoff = 0

    kept_reversed.reverse()
    return kept_reversed, list(items[:cutoff])
