"""Pictures decoded and scaled once (gui/picture_cache.py): a rebuild of the
transcript read and decoded every picture it built again, every time. Kept
by the file as it is now, a picture is read again only once its file has
been written since, or for another size or shape."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import picture_cache  # noqa: E402
from sealedlore.gui.image_codec import encoded  # noqa: E402
from sealedlore.gui.picture_cache import COVER, FIT, WITHIN, PictureCache  # noqa: E402
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def decodes(monkeypatch) -> list[int]:
    counted: list[int] = []
    real = picture_cache.pixmap_of

    def counting(data):
        counted.append(len(data))
        return real(data)

    monkeypatch.setattr(picture_cache, "pixmap_of", counting)
    return counted


def coloured(colour: str, width: int = 200, height: int = 100, fmt: str = "PNG") -> bytes:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(colour))
    return encoded(image, fmt)


def colour_of(pixmap) -> str:
    return pixmap.toImage().pixelColor(pixmap.width() // 2, pixmap.height() // 2).name()


def memory() -> MemoryPictures:
    return MemoryPictures(lambda _kind, _body: "")


def test_a_picture_asked_for_again_is_not_decoded_again(app, tmp_path, decodes):
    store = DiskPictures("story-1", tmp_path)
    store.write("images/a.png", coloured("red"))
    cache = PictureCache()
    first = cache.picture(store, "images/a.png", 100, 100)
    # The store is made anew each time it is asked (StorySession.pictures).
    again = cache.picture(DiskPictures("story-1", tmp_path), "images/a.png", 100, 100)
    assert len(decodes) == 1
    assert again.cacheKey() == first.cacheKey(), "the same pixmap, not a copy"
    assert first.size().toTuple() == (100, 50) and colour_of(first) == "#ff0000"


def test_a_file_written_again_is_decoded_again(app, tmp_path, decodes):
    """The same name, the same size and dimensions, written at once: an edit,
    or a card's file under the name of one taken off."""
    store = DiskPictures("story-1", tmp_path)
    red, blue = coloured("red", fmt="BMP"), coloured("blue", fmt="BMP")
    assert len(blue) == len(red)
    store.write("images/a.bmp", red)
    cache = PictureCache()
    assert colour_of(cache.picture(store, "images/a.bmp", 100, 100)) == "#ff0000"
    store.write("images/a.bmp", blue)
    assert colour_of(cache.picture(store, "images/a.bmp", 100, 100)) == "#0000ff"
    assert len(decodes) == 2


def test_a_memory_chats_file_written_again_is_decoded_again(app, decodes):
    store = memory()
    store.write("images/a.png", coloured("red"))
    cache = PictureCache()
    assert colour_of(cache.picture(store, "images/a.png", 100, 100)) == "#ff0000"
    cache.picture(store, "images/a.png", 100, 100)
    assert len(decodes) == 1
    store.write("images/a.png", coloured("blue"))
    assert colour_of(cache.picture(store, "images/a.png", 100, 100)) == "#0000ff"
    assert len(decodes) == 2
    # Another chat's file under the same name is its own.
    other = memory()
    other.write("images/a.png", coloured("lime"))
    assert colour_of(cache.picture(other, "images/a.png", 100, 100)) == "#00ff00"


def test_another_size_or_shape_is_read_again(app, tmp_path, decodes):
    store = DiskPictures("story-1", tmp_path)
    store.write("images/a.png", coloured("red", 200, 100))
    cache = PictureCache()
    assert cache.picture(store, "images/a.png", 100, 100, WITHIN).size().toTuple() == (100, 50)
    assert cache.picture(store, "images/a.png", 50, 50, WITHIN).size().toTuple() == (50, 25)
    # A thumbnail fills its box's width, enlarged if need be; a cell is filled.
    assert cache.picture(store, "images/a.png", 400, 400, FIT).size().toTuple() == (400, 200)
    assert cache.picture(store, "images/a.png", 40, 60, COVER).size().toTuple() == (40, 60)
    # Never enlarged in the transcript.
    assert cache.picture(store, "images/a.png", 400, 400, WITHIN).size().toTuple() == (200, 100)
    assert len(decodes) == 5
    cache.picture(store, "images/a.png", 100, 100, WITHIN)
    assert len(decodes) == 5


