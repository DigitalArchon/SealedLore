"""Pictures of one's own: cropped as they come in, to any part or to the exact
shape the app uses (gui/crop_dialog.py, engine/picture_shapes.py), and kept
with nothing hidden left in them, whichever way they come in.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog  # noqa: E402

from sealedlore.engine.picture_shapes import (  # noqa: E402
    BACKGROUND,
    FREE,
    ISOMETRIC,
    ORIGINAL,
    PICTURE,
    PORTRAIT,
    picture_shapes,
    size_ratio,
)
from sealedlore.gui import crop_dialog  # noqa: E402
from sealedlore.gui.crop_dialog import (  # noqa: E402
    MIN_SIDE,
    CropDialog,
    cropped_picture,
    fit_rect,
    from_corner,
    from_edge,
    moved,
    whole_pixels,
)
from sealedlore.gui.ref_images import clean_incoming, image_of  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.storage.image_meta import strip_metadata  # noqa: E402
from tests.test_image_meta import GIF, dirty_jpeg, dirty_png, leftovers  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "SampleStories"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- the shapes -----------------------------------------------------------------------


def _listed(model: str, *sizes: str) -> dict:
    """A model as the config keeps the image listing."""
    return {"id": model, "supported_parameters": {"resolutions": list(sizes)}}


def test_each_shape_is_the_size_its_model_draws():
    """A picture brought in should match one drawn: a portrait is the tall
    size the portrait model lists, a lore picture its landscape size. An
    isometric view is the shape of the plan it is laid over on the map."""
    config = Config(
        portrait_model="flux",
        image_models=[
            _listed("flux", "1024*1024", "1280*720", "1024*1536", "1536*1024"),
            _listed("qwen", "1024x1024", "1024x768", "576x1024"),
        ],
    )
    shapes = {s.key: s for s in picture_shapes(config, background=(935, 660))}
    assert [s.key for s in picture_shapes(config)][:2] == [FREE, ORIGINAL]
    assert (shapes[PORTRAIT].width, shapes[PORTRAIT].height) == (2, 3)
    assert (shapes[PICTURE].width, shapes[PICTURE].height) == (16, 9)  # widest up to 1360
    assert (shapes[ISOMETRIC].width, shapes[ISOMETRIC].height) == (5, 3)
    # The background covers the story area, so its shape is the area's own.
    assert shapes[BACKGROUND].ratio == pytest.approx(935 / 660)
    assert "935 × 660" in shapes[BACKGROUND].label
    assert shapes[FREE].ratio is None and shapes[ORIGINAL].ratio is None


def test_without_a_listing_the_shapes_are_the_app_s_own_defaults():
    shapes = {s.key: s for s in picture_shapes(None)}
    assert shapes[PORTRAIT].ratio == pytest.approx(2 / 3)  # what portrait_size asks for unlisted
    assert shapes[ISOMETRIC].ratio == pytest.approx(5 / 3)
    assert shapes[BACKGROUND].ratio == pytest.approx(16 / 9)
    assert size_ratio("1k") is None and size_ratio("1536*1024") == (3, 2)


# --- the geometry ---------------------------------------------------------------------


def test_a_fixed_shape_starts_as_the_largest_frame_centred_in_the_picture():
    assert fit_rect(400, 300, None) == (0, 0, 400, 300)
    x, y, w, h = fit_rect(400, 300, 2 / 3)
    assert (w, h) == (200, 300) and (x, y) == (100, 0)
    x, y, w, h = fit_rect(400, 300, 16 / 9)
    assert w == 400 and h == pytest.approx(225) and y == pytest.approx(37.5)


def test_a_corner_drag_keeps_the_shape_and_the_picture():
    # Dragged far past the picture, a 2:3 frame stops at its edge and stays 2:3.
    x, y, w, h = from_corner((100, 0), (900, 900), 2 / 3, 400, 300)
    assert (x, y) == (100, 0) and w / h == pytest.approx(2 / 3) and y + h <= 300
    # Up and to the left of the anchor, it grows that way.
    x, y, w, h = from_corner((300, 300), (250, 100), 1.0, 400, 300)
    assert (x + w, y + h) == (300, 300) and w == h == 200
    # Free, it follows the pointer exactly, and never under MIN_SIDE.
    assert from_corner((10, 10), (110, 60), None, 400, 300) == (10, 10, 100, 50)
    assert from_corner((10, 10), (11, 11), None, 400, 300)[2:] == (MIN_SIDE, MIN_SIDE)


def test_a_moved_frame_stays_in_the_picture_and_a_side_moves_alone():
    assert moved((0, 0, 100, 100), 500, -50, 400, 300) == (300, 0, 100, 100)
    assert from_edge((50, 50, 100, 100), "right", (300, 0), 400, 300) == (50, 50, 250, 100)
    assert from_edge((50, 50, 100, 100), "left", (500, 0), 400, 300)[2] == MIN_SIDE
    # A narrow frame at the edge, its side pulled in: moved back into the picture.
    assert from_edge((390, 0, 10, 100), "right", (395, 50), 400, 300) == (384, 0, MIN_SIDE, 100)


def _inside(rect, width=400, height=300) -> bool:
    x, y, w, h = rect
    return x >= 0 and y >= 0 and x + w <= width and y + h <= height


def test_a_frame_never_collapses_nor_leaves_the_picture():
    """Live (the review): a corner dragged past the picture's edge from a
    frame already on it kept a 1-pixel sliver, and a frame drawn from the
    margin kept a pixel from outside the picture."""
    # The bottom-right corner of the whole picture dragged past its left edge.
    rect = from_corner((0, 0), (-50, 120), None, 400, 300)
    assert rect[2] >= MIN_SIDE and _inside(rect)
    # A 2:3 frame on the top edge, a corner dragged up past it.
    rect = from_corner((100, 0), (160, -80), 2 / 3, 400, 300)
    assert rect[2] >= MIN_SIDE and rect[2] / rect[3] == pytest.approx(2 / 3) and _inside(rect)
    # Drawn from the margin right of the picture, outward.
    rect = from_corner((400, 100), (450, 150), None, 400, 300)
    assert rect[2] >= MIN_SIDE and _inside(rect)
    # In whole pixels, never a pixel past the edge.
    pixels = whole_pixels((400, 10, 0, 50), 400, 300)
    assert pixels.left() == 399 and pixels.right() <= 399 and pixels.width() == 1


# --- the dialog -----------------------------------------------------------------------


def _image(width: int = 400, height: int = 300) -> QImage:
    image = QImage(width, height, QImage.Format_RGB32)
    for y in range(height):
        for x in range(width):
            image.setPixel(x, y, 0xFF000000 | (x % 256) << 16 | (y % 256) << 8)
    return image


@pytest.mark.real_crop
def test_the_dialog_offers_every_shape_and_crops_to_the_one_chosen(app):
    dialog = CropDialog(_image(), picture_shapes(None), PORTRAIT)
    assert dialog.shape_key() == PORTRAIT
    keys = [dialog.shape.itemData(i) for i in range(dialog.shape.count())]
    assert keys[:2] == [FREE, ORIGINAL] and {PORTRAIT, PICTURE, BACKGROUND, ISOMETRIC} <= set(keys)
    crop = dialog.view.crop()
    assert crop.width() / crop.height() == pytest.approx(2 / 3, abs=0.01)
    assert "Keeps 200 × 300 pixels" in dialog.size_label.text()
    assert "small" in dialog.size_label.text()  # under 512 on its longer side
    dialog.shape.setCurrentIndex(dialog.shape.findData(ORIGINAL))
    assert dialog.view.crop() == QRect(0, 0, 400, 300)
    dialog.close()


@pytest.mark.real_crop
def test_the_frame_is_dragged_with_the_mouse(app):
    """Inside the frame it moves; across the picture outside it, a new one is drawn."""
    dialog = CropDialog(_image(), picture_shapes(None), FREE)
    dialog.show()
    view = dialog.view
    view.set_rect((0, 0, 100, 100))
    scale, left, top = view._placement()

    def at(x: float, y: float) -> QPoint:
        return QPoint(round(left + x * scale), round(top + y * scale))

    QTest.mousePress(view, Qt.LeftButton, Qt.NoModifier, at(50, 50))
    QTest.mouseMove(view, at(150, 100))
    QTest.mouseRelease(view, Qt.LeftButton, Qt.NoModifier, at(150, 100))
    x, y, w, h = view.rect_in_picture()
    assert (round(x), round(y), w, h) == (100, 50, 100, 100)
    QTest.mousePress(view, Qt.LeftButton, Qt.NoModifier, at(300, 200))
    QTest.mouseMove(view, at(380, 280))
    QTest.mouseRelease(view, Qt.LeftButton, Qt.NoModifier, at(380, 280))
    x, y, w, h = view.rect_in_picture()
    assert abs(x - 300) <= 2 and abs(y - 200) <= 2 and abs(w - 80) <= 2 and abs(h - 80) <= 2
    dialog.close()


def _answer(monkeypatch, *, shape: str | None = None, rect=None, whole=False, cancel=False):
    """Answer the crop dialog as the author would: a shape, a frame, the
    whole picture, or Cancel. Remembers what it was offered."""
    seen: dict = {}

    def run(dialog: CropDialog) -> int:
        seen["initial"] = dialog.shape_key()
        seen["title"] = dialog.windowTitle()
        if cancel:
            return QDialog.Rejected
        if shape is not None:
            dialog.shape.setCurrentIndex(dialog.shape.findData(shape))
        if rect is not None:
            dialog.view.set_rect(rect)
        dialog.whole = whole
        return QDialog.Accepted

    monkeypatch.setattr(CropDialog, "exec", run)
    return seen


@pytest.mark.real_crop
def test_a_crop_is_kept_upright_stripped_and_only_the_part_chosen(app, tmp_path, monkeypatch):
    source = tmp_path / "photo.jpg"
    source.write_bytes(dirty_jpeg(orientation=6, width=64, height=48))  # stands 48 x 64
    # A frame that only fits the picture standing up: lying down (64 x 48),
    # its 58-pixel bottom would be cut to 40.
    _answer(monkeypatch, rect=(8, 8, 30, 50))
    data, suffix = cropped_picture(None, source)
    assert suffix == ".jpg" and leftovers(data) == [] and strip_metadata(data) == data
    picture = image_of(data)
    assert (picture.width(), picture.height()) == (30, 50)
    # The whole picture is kept as it came, stripped, nothing re-encoded.
    _answer(monkeypatch, whole=True)
    whole, _ = cropped_picture(None, source)
    assert leftovers(whole) == [] and whole == strip_metadata(source.read_bytes())
    _answer(monkeypatch, cancel=True)
    assert cropped_picture(None, source) is None
    (tmp_path / "notes.txt").write_text("not a picture")
    with pytest.raises(ValueError):
        cropped_picture(None, tmp_path / "notes.txt")


# --- every way in -----------------------------------------------------------------------


@pytest.fixture
def window(app, tmp_path: Path, monkeypatch):
    """Doomsville with a map, imported as a sample is, opened and not begun."""
    from sealedlore.engine.plot_md import parse_plot_markdown
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.gui.ref_images import load_plot_pictures
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario
    from sealedlore.storage.world_art import load_sidecar

    plot = SAMPLES / "Doomsville with a map.md"
    parsed = parse_plot_markdown(plot.read_text(encoding="utf-8"))
    assert load_plot_pictures(parsed, plot.parent) == []
    bundle = bundle_from_scenario(parsed.scenario)
    bundle.world_art, files = load_sidecar(plot)
    bundle.pending_files.update(files)
    save_story_bundle(bundle, root=tmp_path / "data")
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    window.open_story(bundle.story.id)
    yield window
    window.close()


def _pick(monkeypatch, *paths: Path) -> None:
    files = [str(p) for p in paths]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (files[0], ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: (files, ""))


def _dirty(tmp_path: Path) -> tuple[Path, Path]:
    jpeg, png = tmp_path / "camera.jpg", tmp_path / "edited.png"
    jpeg.write_bytes(dirty_jpeg())
    png.write_bytes(dirty_png())
    return jpeg, png


@pytest.mark.real_crop
def test_every_way_a_picture_comes_in_keeps_nothing_hidden_and_offers_its_shape(
    window, tmp_path, monkeypatch
):
    """Cast and Lore pages, the portrait, the picture dialog, the background,
    the plot editor and the isometric view: each offers the crop at its own
    shape, and what is kept has no camera data, comments or anything after
    the image, cropped or whole."""
    from sealedlore.gui.image_dialog import ImageDialog

    session = window.session
    store = session.pictures
    jpeg, png = _dirty(tmp_path)

    def clean(relative: str) -> None:
        data = store.read(relative)
        assert data is not None and leftovers(data) == [], relative

    # The Cast page: two at once, each offered as a portrait, one cropped.
    character = session.cast[0]
    window.cast_panel.pictures.set_refs(character.reference_images)
    before = len(character.reference_images)
    seen = _answer(monkeypatch, rect=(0, 0, 20, 30))
    _pick(monkeypatch, jpeg, png)
    window.cast_panel.pictures._add()
    assert seen["initial"] == PORTRAIT and "2 of 2" in seen["title"]
    added = character.reference_images[before:]
    assert len(added) == 2
    for ref in added:
        clean(ref.file)
    # The portrait itself, whole.
    seen = _answer(monkeypatch, whole=True)
    window.cast_panel.pictures.add_portrait()
    assert seen["initial"] == PORTRAIT
    clean(character.reference_images[0].file)

    # The Lore page offers a lore picture's shape.
    entry = session.bundle.lore[0]
    window.lore_panel.pictures.set_refs(entry.reference_images)
    seen = _answer(monkeypatch, whole=True)
    _pick(monkeypatch, png)
    window.lore_panel.pictures._add()
    assert seen["initial"] == PICTURE
    clean(entry.reference_images[-1].file)

    # The picture dialog's Add a picture from disk…: free.
    seen = _answer(monkeypatch, rect=(4, 4, 20, 20))
    _pick(monkeypatch, jpeg)
    dialog = ImageDialog(session, parent=window)
    dialog._add_from_disk()
    assert seen["initial"] == FREE
    clean(session.story.reference_images[-1].file)
    dialog.close()

    # The background: the story area's own shape.
    seen = _answer(monkeypatch)
    window._set_background_from(jpeg)
    assert seen["initial"] == BACKGROUND
    clean(session.story.background_image)
    area = window.transcript.viewport().size()
    picture = image_of(store.read(session.story.background_image))
    assert picture.width() / picture.height() == pytest.approx(
        area.width() / area.height(), abs=0.05
    )


@pytest.mark.real_crop
def test_the_plot_editor_keeps_a_clean_crop_beside_the_file(app, tmp_path, monkeypatch):
    from sealedlore.gui.plot_pictures import PlotPictureStrip

    folder = tmp_path / "story"
    folder.mkdir()
    jpeg, _png = _dirty(tmp_path)
    strip = PlotPictureStrip(lambda: folder, lambda: True, crop=PORTRAIT)
    pictures: list = []
    strip.set_pictures(pictures, "Jane")
    seen = _answer(monkeypatch, rect=(0, 0, 24, 36))
    _pick(monkeypatch, jpeg)
    strip._add()
    assert seen["initial"] == PORTRAIT and len(pictures) == 1
    data = (folder / pictures[0].file).read_bytes()
    assert leftovers(data) == [] and image_of(data).size().toTuple() == (24, 36)
    strip.deleteLater()


def test_a_scenario_s_or_backup_s_pictures_come_in_clean(app):
    """Storage strips what it can in place and keeps the rest as it came;
    the window re-encodes the rest (a GIF with a comment) before it is
    written. Records and videos are storage's."""
    files = {
        "images/refs/a.jpg": dirty_jpeg(),
        "images/refs/b.gif": GIF,
        "images.json": b'{"comment": "SECRET-COMMENT"}',
        "images/videos/v.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16,
    }
    cleaned = clean_incoming(files)
    assert leftovers(cleaned["images/refs/a.jpg"]) == []
    assert leftovers(cleaned["images/refs/b.gif"]) == []
    assert not image_of(cleaned["images/refs/b.gif"]).isNull()
    assert cleaned["images.json"] == files["images.json"]
    assert cleaned["images/videos/v.mp4"] == files["images/videos/v.mp4"]


