"""SealedLore inside firejail (Nov 8 2026). Under firejail the desktop's own
file dialog took the whole app down: the portal refuses a sandboxed
process, Qt fell back to GTK's dialog, and GTK aborted when firejail
wouldn't let its icon loader start `bwrap`. Inside firejail Qt uses its own
dialogs. Checked live on Ubuntu 26.04 under firejail 0.9.72; here, only
that the switch is made when, and only when, firejail says it is there."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.app import in_firejail, own_dialogs_in_firejail  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def native_dialogs_afterwards():
    yield
    QCoreApplication.setAttribute(Qt.AA_DontUseNativeDialogs, False)


def test_firejail_is_known_by_what_it_says_in_container():
    assert in_firejail({"container": "firejail"})
    assert not in_firejail({})
    # A Flatpak's portal works: it keeps the desktop's dialogs.
    assert not in_firejail({"container": "flatpak"})


def test_inside_firejail_every_dialog_is_qts_own(app, native_dialogs_afterwards):
    assert not own_dialogs_in_firejail({"container": "flatpak"})
    assert not QCoreApplication.testAttribute(Qt.AA_DontUseNativeDialogs)
    assert own_dialogs_in_firejail({"container": "firejail"})
    assert QCoreApplication.testAttribute(Qt.AA_DontUseNativeDialogs)