def test_a_missing_file_is_a_null_picture_until_it_is_there(app, tmp_path, decodes):
    store = DiskPictures("story-1", tmp_path)
    cache = PictureCache()
    assert cache.picture(store, "images/a.png", 100, 100).isNull()
    assert cache.picture(store, "", 100, 100).isNull()
    assert cache.picture(None, "images/a.png", 100, 100).isNull()
    store.write("images/a.png", coloured("red"))
    assert colour_of(cache.picture(store, "images/a.png", 100, 100)) == "#ff0000"


def test_a_photo_held_sideways_is_kept_upright(app):
    from tests.test_image_meta import dirty_jpeg

    store = memory()
    # Stored 64 wide and 48 high, to be shown turned: 48 wide and 64 high.
    store.write("images/a.jpg", dirty_jpeg(orientation=6))
    shown = PictureCache().picture(store, "images/a.jpg", 40, 32)
    assert shown.size().toTuple() == (24, 32)


def test_only_the_files_listed_are_kept(app, decodes):
    store = memory()
    for name in "ab":
        store.write(f"images/{name}.png", coloured("red", 10, 10))
    cache = PictureCache()
    for name in "ab":
        cache.picture(store, f"images/{name}.png", 10, 10, FIT)
        cache.picture(store, f"images/{name}.png", 5, 5, FIT)
    cache.keep({"images/b.png"})
    assert len(cache) == 2 and cache.held_bytes == 10 * 10 * 4 + 5 * 5 * 4
    assert cache.peek(store, "images/a.png", 10, 10, FIT) is None
    assert cache.peek(store, "images/b.png", 5, 5, FIT) is not None


def test_what_is_kept_stays_within_its_budget(app, decodes):
    store = memory()
    for name in "abc":
        store.write(f"images/{name}.png", coloured("red", 100, 100))
    # Room for two 100 x 100 pictures (40,000 bytes each).
    cache = PictureCache(budget=90_000)
    for name in "abc":
        cache.picture(store, f"images/{name}.png", 100, 100)
    assert len(cache) == 2 and cache.held_bytes == 80_000
    # The least recently used went: "a"; "b" and "c" are kept.
    cache.picture(store, "images/b.png", 100, 100)
    cache.picture(store, "images/c.png", 100, 100)
    assert len(decodes) == 3
    # Asking for "b" makes it the newest, so "c" goes for "a".
    cache.picture(store, "images/b.png", 100, 100)
    cache.picture(store, "images/a.png", 100, 100)
    assert len(decodes) == 4
    cache.picture(store, "images/b.png", 100, 100)
    assert len(decodes) == 4
    # Larger than the whole budget: shown, but not kept.
    store.write("images/big.png", coloured("red", 200, 200))
    assert not cache.picture(store, "images/big.png", 200, 200).isNull()
    assert len(cache) == 2 and cache.held_bytes == 80_000
    cache.forget("images/a.png")
    assert len(cache) == 1 and cache.held_bytes == 40_000
    cache.forget()
    assert len(cache) == 0 and cache.held_bytes == 0


def test_peek_reads_nothing(app, decodes):
    store = memory()
    store.write("images/a.png", coloured("red"))
    cache = PictureCache()
    assert cache.peek(store, "images/a.png", 100, 100) is None
    assert decodes == []
    kept = cache.picture(store, "images/a.png", 100, 100)
    assert cache.peek(store, "images/a.png", 100, 100).cacheKey() == kept.cacheKey()


def test_a_closed_memory_chat_s_files_have_no_stamp(app):
    store = memory()
    store.write("images/a.png", coloured("red"))
    assert store.stamp("images/a.png") is not None
    store.remove("images/a.png")
    assert store.stamp("images/a.png") is None
    store.write("images/a.png", coloured("red"))
    store.close()
    assert store.stamp("images/a.png") is None
