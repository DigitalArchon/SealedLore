"""Pictures in the window: approval, the background job, the transcript, the background.

Offscreen, and never the network: the prompt writer is the scripted mock, the
image endpoint an httpx MockTransport, and the model listing is preset so the
dialog never fetches it.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.images import ImageRequest  # noqa: E402
from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.ref_images import MAX_SIDE, picture_bytes  # noqa: E402
from sealedlore.gui.transcript import (  # noqa: E402
    ImageMessageWidget,
    PendingPictureWidget,
    PictureView,
)
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.images import ImageClient  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.images import load_generated_images, write_story_file  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_images import LISTING, PNG  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def flush(app: QApplication) -> None:
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    save_story_bundle(bundle, root=tmp_path)
    write_story_file(story.id, "images/refs/serrik.png", PNG, tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.image_models = LISTING["data"]
    window.config.image_models_fetched_at = utc_now_iso()
    window.open_story(story.id)
    yield window
    window.image_jobs.stop_all()
    window.image_jobs.wait_all()
    # A memory chat still open would ask before going, with no one to answer.
    window._chat_allows_close = lambda: True
    window.close()


def mock_image_client(delay: float = 0.0) -> ImageClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        time.sleep(delay)
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "cost": 0.09}
        )

    return ImageClient(
        "https://nano-gpt.com/api/v1",
        "key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_generate_sends_the_prompt_as_it_stands_in_the_dialog(app, window: MainWindow):
    dialog = ImageDialog(window.session, parent=window)
    dialog.model.setText("bytedance/seedream-v5.0-pro")
    dialog.size_choice.setCurrentText("1k")
    text = "  Serrik in the undercroft, torch raised.  \n"
    dialog.prompt.setPlainText(text)
    assert dialog.generate_button.isEnabled()
    assert "0.045" in dialog.generate_button.text()  # listed for 1k

    dialog._approve()
    request = dialog.request
    assert request is not None
    assert request.prompt == text
    assert request.model == "bytedance/seedream-v5.0-pro" and request.size == "1k"
    assert request.anchor_node_id == "a1"
    assert request.listed_price == pytest.approx(0.045)


def test_the_writer_fills_the_prompt_and_its_pictures(app, window: MainWindow):
    window.session.provider = MockChatProvider(
        ['{"references": ["R1"], "prompt": "The man in image 1 waits by the door."}']
    )
    dialog = ImageDialog(window.session, parent=window)
    dialog.direction.setPlainText("Serrik, waiting")
    dialog._write()
    wait_for(app, lambda: dialog._worker is None)
    assert dialog.prompt.toPlainText() == "The man in image 1 waits by the door."
    assert [use.owner_name for use in dialog._refs] == ["Serrik Vaun"]
    assert not dialog.warning.isVisible()

    # Dropping the picture the prompt names is flagged, not silently fixed.
    dialog.refs.setCurrentRow(0)
    dialog._remove_ref()
    assert "image 1" in dialog.warning.text()
    assert dialog.prompt.toPlainText() == "The man in image 1 waits by the door."


def test_a_story_can_add_a_picture_from_disk(app, window: MainWindow, tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QFileDialog

    source = tmp_path / "gate.png"
    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(QColor("#336699"))
    assert image.save(str(source), "PNG")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), ""))

    dialog = ImageDialog(window.session, parent=window)
    dialog.model.setText("bytedance/seedream-v5.0-pro")
    dialog._fill_add_menu()
    labels = [action.text() for action in dialog.add_ref.menu().actions() if action.text()]
    assert labels[-1] == "Add a picture from disk…"
    assert dialog.add_ref.isEnabled()

    before = len(dialog._refs)
    dialog._add_from_disk()
    added = window.session.story.reference_images
    assert len(added) == 1 and added[0].file.startswith("images/refs/")
    assert [use.ref_id for use in dialog._refs][before:] == [added[0].id]
    assert dialog._refs[-1].owner_kind == "story"
    # Kept with the story, and offered the next time the dialog opens.
    again = ImageDialog(window.session, parent=window)
    assert added[0].id in [choice.use.ref_id for choice in again._choices]


def test_a_model_that_takes_no_pictures_sends_none(app, window: MainWindow):
    dialog = ImageDialog(window.session, parent=window)
    dialog._set_refs([window.session.image_ref_choices()[0].use])
    dialog.model.setText("hidream")
    dialog.prompt.setPlainText("A duellist.")
    assert "takes no reference pictures" in dialog.warning.text()
    dialog._approve()
    assert dialog.request is not None and dialog.request.references == ()


def test_a_picture_is_drawn_in_the_background_and_lands_after_its_passage(app, window: MainWindow):
    request = ImageRequest(
        story_id=window.session.story.id,
        model="bytedance/seedream-v5.0-pro",
        prompt="A duellist.",
        size="16:9",
        n=1,
        references=(window.session.image_ref_choices()[0].use,),
        anchor_node_id="a0",
    )
    window.image_jobs.start(request, mock_image_client(delay=0.2))
    window.reload_transcript()
    flush(app)
    assert window.transcript.findChildren(PendingPictureWidget)
    # The story stays playable meanwhile.
    assert window.composer.input.isEnabled()

    wait_for(app, lambda: not window.image_jobs.pending())
    flush(app)
    images = load_generated_images(window.session.story.id, window.root)
    assert len(images) == 1 and images[0].prompt == "A duellist."
    pictures = window.transcript.findChildren(ImageMessageWidget)
    assert len(pictures) == 1
    assert not window.transcript.findChildren(PendingPictureWidget)
    # After passage a0, before the next author turn.
    column = window.transcript._layout
    order = [column.itemAt(i).widget() for i in range(column.count() - 1)]
    position = order.index(pictures[0])
    assert getattr(order[position - 1], "node_id", None) == "a0"
    assert window.images_panel.list.count() == 1


def test_a_picture_for_a_story_no_longer_open_goes_to_that_story(
    app, window: MainWindow, tmp_path: Path
):
    first = window.session.story.id
    other = Story(title="Other")
    save_story_bundle(StoryBundle(story=other), root=tmp_path)
    request = ImageRequest(
        story_id=first,
        model="hidream",
        prompt="p",
        size="1024x1024",
        n=1,
        references=(),
        anchor_node_id="a1",
    )
    window.image_jobs.start(request, mock_image_client(delay=0.2))
    window.open_story(other.id)
    wait_for(app, lambda: not window.image_jobs.pending())
    assert len(load_generated_images(first, tmp_path)) == 1
    assert load_generated_images(other.id, tmp_path) == []
    assert window.images_panel.list.count() == 0


def test_a_background_is_set_kept_and_cleared(app, window: MainWindow, tmp_path: Path):
    source = tmp_path / "bg.png"
    source.write_bytes(PNG)
    window._set_background_from(source)
    story = window.session.story
    assert story.background_image == "images/background.png"
    assert window.transcript.has_background
    assert window.clear_background_action.isEnabled()

    window.open_story(story.id)
    assert window.transcript.has_background

    window.clear_background()
    assert window.session.story.background_image is None
    assert not window.transcript.has_background

    # A file gone missing is simply no background.
    window.session.story.background_image = "images/missing.png"
    window._apply_background()
    assert not window.transcript.has_background


def test_a_large_picture_is_scaled_on_the_way_in(app, tmp_path: Path):
    image = QImage(3000, 1000, QImage.Format_RGB32)
    image.fill(QColor("darkslateblue"))
    path = tmp_path / "big.bmp"
    image.save(str(path))
    data, suffix = picture_bytes(path)
    assert suffix == ".jpg"
    scaled = QImage.fromData(data)
    assert max(scaled.width(), scaled.height()) == MAX_SIDE

    small = tmp_path / "small.png"
    small.write_bytes(PNG)
    assert picture_bytes(small) == (PNG, ".png")
    with pytest.raises(ValueError):
        (tmp_path / "not.png").write_bytes(b"text")
        picture_bytes(tmp_path / "not.png")


def test_the_prompt_writer_is_chosen_in_the_dialog_and_kept(app, window: MainWindow, monkeypatch):
    """Playtesting: the writer could be changed in Settings only, so in play it
    couldn't be found. Blank is the story model; a choice is kept."""
    from sealedlore.gui import window_images
    from sealedlore.models.config import ProviderConfig
    from sealedlore.storage.repository import load_config

    session = window.session
    session.provider = MockChatProvider(['{"references": [], "prompt": "One."}'] * 2)
    dialog = ImageDialog(session, parent=window)
    assert dialog.writer.text() == "" and dialog.writer_model() is None
    assert dialog.writer.placeholderText() == (session.model or "the story's model")
    dialog._write()
    wait_for(app, lambda: dialog._worker is None)
    assert session.provider.requests[-1].model == session.model

    dialog.writer.setText("zai-org/glm-5.3")
    assert "without the storyteller's cache" in dialog.write_button.toolTip()
    dialog._write()
    wait_for(app, lambda: dialog._worker is None)
    assert session.provider.requests[-1].model == "zai-org/glm-5.3"

    # Through the window: the choice becomes the setting, and sticks.
    class Picked(ImageDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.writer.setText("zai-org/glm-5.3")
            return 0

    monkeypatch.setattr(window_images, "ImageDialog", Picked)
    if window.config.active_provider() is None:
        window.config.providers.append(ProviderConfig(name="p", base_url="https://x.test/v1"))
        window.config.active_provider_name = "p"
    window.generate_image()
    assert window.config.image_prompt_model == "zai-org/glm-5.3"
    assert load_config(root=window.root).image_prompt_model == "zai-org/glm-5.3"
    assert ImageDialog(session, parent=window).writer.text() == "zai-org/glm-5.3"


# --- a chat kept in memory only --------------------------------------------------


def data_listing(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_memory_chat_keeps_its_pictures_in_memory_and_can_save_them(
    app, window: MainWindow, tmp_path: Path, tmp_path_factory, monkeypatch
):
    """The author (Sept 2026): pictures in a memory-only chat, kept in memory
    only, with a way to export them. Everything a picture touches (a
    reference from disk, the drawn picture, its log, the background) stays
    off the disk; Save all writes only where the author chose."""
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    import sealedlore.gui.image_dialog as image_dialog
    from tests.test_chat_gui import make_chat

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    outside = tmp_path_factory.mktemp("elsewhere")
    source = outside / "lighthouse.png"
    source.write_bytes(PNG)
    make_chat(window, monkeypatch, model="z-ai/glm-5.3", keep="memory")
    session = window.session
    assert session.memory_only
    before = data_listing(tmp_path)

    # A reference picture from disk, through the picture dialog.
    monkeypatch.setattr(
        image_dialog.QFileDialog, "getOpenFileName", lambda *a, **k: (str(source), "")
    )
    dialog = ImageDialog(session, parent=window)
    dialog._add_from_disk()
    ref = session.story.reference_images[0]
    assert session.pictures.read(ref.file) is not None
    dialog.deleteLater()

    # A picture drawn with it lands in the transcript and the Images tab.
    use = next(c.use for c in session.image_ref_choices() if c.use.ref_id == ref.id)
    request = ImageRequest(
        story_id=session.story.id,
        model="hidream",
        prompt="A lighthouse at dusk.",
        size="1024x1024",
        n=1,
        references=(use,),
        anchor_node_id=None,
    )
    window.image_jobs.start(request, mock_image_client(delay=0.1), session.pictures)
    wait_for(app, lambda: not window.image_jobs.pending())
    flush(app)
    assert len(session.generated_images()) == 1
    assert window.images_panel.list.count() == 1
    assert window.images_panel.save_all_button.isEnabled()
    assert any(p.picture is not None for p in window.transcript.findChildren(ImageMessageWidget))
    assert {"image_request", "image_response"} <= {e["kind"] for e in session.memory_log}

    # A background, from a file and from the picture made.
    window._set_background_from(source)
    assert window.transcript.has_background
    window._image_action("background", session.generated_images()[0].id)
    assert window.transcript.has_background

    assert data_listing(tmp_path) == before, "a memory chat's picture reached the disk"

    # Save all writes every picture where the author chose, and nowhere else.
    target = tmp_path_factory.mktemp("saved")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(target))
    window.save_all_pictures()
    saved = sorted(p.name for p in target.iterdir())
    assert len(saved) == 1 and saved[0].startswith("001-")
    assert data_listing(tmp_path) == before
    # And one picture at a time, from the picture's own menu.
    one = tmp_path_factory.mktemp("one") / "picture.png"
    monkeypatch.setattr(image_dialog.QFileDialog, "getSaveFileName", lambda *a, **k: (str(one), ""))
    window._image_action("save", session.generated_images()[0].id)
    assert one.read_bytes() == session.pictures.read(session.generated_images()[0].file)
    assert data_listing(tmp_path) == before

    # A picture still being drawn when the chat closes is lost with it.
    window.image_jobs.start(request, mock_image_client(delay=0.3), session.pictures)
    window.close_story()
    assert window.session is None
    wait_for(app, lambda: not window.image_jobs.pending())
    flush(app)
    assert session.pictures.images() == []
    assert data_listing(tmp_path) == before


def test_an_image_listing_fetched_in_a_memory_chat_stays_out_of_the_config(app, tmp_path: Path):
    from sealedlore.gui.image_jobs import ImageCatalog
    from sealedlore.models.config import Config
    from sealedlore.providers.images import parse_image_models

    config = Config()
    before = config.model_dump_json()
    memory = [True]
    catalog = ImageCatalog(config, tmp_path, in_memory=lambda: memory[0])
    found = parse_image_models(LISTING)
    catalog._on_finished(found)
    assert config.model_dump_json() == before, "the fetch reached the config"
    assert not (tmp_path / "config.json").exists()
    assert [info.id for info in catalog.models] == [info.id for info in found]
    # Out of the memory chat, a fetch is kept in the config as ever.
    memory[0] = False
    catalog._on_finished(found)
    assert config.image_models and config.image_models_fetched_at


def test_a_memory_chat_exports_and_comes_back_in_memory_or_saved(
    app, window: MainWindow, tmp_path: Path, tmp_path_factory, monkeypatch
):
    """The author: a memory chat can be exported, and its backup brought back
    in memory again (nothing written) or as an ordinary saved chat."""
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.engine.prompt import TurnRequest
    from sealedlore.models.node import CHAT_USER_ID
    from tests.test_chat_gui import make_chat

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    make_chat(window, monkeypatch, model="z-ai/glm-5.3", keep="memory")
    session = window.session
    session.provider.responses = ["A lamp on the point."]
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Your lighthouse?")))
    request = ImageRequest(
        story_id=session.story.id,
        model="hidream",
        prompt="A lighthouse.",
        size="1024x1024",
        n=1,
        references=(),
        anchor_node_id=None,
    )
    window.image_jobs.start(request, mock_image_client(), session.pictures)
    wait_for(app, lambda: not window.image_jobs.pending())
    window._update_controls()
    for action in (window.archive_action, window.markdown_action):
        assert action.isEnabled(), action.text()
    assert not window.restart_action.isEnabled() and not window.export_action.isEnabled()

    before = data_listing(tmp_path)
    backup = tmp_path_factory.mktemp("backups") / "chat.sealedlore-backup.json"
    monkeypatch.setattr(window, "_save_path", lambda *a: backup)
    window.export_story_archive()
    assert backup.is_file()
    window.close_story()
    assert data_listing(tmp_path) == before

    # Back into memory: nothing written; its picture, text and cost are back.
    monkeypatch.setattr(window, "_ask_memory_import", lambda title: "memory")
    window._import_archive(backup)
    again = window.session
    assert again.memory_only and again.path()[-1].content == "A lamp on the point."
    image = again.generated_images()[0]
    assert again.pictures.read(image.file) is not None
    assert {"image_request", "image_response"} <= {e["kind"] for e in again.memory_log}
    assert data_listing(tmp_path) == before
    window.close_story()

    # Cancel imports nothing.
    monkeypatch.setattr(window, "_ask_memory_import", lambda title: None)
    window._import_archive(backup)
    assert window.session is None and data_listing(tmp_path) == before

    # Saved: an ordinary chat on disk, with its picture.
    monkeypatch.setattr(window, "_ask_memory_import", lambda title: "disk")
    window._import_archive(backup)
    saved = window.session
    assert saved is not None and not saved.memory_only
    assert saved.story.chat_keep == "disk"
    assert len(load_generated_images(saved.story.id, tmp_path)) == 1
    window.close_story()


def test_an_ordinary_backup_is_imported_without_asking(app, window: MainWindow, tmp_path_factory):
    backup = tmp_path_factory.mktemp("backups") / "story.sealedlore-backup.json"
    window._save_path = lambda *a: backup
    window.export_story_archive()

    def never(title):
        raise AssertionError("asked where to keep an ordinary story")

    window._ask_memory_import = never
    window._import_archive(backup)
    assert window.session is not None and not window.session.memory_only


# --- a transcript picture is held no larger than it is shown ----------------------------


def plain_picture(width: int, height: int, fmt: str = "PNG") -> bytes:
    from sealedlore.gui.ref_images import _encoded

    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor("#336699"))
    return _encoded(image, fmt)


