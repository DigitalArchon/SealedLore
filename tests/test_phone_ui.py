"""Phone access as the author sees it: Settings → Phone, File → Phone access
and Phone devices…, the status bar's sign, the pairing sheet and its
question, the stories shared, Share with phone in the Stories list. Tor is
tests/data/fake_tor.py throughout."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from sealedlore.gui import window_phone  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.phone_dialog import PairSheet, PhoneDevicesDialog  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import PhoneDevice  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_tor_service import fake_tor, wait_for  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def place(monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix="slu"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(folder / "run"))
    tor, _ = fake_tor(folder)
    monkeypatch.setenv("SEALEDLORE_TOR", str(tor))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def said(monkeypatch):
    """Message boxes answered, and what they said kept."""
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda parent, title, text: told.append(text))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: told.append(args[2]) or QMessageBox.Yes
    )
    return told


def window_with(place: Path, story, cast, *, stories=()) -> MainWindow:
    root = place / "data"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=root)
    for each in stories:
        save_story_bundle(StoryBundle(story=each, cast=cast), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.show()
    return window


def turned_on(app, window: MainWindow) -> None:
    window.phone_action.setChecked(True)
    wait_for(app, lambda: window.phone_step == "ready")


# --- Settings → Phone --------------------------------------------------------------------


def test_settings_says_whether_phone_access_starts_with_the_app(app, place, story):
    window = window_with(place, story, [])
    dialog = SettingsDialog(window.config, None, window)
    names = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert names[-2:] == ["Private", "Phone"]
    assert not dialog.phone_on_launch.isChecked() and dialog.phone_notify.isChecked()
    dialog.phone_on_launch.setChecked(True)
    dialog.phone_notify.setChecked(False)
    dialog._save()
    assert window.config.phone.start_on_launch and not window.config.phone.notify_connect
    window.close()


def test_phone_access_starts_with_the_app_only_when_chosen(app, place, story):
    window = window_with(place, story, [])
    window._maybe_start_phone()
    assert window.phone_step == "off"
    window.config.phone.start_on_launch = True
    window._maybe_start_phone()
    wait_for(app, lambda: window.phone_step == "ready")
    assert window.phone_action.isChecked()
    window.close()


# --- File → Phone access, and the status bar ---------------------------------------------


def test_the_menu_turns_it_on_and_off_and_the_status_bar_shows_it(app, place, story):
    window = window_with(place, story, [])
    assert not window.phone_status.isVisible()
    turned_on(app, window)
    assert window.phone_status.isVisible()
    assert window.phone_status.text() == "Phone: ready"
    assert "Phone access is on" in window.phone_status.toolTip()
    window.phone_action.setChecked(False)
    assert window.phone_step == "off" and not window.phone_status.isVisible()
    assert window.minimumSizeHint().width() < 1920
    window.close()


def test_without_tor_it_says_why_and_stays_off(app, place, story, monkeypatch, said):
    monkeypatch.delenv("SEALEDLORE_TOR")
    monkeypatch.setenv("PATH", str(place / "nothing"))
    window = window_with(place, story, [])
    window.phone_action.setChecked(True)
    assert said == [window_phone.NO_TOR]
    assert not window.phone_action.isChecked() and window.phone_step == "off"
    window.close()


def test_a_phone_connecting_is_said_once(app, place, story):
    window = window_with(place, story, [])
    window.config.phone.devices = [PhoneDevice(id="p1", name="Pixel", token_sha256="x")]
    turned_on(app, window)
    hub = window._phone.api.hub
    hub.subscribe("p1", lambda data: True, lambda: None)
    window._phone_stream_opened("p1")
    assert window.statusBar().currentMessage() == "Phone connected: Pixel"
    assert window.phone_status.text() == "Phone: 1 connected"
    window.statusBar().clearMessage()
    hub.subscribe("p1", lambda data: True, lambda: None)  # its second stream, a reconnect
    window._phone_stream_opened("p1")
    assert window.statusBar().currentMessage() == ""
    window.close()


# --- Phone devices… ------------------------------------------------------------------------


def test_pairing_from_the_dialog(app, place, story):
    window = window_with(place, story, [])
    window.open_phone_devices()
    dialog = window._phone_dialog
    assert isinstance(dialog, PhoneDevicesDialog)
    assert "Phone access is off" in dialog.status.text()
    assert not dialog.pair_button.isEnabled()
    turned_on(app, window)
    assert dialog.pair_button.isEnabled()
    assert window._phone.onion in dialog.address.text()

    dialog.pair()
    sheet = dialog.pair_sheet
    assert isinstance(sheet, PairSheet)
    assert sheet.link == f"http://{window._phone.onion}/#pair={window._phone.pairing.code}"
    assert len(sheet.code.matrix) >= 21

    claim = window._phone.pairing.claim(window._phone.pairing.code, "Pixel 9")
    window._phone.api.backend.claimed(claim)
    box = next(w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible())
    assert claim.check in box.text() and "Pixel 9" in box.text()
    allow = next(b for b in box.buttons() if b.text() == "Allow")
    allow.click()
    app.processEvents()
    assert [d.name for d in window.config.phone.devices] == ["Pixel 9"]
    assert dialog.devices.rowCount() == 1 and dialog.devices.item(0, 0).text() == "Pixel 9"

    sheet.close()
    app.processEvents()
    assert window._phone.pairing.code is None  # closing the sheet spent the code
    window.close()


def test_a_refused_phone_is_not_kept(app, place, story):
    window = window_with(place, story, [])
    turned_on(app, window)
    code = window._phone.pairing.open()
    claim = window._phone.pairing.claim(code, "Someone else's phone")
    window._phone_claim(claim)
    box = next(w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible())
    next(b for b in box.buttons() if b.text() == "Refuse").click()
    app.processEvents()
    assert window.config.phone.devices == []
    assert window._phone.pairing.poll(claim.id) == ("refused", "")
    window.close()


def test_stories_are_shared_from_the_dialog_and_a_visual_game_never(app, place, story, cast):
    """A Visual Story left in the data folder (an app of its own now) can't
    be opened, so it isn't offered to the phones."""
    window = window_with(place, story, cast)
    game = place / "data" / "stories" / "game"
    game.mkdir()
    (game / "story.json").write_text('{"id": "game", "title": "With a map", "mode": "visual"}')
    window.open_phone_devices()
    dialog = window._phone_dialog
    items = {
        dialog.stories.item(i).data(Qt.UserRole)[0]: dialog.stories.item(i)
        for i in range(dialog.stories.count())
    }
    assert "game" not in items
    plain = items[story.id]
    plain.setCheckState(Qt.Checked)
    assert window.config.phone.shared_stories == [story.id]
    plain.setCheckState(Qt.Unchecked)
    assert window.config.phone.shared_stories == []
    window.close()


