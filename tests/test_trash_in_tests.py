"""The suite's own guard against tests filling the desktop's trash (conftest's
`_trash_of_its_own`): whatever a test deletes goes to a trash of the run's own,
gone from where it was, as the app expects."""

from __future__ import annotations

from pathlib import Path

import pytest

QtCore = pytest.importorskip("PySide6.QtCore")


def test_a_deleted_folder_goes_to_the_runs_own_trash(tmp_path, tmp_path_factory):
    folder = tmp_path / "a story"
    folder.mkdir()
    (folder / "story.json").write_text("{}", encoding="utf-8")

    trashed, where = QtCore.QFile.moveToTrash(str(folder))

    assert trashed and not folder.exists()
    assert Path(where).is_relative_to(tmp_path_factory.getbasetemp() / "trash")
    assert (Path(where) / "story.json").read_text(encoding="utf-8") == "{}"


def test_something_that_isnt_there_isnt_trashed(tmp_path):
    assert QtCore.QFile.moveToTrash(str(tmp_path / "nothing")) == (False, "")
