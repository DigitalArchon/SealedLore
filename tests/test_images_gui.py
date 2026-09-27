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
from sealedlore.gui.transcript import ImageMessageWidget, PendingPictureWidget  # noqa: E402
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