def test_crops_use_the_window_s_config_and_never_keep_the_window(app, tmp_path):
    import gc
    import weakref

    from sealedlore.gui.main_window import MainWindow

    window = MainWindow(root=tmp_path / "data", use_mock=True)
    window.config.portrait_model = "tall"
    window.config.image_models = [_listed("tall", "768x1024")]
    portrait = next(s for s in crop_dialog.shapes() if s.key == PORTRAIT)
    assert (portrait.width, portrait.height) == (3, 4)
    window.close()

    # Held weakly: an owner gone is the defaults, whoever else held a window.
    class Owner:
        config = window.config

    owner = Owner()
    crop_dialog.use_config(owner, lambda o: o.config)
    gone = weakref.ref(owner)
    assert next(s for s in crop_dialog.shapes() if s.key == PORTRAIT).height == 4
    del owner
    gc.collect()
    assert gone() is None
    portrait = next(s for s in crop_dialog.shapes() if s.key == PORTRAIT)
    assert (portrait.width, portrait.height) == (2, 3)  # what portrait_size asks unlisted


@pytest.mark.real_crop
def test_a_frame_too_small_to_keep_cannot_be_cropped(app):
    dialog = CropDialog(_image(), crop_dialog.shapes())
    dialog.view.set_rect((10, 10, MIN_SIDE - 6, 40))
    assert not dialog.crop_button.isEnabled()
    dialog.view.set_rect((10, 10, MIN_SIDE, 40))
    assert dialog.crop_button.isEnabled()
    dialog.deleteLater()


