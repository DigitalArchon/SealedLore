"""The Lore page says which always-on entries went in full with the turn, and
why (engine/lore_whole_back.py), under the entry's own title. Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.retrieval import Injection, RetrievalReport  # noqa: E402
from sealedlore.gui.lore_panel import LorePanel  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_page_says_an_entry_went_in_full_and_why(app):
    guild = LoreEntry(title="The Guild", content="short", always_on=True)
    whole = guild.model_copy(update={"title": "The Guild (in full)", "content": "long"})
    report = RetrievalReport(
        injected=(
            Injection(
                entry=whole, reason="in_full", tokens=40, rank=1, score=0.61, title="The Guild"
            ),
        ),
        tokens=40,
        standing=(guild,),
        condensed=(guild.id,),
    )
    panel = LorePanel()
    panel.show_retrieval(report)
    rows = [panel.injected.item(i).text() for i in range(panel.injected.count())]
    assert rows == [
        "The Guild — always on, sent with the world, condensed",
        "The Guild — in full this turn: the turn's match no. 1 (similarity 0.61)",
    ]