def test_a_picture_is_decoded_within_the_box_and_keeps_its_shape(app):
    from PySide6.QtCore import QSize

    from sealedlore.gui.ref_images import pixmap_of, pixmap_within

    box = QSize(920, 480)
    wide = pixmap_within(plain_picture(2048, 1152, "JPEG"), box)
    assert wide.size().toTuple() == (853, 480)
    tall = pixmap_within(plain_picture(896, 1152), box)
    assert tall.size().toTuple() == (373, 480)
    # One that fits is as it is in the file: never enlarged.
    small = plain_picture(300, 200)
    assert pixmap_within(small, box).size() == pixmap_of(small).size()
    assert pixmap_within(small, box).size().toTuple() == (300, 200)
    assert pixmap_within(None, box).isNull() and pixmap_within(b"not a picture", box).isNull()


def test_a_photo_held_sideways_is_upright_within_the_box(app):
    from PySide6.QtCore import QSize

    from sealedlore.gui.ref_images import pixmap_of, pixmap_within
    from sealedlore.storage.image_meta import strip_metadata
    from tests.test_image_meta import dirty_jpeg

    # Stored 64 wide and 48 high, to be shown turned: 48 wide and 64 high.
    for data in (dirty_jpeg(orientation=6), strip_metadata(dirty_jpeg(orientation=6))):
        assert pixmap_of(data).size().toTuple() == (48, 64)
        shown = pixmap_within(data, QSize(40, 32))
        assert shown.size().toTuple() == (24, 32)