def test_a_picture_named_at_2x_is_cropped_in_its_own_pixels(app, tmp_path):
    """Qt reads "face@2x.png" as twice as dense: the crop dialog drew it at
    half size under a full-size frame."""
    from sealedlore.gui.ref_images import load_image

    path = tmp_path / "face@2x.png"
    _image(64, 48).save(str(path))
    image = load_image(path)
    assert image.devicePixelRatio() == 1.0 and image.size().toTuple() == (64, 48)


def test_a_scenario_or_backup_imported_by_the_window_is_cleaned(app, tmp_path, monkeypatch, story):
    """The window's own imports, not just `clean_incoming`: a GIF with a
    comment in someone else's scenario, or in a backup, comes in clean."""
    import base64

    from sealedlore.gui.main_window import MainWindow
    from sealedlore.models.character import Character
    from sealedlore.models.image import ImageRef
    from sealedlore.models.scenario import Scenario
    from sealedlore.storage.repository import StoryBundle

    assert b"comment" in GIF.lower() or leftovers(GIF) != []
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    opened: list = []
    monkeypatch.setattr(window, "_open_new_bundle", lambda bundle, **_k: opened.append(bundle))
    scenario = Scenario(
        title="Gif",
        cast=[Character(name="Ann", reference_images=[ImageRef(file="images/refs/a.gif")])],
        reference_files={"images/refs/a.gif": base64.b64encode(GIF).decode()},
    )
    path = tmp_path / "gif.sealedlore-scenario.json"
    path.write_text(scenario.model_dump_json(), encoding="utf-8")
    window._import_scenario(path)
    [bundle] = opened
    assert leftovers(bundle.pending_files["images/refs/a.gif"]) == []

    backup = StoryBundle(story=story)
    backup.pending_files = {"images/refs/b.gif": GIF}
    monkeypatch.setattr(main_window_module(), "read_archive", lambda _p: (backup, []))
    kept: list = []
    monkeypatch.setattr(
        main_window_module(), "restore_archive", lambda b, *_a, **_k: kept.append(b) or b
    )
    monkeypatch.setattr(window, "open_story", lambda *_a, **_k: None)
    window._import_archive(tmp_path / "x.sealedlore-backup.zip")
    assert kept and leftovers(kept[0].pending_files["images/refs/b.gif"]) == []
    window.close()


def main_window_module():
    import sealedlore.gui.main_window as module

    return module
