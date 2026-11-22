"""Play and the window, looked over before 3.5.0b1 (the window's side;
tests/test_review_3_5_play.py has the engine's). Offscreen, mock clients only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui import lorebooks_page  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.lorebooks import load_lorebooks  # noqa: E402
from tests.test_background_queue import _window  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- W1: a kept lorebook is condensed on Your characters' own client ---------------

VALLEY = {
    "entries": {
        "0": {
            "uid": 0,
            "comment": "The Valley",
            "constant": True,
            "key": [],
            "content": "the valley " * 200,
        }
    }
}


def test_a_kept_lorebook_is_condensed_on_its_own_client_never_the_storys(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """Lorebooks → Condense the always-on entries… took the story's client:
    its Stop cut the story's passage, the story's Stop cut it, and Your
    characters' session kept the story's client from then on."""
    path = tmp_path / "valley.json"
    path.write_text(json.dumps(VALLEY), encoding="utf-8")
    window = _window(tmp_path / "data", story, cast, ["A passage of the story's."])
    monkeypatch.setattr(
        "sealedlore.gui.file_dialogs.open_file_names", lambda *a, **k: ([str(path)], "")
    )
    monkeypatch.setattr(window, "_ask_lorebook_arrival", lambda *a, **k: "keep", raising=False)
    window.show_lorebooks(True)
    window.lorebooks_page.action_requested.emit(lorebooks_page.IMPORT, "")
    (stored,) = window._lorebooks()
    own = MockChatProvider(["A valley."])
    window._collection_session().provider = own
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    window.lorebooks_page.action_requested.emit(lorebooks_page.CONDENSE, stored.id)

    theirs = window._current_provider()
    assert window._collection_session().provider is own
    assert own is not theirs
    assert len(own.requests) == 1 and not theirs.requests
    (saved,) = load_lorebooks(tmp_path / "data").books
    assert list(saved.condensed.values()) == ["A valley."]
    window.close()