def test_the_transcript_holds_a_picture_at_the_size_it_shows(app):
    from sealedlore.gui.transcript import PICTURE_MAX_HEIGHT, READING_WIDTH
    from sealedlore.models.image import GeneratedImage

    record = GeneratedImage(file="images/big.png", prompt="p", model="m", size="2048x1152")
    widget = ImageMessageWidget(record, plain_picture(2048, 1152))
    held = widget.picture._pixmap.size()
    assert held.width() <= READING_WIDTH and held.height() == PICTURE_MAX_HEIGHT
    # Shown at the size it was decoded, it is drawn as it is: no second copy.
    view = PictureView(widget.picture._pixmap)
    view.resize(READING_WIDTH, PICTURE_MAX_HEIGHT)
    view.grab()
    assert view._scaled is view._pixmap
    # A narrower column scales it down from what is held.
    view.resize(400, PICTURE_MAX_HEIGHT)
    view.grab()
    assert view._scaled.width() in (399, 400) and view._scaled.height() == 225


def test_a_picture_held_smaller_is_what_the_view_would_have_shown(app):
    """Scaled by the decoder instead, a JPEG came out softer (9-37% less fine
    detail on real pictures): it is read whole and scaled as the view scales."""
    from PySide6.QtCore import QSize, Qt

    from sealedlore.gui.ref_images import pixmap_of, pixmap_within
    from tests.test_image_meta import qt_picture

    for fmt in ("JPEG", "PNG"):
        data = qt_picture(fmt, width=256, height=192)
        shown = pixmap_of(data).scaled(QSize(80, 60), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        held = pixmap_within(data, QSize(100, 60))
        assert held.size().toTuple() == (80, 60)
        assert held.toImage() == shown.toImage()
