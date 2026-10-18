"""The map's drawing, after the author's new AfterDark playthrough (Oct 18
2026): a run furnished three rooms and drew none of their pieces until
Settings was saved four minutes later; ten FLUX.3 Klein pictures then
appeared beside a Seedream painting with nothing saying what they were; the
painting was made from the tile set's stand-ins; and one wheel notch zoomed
the play view about six times. Offscreen, mocks only."""

from __future__ import annotations

import json
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sealedlore.engine.session_world_art import job_words  # noqa: E402
from sealedlore.engine.world_pictures import PictureJob  # noqa: E402
from sealedlore.models.config import MediaEndpoint  # noqa: E402
from tests.test_area import make_session  # noqa: E402

BED = json.dumps(
    {
        "rooms": [
            {
                "room": 0,
                "purpose": "bedroom",
                "items": [{"name": "a wooden bed", "w": 2, "h": 1, "where": "wall"}],
            }
        ]
    }
)


def _pictures_on(session) -> None:
    session.config.map_art = True
    session.config.map_pictures_mode = "auto"
    session.config.image_endpoint = MediaEndpoint(
        base_url="https://nano-gpt.com/api/v1", api_key="k"
    )


def test_a_run_that_furnishes_has_the_pieces_drawn_by_the_next_at_once(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, [BED, BED, BED])
    _pictures_on(session)
    furnish, _pictures = session.tile_art_jobs()
    house = [a for a in furnish if a.id == "house"]
    assert house
    session._tile_run(house, [], None, session.provider)
    assert session.map_art_wanted_again and session.tile_art_running
    _furnish, pictures = session.tile_art_jobs()
    assert "item:a-wooden-bed" in {j.key for j in pictures}, "drawn by the follow-up run"


def test_the_map_says_what_it_is_drawing_and_with_what(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    item = PictureJob(
        key="item:gun-cabinet", subject="item", subject_id="x", name="gun cabinet", prompt=""
    )
    walker = PictureJob(
        key="walker:Ruth", subject="walker", subject_id="Ruth", name="Ruth", prompt=""
    )
    assert job_words(item) == "the gun cabinet" and job_words(walker) == "Ruth's avatar"
    assert session.map_art_now() == ""
    done = threading.Event()
    thread = threading.Thread(target=done.wait, daemon=True)
    thread.start()
    session._tile_thread = thread
    session._doing("tile", "the gun cabinet, by FLUX.3 Image Klein")
    assert session.map_art_now() == "the gun cabinet, by FLUX.3 Image Klein"
    done.set()
    thread.join(5)
    assert session.map_art_now() == "", "a run that ended says nothing"


@pytest.fixture
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_wheel_notch_zooms_one_step_however_many_events_it_comes_in(app):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    from sealedlore.gui.area_view import ZOOM_STEP, _Graphics

    view = _Graphics()
    view.resize(400, 300)
    start = view.transform().m11()

    def notch(delta: int) -> None:
        event = QWheelEvent(
            QPointF(100, 100),
            QPointF(100, 100),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        view.wheelEvent(event)

    for _ in range(8):  # a high-resolution wheel: one notch as eight events
        notch(15)
    assert view.transform().m11() == pytest.approx(start * ZOOM_STEP)
    notch(-120)
    assert view.transform().m11() == pytest.approx(start)
    for _ in range(100):
        notch(120)
    assert view.transform().m11() == pytest.approx(2.0), "stops at the most zoom"
    view.deleteLater()


def _window():
    pytest.importorskip("PySide6")
    import tests.test_area_view as area_tests

    return area_tests.window


window = _window()


def test_paint_waits_for_the_place_s_pieces_then_starts(app, window, monkeypatch):
    """Painted from the play view as it stands: while its pieces are drawn
    that is the tile set's stand-ins, so the painting starts once they are in."""
    import tests.test_area_view as area_tests
    from sealedlore.engine import session_world_art
    from tests.test_world_pictures import FakeImages

    session = window.session
    _pictures_on(session)
    session.config.image_models = [
        {
            "id": session.config.map_isometric_model,
            "supported_parameters": {"resolutions": ["1024x576"], "max_input_images": 3},
        }
    ]
    FakeImages.sent = []
    monkeypatch.setattr(session_world_art, "ImageClient", FakeImages)
    monkeypatch.setattr(session_world_art, "list_image_models", lambda *a, **k: [])
    window.right_tabs.setCurrentWidget(window.world_view)
    window.world_view.go_local()
    area_tests.stand(window, {"Serrik Vaun": ("house", 5, 10)})
    area_tests.pump(app)
    session._tile_again = True  # the house's pieces are still to be drawn
    monkeypatch.setattr(type(session), "_start_tile_art", lambda self: False)
    window._area_paint_place()
    assert session.isometric_painting is None, "it waits"
    assert window._paint_waiting == "house" and "as soon as" in window.area_view.hint.text()
    assert window._progress_timer.isActive(), "and keeps checking"
    session._tile_again = False  # the pieces are in
    window._sync_progress()
    assert window._paint_waiting is None
    assert session.isometric_painting == "house" or FakeImages.sent, "painted now"
    if session._isometric_thread is not None:
        session._isometric_thread.join(10)
