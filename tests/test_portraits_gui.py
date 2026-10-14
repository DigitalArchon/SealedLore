"""Portraits in the window: the dialog (every prompt shown, sent as it stands),
the Cast page's large portrait with the other pictures small, and the offer
when a story starts. Offscreen; the mock provider."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.engine.tokens import TokenEstimator, fallback_counter  # noqa: E402
from sealedlore.gui.cast_panel import CastPanel  # noqa: E402
from sealedlore.gui.portraits_dialog import PortraitsDialog  # noqa: E402
from sealedlore.gui.start_dialog import StartDialog  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.picture_store import MemoryPictures  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402


def _png() -> bytes:
    """A 2x2 grey PNG, valid to the byte."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = b"".join(b"\0" + b"\x80\x80\x80" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = _png()


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.01)


def test_the_dialog_shows_every_prompt_and_draws_them_as_they_stand(
    app, tmp_path: Path, story, cast
):
    names = [c.name for c in cast]
    written = json.dumps(
        {"portraits": [{"name": n, "prompt": f"A face for {i}."} for i, n in enumerate(names)]}
    )
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(),
        MockChatProvider([written]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    dialog = PortraitsDialog(session, cast, catalog=None, browse=None)
    assert not dialog.draw_button.isEnabled()  # nothing to approve yet
    wait_for(app, lambda: not dialog.writing())
    app.processEvents()
    assert [row.prompt.toPlainText() for row in dialog.rows] == [
        f"A face for {i}." for i in range(len(cast))
    ]
    assert dialog.draw_button.isEnabled()
    assert dialog.model.text() == Config().portrait_model == "flux-2-klein-9b"
    # The author edits one and leaves another out.
    dialog.rows[0].prompt.setPlainText("  My own words.\n")
    dialog.rows[1].tick.setChecked(False)
    requests = dialog.requests()
    assert [r.prompt for r in requests][0] == "  My own words.\n"
    assert all(r.portrait_of for r in requests) and len(requests) == len(cast) - 1
    assert dialog.draw_button.text() == f"Draw {len(cast) - 1}"
    dialog.reject()


def test_the_cast_page_shows_the_portrait_large_and_the_others_small(app):
    store = MemoryPictures(lambda _kind, _body: "")
    store.write("images/refs/a.png", PNG)
    store.write("images/refs/b.png", PNG)
    jane = Character(
        name="Jane",
        reference_images=[
            ImageRef(id="a", file="images/refs/a.png", caption="portrait"),
            ImageRef(id="b", file="images/refs/b.png", caption="in her coat"),
        ],
    )
    john = Character(name="John")
    panel = CastPanel(lambda: store)
    panel.set_cast([jane, john])
    panel.select_character(jane.id)
    assert not panel.portrait.picture.isHidden() and panel.portrait.empty.isHidden()
    # The portrait is not repeated among the others.
    assert panel.pictures.list.count() == 1
    assert panel.pictures.list.item(0).text() == "in her coat"
    # Make portrait: the other picture goes first.
    panel.pictures.list.setCurrentRow(0)
    panel.pictures.portrait_button.click()
    assert [r.id for r in jane.reference_images] == ["b", "a"]
    assert panel.portrait.caption.text() == "in her coat"
    # Someone with none: a place for one, and the ways to get one.
    asked: list[str] = []
    panel.portrait_requested.connect(asked.append)
    panel.select_character(john.id)
    assert panel.portrait.picture.isHidden() and not panel.portrait.empty.isHidden()
    panel.portrait.draw_button.click()
    assert asked == [john.id]


def test_start_offers_portraits_only_for_those_without(app, story, cast):
    plain = StartDialog(story, cast)
    assert plain.portraits is None and not plain.portraits_wanted()
    offered = StartDialog(story, cast, without_portraits=cast[:1])
    assert offered.portraits is not None and "(1)" in offered.portraits.text()
    offered.portraits.setChecked(True)
    assert offered.portraits_wanted()
