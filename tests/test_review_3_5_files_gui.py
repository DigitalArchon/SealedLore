"""What the review of 3.5.0b1 found in files from strangers, the window's
side (Nov 22 2026): each test fails on 0f289da. Every file is made here."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui import window_collection  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage import collection  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    yield window
    window.close()


def boxes(monkeypatch) -> list[str]:
    """Every message box answered Ok, its words kept."""
    said: list[str] = []

    def exec_(box) -> int:
        said.append(f"{box.windowTitle()}: {box.text()} {box.informativeText()}")
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return said


# --- F2: a full disk while a backup of characters is put back -----------------------


def test_a_log_that_cant_be_written_is_said_and_the_characters_are_back(
    app, window, tmp_path: Path, monkeypatch
):
    marcus = collection.kept("Marcus")
    path = tmp_path / "theirs.sealedlore-characters.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        archive.writestr("characters.json", json.dumps([marcus.model_dump(mode="json")]))
        archive.writestr("api_log.jsonl", json.dumps({"id": "e1", "kind": "image_response"}) + "\n")

    def full(*args, **kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(window_collection, "append_api_log", full)
    said = boxes(monkeypatch)
    window.restore_collection(path)
    assert [c.name for c in window._collection_session().cast] == ["Marcus"]
    assert len(said) == 1 and "No space left" in said[0] and "put back" in said[0]