def test_revoking_from_the_dialog(app, place, story, said):
    window = window_with(place, story, [])
    window.config.phone.devices = [PhoneDevice(id="p1", name="Pixel", token_sha256="x")]
    window.open_phone_devices()
    dialog = window._phone_dialog
    revoke = dialog.devices.cellWidget(0, 2 + 1)
    assert isinstance(revoke, QPushButton)
    revoke.click()
    assert window.config.phone.devices == [] and dialog.devices.rowCount() == 0
    assert "Pixel" in said[0]
    window.close()


def test_a_new_address_forgets_every_phone(app, place, story, said, memory_keychain):
    window = window_with(place, story, [])
    turned_on(app, window)
    first = window._phone.onion
    window.config.phone.devices = [PhoneDevice(id="p1", name="Pixel", token_sha256="x")]
    assert window.phone_new_address() is None
    wait_for(app, lambda: window.phone_step == "ready")
    assert window._phone.onion != first
    assert window.config.phone.devices == []
    window.close()


# --- the Stories list ------------------------------------------------------------------------


def test_share_with_phone_in_the_stories_list(app, place, story, cast):
    mapped = story.model_copy(
        update={
            "id": "mapped",
            "mode": "visual",
        }
    )
    window = window_with(place, story, cast, stories=[mapped])
    panel = window.stories
    names = [a.text() for a in panel.menu_for(story.id).actions()]
    assert "Share with phone" not in names  # phone access is off
    turned_on(app, window)
    share = next(a for a in panel.menu_for(story.id).actions() if a.text() == "Share with phone")
    assert "Share with phone" not in [a.text() for a in panel.menu_for("mapped").actions()]
    share.setChecked(True)
    assert window.config.phone.shared_stories == [story.id]
    row = next(
        panel.list.item(i)
        for i in range(panel.list.count())
        if panel.list.item(i).data(Qt.UserRole) == story.id
    )
    from sealedlore.gui.stories import _StoryRow

    title, meta = _StoryRow._parts(panel.list.indexFromItem(row))
    assert meta[0].endswith("on your phones")
    assert "on your phones" not in row.text()  # the mark is drawn, not the item's text
    window.close()


def test_the_sign_and_the_dialog_say_when_tor_is_out_of_date(app, place, story, monkeypatch):
    tor, _ = fake_tor(place, "obsolete")
    monkeypatch.setenv("SEALEDLORE_TOR", str(tor))
    window = window_with(place, story, [])
    notices: list[str] = []
    real = window.statusBar().showMessage
    monkeypatch.setattr(
        window.statusBar(),
        "showMessage",
        lambda text, *rest: (notices.append(text), real(text, *rest))[1],
    )
    turned_on(app, window)
    wait_for(app, lambda: "out of date" in window.phone_status.text())
    assert window.phone_status.text() == "Phone: ready · tor out of date"
    assert "Update the tor at" in window.phone_status.toolTip()
    assert "out of date" in window.phone_words and window.phone_step == "ready"
    assert len([n for n in notices if "out of date" in n]) == 1, "said once, not at every step"
    window.close()


def test_a_tor_that_is_current_changes_nothing_on_the_sign(app, place, story):
    window = window_with(place, story, [])
    turned_on(app, window)
    assert window.phone_status.text() == "Phone: ready"
    assert "out of date" not in window.phone_status.toolTip()
    assert "out of date" not in window.phone_words
    window.close()


def test_the_status_bar_counts_only_the_shared_stories_that_are_here(app, place, story):
    """Live, a config copied from another machine said "3 stories shared" over
    a data folder holding none of them. The ids stay listed (a story may come
    back from the Trash); only those here are counted."""
    window = window_with(place, story, [])
    window.config.phone.shared_stories = [story.id, "ghost-1", "ghost-2"]
    turned_on(app, window)
    assert "1 story shared" in window.phone_status.toolTip()
    assert window.phone_shared("ghost-1"), "kept, not counted"
    window.close()
