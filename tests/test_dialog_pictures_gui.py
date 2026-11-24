"""The picture dialogs open before their pictures are read: Draw pictures for
the cards, Redraw in a style, Fill the gaps, the picture and video dialogs,
A set of expressions, Change a picture, a SillyTavern card's and a linked
character's changes decoded every picture while they were being built (a
large cast held the window), and the Cast and Lore pages' lists and strips
on every build. They read through `cast_board.Thumbs` now, a few a tick once
built, each picture once while its file stands as it is. Offscreen; nothing
is sent anywhere."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from sealedlore.engine.look_sheet import sheet_for  # noqa: E402
from sealedlore.gui import picture_cache, ref_images  # noqa: E402
from sealedlore.gui.cast_board import ASPECT, Thumbs  # noqa: E402
from sealedlore.gui.fill_gaps import FillGapsDialog  # noqa: E402
from sealedlore.gui.image_codec import encoded  # noqa: E402
from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.picture_cache import FIT  # noqa: E402
from sealedlore.gui.portraits_dialog import PortraitsDialog  # noqa: E402
from sealedlore.gui.restyle_dialog import THUMB, RestyleDialog  # noqa: E402
from sealedlore.gui.video_dialog import VideoDialog  # noqa: E402
from sealedlore.gui.video_jobs import model_entry  # noqa: E402
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef, RefUse  # noqa: E402
from sealedlore.storage.images import write_story_file  # noqa: E402
from sealedlore.storage.picture_store import MemoryPictures  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange, pictures_read  # noqa: E402
from tests.test_images import LISTING  # noqa: E402
from tests.test_videos import nanogpt_models  # noqa: E402

COLOURS = ("#cc2200", "#22aa44", "#2244cc")
LOOKS = (("winter coat", "neutral"), ("winter coat", "happy"), ("combat armour", "neutral"))


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def picture(colour: str, width: int = 300, height: int = 400) -> bytes:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(colour))
    return encoded(image, "PNG")


def colour_of(pixmap: QPixmap) -> str:
    image = pixmap.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2).name()


@pytest.fixture
def decodes(monkeypatch) -> list[int]:
    """Every picture decoded from bytes or a file, however it is asked for."""
    counted: list[int] = []
    real = ref_images.read_image

    def counting(make_reader, fmt):
        counted.append(1)
        return real(make_reader, fmt)

    monkeypatch.setattr(ref_images, "read_image", counting)
    return counted


@pytest.fixture
def thumbnails(monkeypatch) -> list[int]:
    """Every picture decoded for a thumbnail (`picture_cache`, which every
    `Thumbs` reads through)."""
    counted: list[int] = []
    real = picture_cache.pixmap_of

    def counting(data):
        counted.append(1)
        return real(data)

    monkeypatch.setattr(picture_cache, "pixmap_of", counting)
    return counted


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    """Serrik with three pictures of three colours, and nine more characters
    with one each."""
    serrik = cast[0]
    for index, (look, expression) in enumerate(LOOKS):
        serrik.reference_images.append(
            ImageRef(
                id=f"ref-{index}", file=f"images/refs/{index}.png", look=look, expression=expression
            )
        )
    for index in range(9):
        cast.append(
            Character(
                id=f"char-{index}",
                name=f"Guard {index}",
                reference_images=[ImageRef(id=f"g-{index}", file=f"images/refs/g{index}.png")],
            )
        )
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    for index, colour in enumerate(COLOURS):
        write_story_file(story.id, f"images/refs/{index}.png", picture(colour), tmp_path)
    for index in range(9):
        write_story_file(story.id, f"images/refs/g{index}.png", picture("#808080"), tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.image_models = LISTING["data"]
    window.config.image_models_fetched_at = utc_now_iso()
    window.config.video_models = [model_entry(m) for m in nanogpt_models().values()]
    window.config.video_models_fetched_at = utc_now_iso()
    window.config.video_endpoint.api_key = "video-key"
    window.open_story(story.id)
    pictures_read(window)
    yield window
    window.image_jobs.stop_all()
    window.image_jobs.wait_all()
    window.video_jobs.stop_all()
    window.video_jobs.wait_all()
    window.close()


def test_a_thumbnail_is_given_once_read_and_not_after_a_cancel(app, decodes):
    store = MemoryPictures(lambda _kind, _body: "")
    store.write("images/a.png", picture("#cc2200"))
    store.write("images/b.png", picture("#2244cc"))
    owner = QObject()
    thumbs = Thumbs(lambda: store, lambda: None, owner, shape=FIT)
    given: list[str] = []
    thumbs.fill("images/a.png", 30, 40, lambda pixmap: given.append(colour_of(pixmap)))
    assert given == [] and decodes == [], "read once the event loop runs, not here"
    wait_for(app, lambda: given)
    assert given == ["#cc2200"]
    # Read already: given at once, and not read again.
    thumbs.fill("images/a.png", 30, 40, lambda pixmap: given.append(colour_of(pixmap)))
    assert given == ["#cc2200", "#cc2200"] and len(decodes) == 1
    # Cancelled (the icons it would go to are made again): never given.
    thumbs.fill("images/b.png", 30, 40, lambda pixmap: given.append("late"))
    thumbs.cancel_fills()
    wait_for(app, lambda: len(decodes) == 2)
    app.processEvents()
    assert "late" not in given
    assert thumbs.now("images/b.png", 30, 40).size().toTuple() == (30, 40)
    assert len(decodes) == 2


def test_the_cards_dialog_opens_before_its_pictures_are_read(app, window: MainWindow, decodes):
    cast = list(window.session.cast)
    dialog = PortraitsDialog(window.session, cast, catalog=None, browse=None, parent=window)
    built = len(decodes)
    try:
        assert built == 0, "nothing read while the dialog was built"
        pictured = [row for row in dialog.rows if row.card.reference_images]
        assert len(pictured) == 10
        wait_for(app, lambda: all(not row.picture.pixmap().isNull() for row in pictured))
        assert len(decodes) == 10
        assert colour_of(pictured[0].picture.pixmap()) == COLOURS[0]
        bare = [row for row in dialog.rows if not row.card.reference_images]
        assert bare and all(row.picture.text() == "—" for row in bare)
    finally:
        # Its prompts are being written on a thread of its own.
        wait_for(app, lambda: not dialog.writing())
        dialog.reject()


def test_redraw_in_a_style_opens_before_its_pictures_are_read(app, window: MainWindow, decodes):
    serrik = window.session.cast[0]
    dialog = RestyleDialog(window.session, [serrik], catalog=None, browse=None, parent=window)
    assert decodes == []
    labels = [
        label
        for label in dialog.findChildren(QLabel)
        if label.minimumSize() == QSize(THUMB, int(THUMB * ASPECT))
    ]
    assert len(labels) == 3
    wait_for(app, lambda: len(decodes) == 3)
    app.processEvents()
    shown = sorted(colour_of(label.pixmap()) for label in labels if not label.pixmap().isNull())
    assert shown == sorted(COLOURS)
    dialog.reject()


def test_fill_the_gaps_reads_each_source_once_after_it_opens(app, window: MainWindow, decodes):
    serrik = window.session.cast[0]
    gaps = list(sheet_for(serrik).drawable())
    sources = {gap.source.file for gap in gaps if gap.source is not None}
    assert len([gap for gap in gaps if gap.source is not None]) > len(sources), (
        "gaps share their sources"
    )
    dialog = FillGapsDialog(window.session, serrik, gaps, catalog=None, browse=None, parent=window)
    assert decodes == []
    wait_for(app, lambda: len(decodes) == len(sources))
    for _ in range(5):
        app.processEvents()
    assert len(decodes) == len(sources)
    dialog.reject()


def test_the_picture_dialog_reads_each_reference_once(app, window: MainWindow, decodes):
    dialog = ImageDialog(window.session, parent=window)
    uses = [choice.use for choice in window.session.image_ref_choices()][:3]
    assert [use.file for use in uses] == [f"images/refs/{i}.png" for i in range(3)]
    dialog._set_refs(uses)
    dialog._set_refs(list(reversed(uses)))
    assert decodes == []
    wait_for(app, lambda: len(decodes) == 3)
    for _ in range(5):
        app.processEvents()
    icons = [dialog.refs.item(i).icon() for i in range(dialog.refs.count())]
    assert [colour_of(icon.pixmap(88, 88)) for icon in icons] == list(reversed(COLOURS))
    # Moved about again: nothing read again.
    dialog._set_refs(uses)
    app.processEvents()
    assert len(decodes) == 3
    assert colour_of(dialog.refs.item(0).icon().pixmap(88, 88)) == COLOURS[0]
    dialog.reject()


def test_the_video_dialog_shows_the_frame_chosen_last(app, window: MainWindow, decodes):
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    combo = dialog.start_choice

    def choose(file: str) -> None:
        index = next(
            i
            for i in range(combo.count())
            if isinstance(combo.itemData(i), RefUse) and combo.itemData(i).file == file
        )
        combo.setCurrentIndex(index)
        dialog._show_thumb(combo, dialog.start_thumb)

    choose("images/refs/0.png")
    wait_for(app, lambda: not dialog.start_thumb.pixmap().isNull())
    assert colour_of(dialog.start_thumb.pixmap()) == COLOURS[0]
    # Chosen one after another before they are read: the last one stays.
    choose("images/refs/2.png")
    choose("images/refs/0.png")
    choose("images/refs/1.png")
    wait_for(app, lambda: len(decodes) == 3)
    for _ in range(5):
        app.processEvents()
    assert colour_of(dialog.start_thumb.pixmap()) == COLOURS[1]
    dialog.reject()


def icon_colour(item) -> str:
    return colour_of(item.icon().pixmap(item.listWidget().iconSize()))


def test_the_cast_page_is_built_before_its_pictures_are_read(
    app, window: MainWindow, tmp_path: Path, thumbnails
):
    """The list's icons and the strip's thumbnails were decoded on every
    build of the page (each card's first picture, every one of the chosen
    card's others); now each once, and again only once its file is written."""
    panel = window.cast_panel
    serrik = window.session.cast[0]
    panel._icons._thumbs.forget()
    panel.pictures._thumbs.forget()
    panel.set_cast(window.session.cast)
    panel.select_character(serrik.id)
    assert thumbnails == [], "nothing decoded while the page is built"
    pictures_read(window)
    items = {
        panel.list.item(row).data(Qt.UserRole): panel.list.item(row)
        for row in range(panel.list.count())
        if panel.list.item(row).data(Qt.UserRole)
    }
    assert icon_colour(items[serrik.id]) == COLOURS[0]
    assert icon_colour(items["char-3"]) == "#808080"
    strip = panel.pictures.list
    assert [icon_colour(strip.item(row)) for row in range(strip.count())] == list(COLOURS[1:])
    read = len(thumbnails)
    assert read == 10 + 2  # each card's first, and Serrik's two others
    panel.set_cast(window.session.cast)
    panel.select_character("char-3")
    panel.select_character(serrik.id)
    pictures_read(window)
    assert len(thumbnails) == read, "an unchanged picture is never decoded again"
    # His portrait's file written again: that one alone is read again.
    write_story_file(window.session.story.id, "images/refs/0.png", picture("#ffcc00"), tmp_path)
    panel.refresh_pictures()
    pictures_read(window)
    assert len(thumbnails) == read + 1
    item = next(
        panel.list.item(row)
        for row in range(panel.list.count())
        if panel.list.item(row).data(Qt.UserRole) == serrik.id
    )
    assert icon_colour(item) == "#ffcc00"
