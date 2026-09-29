"""Videos in the window: the price and approval, the job, the transcript,
playing, resuming after a restart, and backups without videos.

Offscreen, and never the network: the prompt writer is the scripted mock,
the video endpoint an httpx MockTransport answering as NanoGPT did live, and
the model listing is the real capture in tests/data, preset so nothing is
fetched. The video is tests/data/tiny.mp4.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

import sealedlore.gui.window_videos as window_videos  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.video_dialog import VideoDialog  # noqa: E402
from sealedlore.gui.video_jobs import model_entry, model_from_entry  # noqa: E402
from sealedlore.gui.video_view import (  # noqa: E402
    PendingVideoWidget,
    VideoMessageWidget,
    multimedia_available,
    playback,
    stop_playback,
)
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.video import GeneratedVideo  # noqa: E402
from sealedlore.providers.videos import VideoClient  # noqa: E402
from sealedlore.storage.images import load_videos, write_story_file  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_video_jobs import MP4, PNG_B64, Endpoint, seedance  # noqa: E402
from tests.test_videos import nanogpt_models  # noqa: E402


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
    import base64

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    write_story_file(story.id, "images/refs/serrik.png", base64.b64decode(PNG_B64), tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.video_models = [model_entry(m) for m in nanogpt_models().values()]
    window.config.video_models_fetched_at = utc_now_iso()
    window.open_story(story.id)
    yield window
    window.video_jobs.stop_all()
    window.video_jobs.wait_all()
    stop_playback()
    window._chat_allows_close = lambda: True
    window.close()


def with_key(window: MainWindow, key: str = "video-key") -> None:
    window.config.video_endpoint.api_key = key


def mock_client(endpoint: Endpoint) -> VideoClient:
    client = VideoClient(
        "https://nano-gpt.com/api/v1",
        "video-key",
        client=httpx.Client(transport=httpx.MockTransport(endpoint)),
    )
    client.http._wake.wait = lambda _delay: False
    return client


def test_a_listed_model_survives_the_config():
    model = seedance()
    assert model_from_entry(model_entry(model)) == model


# --- the dialog ------------------------------------------------------------------------


def test_the_price_comes_first_and_must_be_accepted_again_when_it_changes(app, window: MainWindow):
    with_key(window)
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    assert set(dialog._controls) == {"resolution", "duration", "aspect_ratio", "generate_audio"}
    assert dialog.settings()["resolution"] == "480p", "the default: 2.5 at 480p for 5 s"
    assert "$0.90" in dialog.cost_banner.text() and "~10 pictures" in dialog.cost_banner.text()
    assert "far more expensive" in dialog.cost_banner.text()
    dialog.prompt.setPlainText("  A lantern swings in the rain.\n")
    assert not dialog.generate_button.isEnabled(), "not until the price is accepted"
    dialog.accept_cost.setChecked(True)
    assert dialog.generate_button.isEnabled()
    assert dialog.generate_button.text() == "Generate video ($0.90)"
    resolution = dialog._controls["resolution"]
    resolution.setCurrentIndex(resolution.findData("1080p"))
    assert "$4.50" in dialog.price.text()
    assert not dialog.accept_cost.isChecked(), "ticked for $0.90, not for $4.50"
    assert not dialog.generate_button.isEnabled()
    resolution.setCurrentIndex(resolution.findData("480p"))
    dialog.accept_cost.setChecked(True)
    dialog._approve()
    request = dialog.request
    assert request.prompt == "  A lantern swings in the rain.\n", "sent as it stands"
    assert request.settings["resolution"] == "480p" and request.quote == pytest.approx(0.9)
    assert request.start_frame is None


def test_an_unknown_price_is_accepted_as_unknown(app, window: MainWindow):
    with_key(window)
    window.config.video_model = "bytedance-seedance-2-0-fast"
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    assert "Price unknown" in dialog.price.text()
    assert "without knowing what it will cost" in dialog.accept_cost.text()
    assert dialog.end_choice.isEnabled(), "this one takes an end frame"


def test_a_shared_key_is_warned_about_every_time(app, window: MainWindow, monkeypatch):
    provider = window.config.active_provider() or None
    key = provider.api_key if provider is not None and provider.api_key else "shared"
    if provider is not None:
        provider.api_key = key
    else:
        window.config.image_endpoint.api_key = key
    with_key(window, key)
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    assert not dialog.shared_warning.isHidden()
    dialog.prompt.setPlainText("x")
    dialog.accept_cost.setChecked(True)
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: asked.append(a[1]) or QMessageBox.No
    )
    dialog._approve()
    assert asked == ["The video key is shared"] and dialog.request is None


def test_video_is_off_without_a_key_and_says_where_to_put_one(app, window: MainWindow, monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()) or 0)
    window.generate_video()
    assert shown and "Settings → Video" in shown[0] and "never uses your chat key" in shown[0]


# --- the job, the transcript and playing ---------------------------------------------------


def approve_with(monkeypatch, prompt: str = "A lantern in the rain.") -> None:
    class Approved(VideoDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.prompt.setPlainText(prompt)
            self.accept_cost.setChecked(True)
            self._approve()
            return 1

    monkeypatch.setattr(window_videos, "VideoDialog", Approved)


def test_a_video_is_on_disk_once_accepted_then_shown_and_played(
    app, window: MainWindow, monkeypatch
):
    with_key(window)
    approve_with(monkeypatch)
    endpoint = Endpoint(states=["IN_PROGRESS", "IN_PROGRESS", "COMPLETED"])
    monkeypatch.setattr(window.session, "video_client", lambda: mock_client(endpoint))
    story_id = window.session.story.id
    window.generate_video()
    wait_for(app, lambda: not window.video_jobs.jobs)
    videos = load_videos(story_id, window.root)
    assert len(videos) == 1 and videos[0].status == "done"
    assert videos[0].prompt == "A lantern in the rain."
    assert window.config.video_params["resolution"] == "480p", "the next one starts from these"
    flush(app)
    shown = window.transcript.findChildren(VideoMessageWidget)
    assert shown and shown[-1].video_id == videos[0].id
    if not multimedia_available():
        return
    # Its frames are taken once: the poster, and the last for the next video.
    wait_for(app, lambda: load_videos(story_id, window.root)[0].poster != "")
    record = load_videos(story_id, window.root)[0]
    assert window.session.pictures.read(record.poster)[:2] == b"\xff\xd8"
    assert record.last_frame and record.duration == pytest.approx(1.0, abs=0.2)
    assert any(use.file == record.last_frame for use in window.session.video_frame_choices())
    # Played in place, muted for the test.
    flush(app)
    widget = window.transcript.findChildren(VideoMessageWidget)[-1]
    widget._toggle_mute()
    widget.toggle()
    wait_for(app, lambda: widget.surface._frame is not None, timeout=10)
    assert playback().surface is widget.surface
    stop_playback()
    assert widget.surface._frame is None


def test_a_video_being_made_says_so_and_is_asked_after_again_on_opening(
    app, window: MainWindow, monkeypatch
):
    with_key(window)
    story_id = window.session.story.id
    pending = GeneratedVideo(
        anchor_node_id="a1",
        prompt="p",
        model="bytedance/seedance-2.5",
        api="nanogpt",
        base_url="https://nano-gpt.com/api/v1",
        job_id="vid_1",
        charged=0.9,
    )
    window.session.pictures.put_video(pending)
    window.refresh_images()
    window.reload_transcript()
    flush(app)
    waiting = window.transcript.findChildren(PendingVideoWidget)
    texts = [label.text() for w in waiting for label in w.findChildren(QLabel)]
    assert any("still being made" in text and "$0.90" in text for text in texts)
    # Opened again (as after a restart): followed, fetched, done.
    endpoint = Endpoint(states=["COMPLETED"])
    monkeypatch.setattr(window, "_client_for", lambda video: mock_client(endpoint))
    window.open_story(story_id)
    wait_for(app, lambda: not window.video_jobs.jobs)
    assert load_videos(story_id, window.root)[0].status == "done"
    assert endpoint.requests[0].url.params["requestId"] == "vid_1"


def test_closing_while_a_video_is_made_leaves_it_to_be_fetched_later(
    app, window: MainWindow, monkeypatch
):
    with_key(window)
    approve_with(monkeypatch)
    endpoint = Endpoint(states=["IN_PROGRESS"])

    def slow_client():
        client = mock_client(endpoint)
        client.http._wake.wait = lambda delay: time.sleep(0.01) or client.http.cancelled
        return client

    monkeypatch.setattr(window.session, "video_client", slow_client)
    story_id = window.session.story.id
    window.generate_video()
    wait_for(app, lambda: load_videos(story_id, window.root) != [])
    assert window._videos_allow_close()
    wait_for(app, lambda: not window.video_jobs.jobs)
    assert load_videos(story_id, window.root)[0].status == "pending"


# --- backups ------------------------------------------------------------------------------


def test_a_backup_can_leave_the_videos_out(app, window: MainWindow, monkeypatch, tmp_path):
    from sealedlore.storage.archive import read_archive

    done = GeneratedVideo(
        anchor_node_id="a1",
        status="done",
        file="images/videos/v1.mp4",
        prompt="p",
        model="m",
        api="nanogpt",
        base_url="https://nano-gpt.com/api/v1",
    )
    window.session.pictures.write(done.file, MP4)
    window.session.pictures.put_video(done)
    target = tmp_path / "out.sealedlore-archive.json"
    monkeypatch.setattr(window, "_save_path", lambda *a: target)
    choices: list[str] = []

    def answer(box):
        choices.append(box.text())
        leave = next(b for b in box.buttons() if b.text() == "Leave videos out")
        box.clickedButton = lambda: leave
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    window.export_story_archive()
    assert choices and "MB" in choices[0]
    bundle, _log = read_archive(target)
    assert done.file not in bundle.pending_files and "videos.json" not in bundle.pending_files
    assert "images/refs/serrik.png" in bundle.pending_files
