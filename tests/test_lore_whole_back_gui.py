"""The Lore page says which always-on entries sent condensed had parts of
their full text sent with the turn, how many and why
(engine/lore_whole_back.py), under the entry's own title. Offscreen."""

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


def test_the_page_says_what_came_from_the_full_entry_and_why(app):
    guild = LoreEntry(title="The Guild", content="short", always_on=True)
    parts = guild.model_copy(
        update={"title": "The Guild (from the full entry)", "content": "[…]\n\nlong"}
    )
    report = RetrievalReport(
        injected=(
            Injection(
                entry=parts,
                reason="in_full",
                tokens=40,
                rank=1,
                score=0.61,
                whole=400,
                title="The Guild",
                matches=((1, 0.61), (3, 0.55)),
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
        "The Guild — from the full entry: 2 parts, the turn's matches no. 1 and 3 "
        "(similarity 0.61, 0.55); 40 of its 400 tokens",
    ]
