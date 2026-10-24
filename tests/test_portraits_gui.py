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

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.engine.tokens import TokenEstimator, fallback_counter  # noqa: E402
from sealedlore.gui.cast_panel import CastPanel  # noqa: E402
from sealedlore.gui.portraits_dialog import PortraitsDialog  # noqa: E402
from sealedlore.gui.start_dialog import StartDialog  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.picture_store import MemoryPictures  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

K3 = "blackforestlabs/flux-3/klein/text-to-image"
K3E = "blackforestlabs/flux-3/klein/edit-image"


def _png(width: int = 2, height: int = 2) -> bytes:
    """A grey PNG (2x2 by default), valid to the byte."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = b"".join(b"\0" + b"\x80\x80\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
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
    assert dialog.model.text() == Config().portrait_model == K3
    # The author edits one and leaves another out.
    dialog.rows[0].prompt.setPlainText("  My own words.\n")
    dialog.rows[1].tick.setChecked(False)
    requests = dialog.requests()
    assert [r.prompt for r in requests][0] == "  My own words.\n"
    assert all(r.card_id and r.card_first for r in requests) and len(requests) == len(cast) - 1
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
    asked: list[tuple] = []
    panel.picture_requested.connect(lambda *args: asked.append(args))
    panel.portrait.edit_button.click()  # on Jane's portrait, now "b"
    panel.pictures.list.setCurrentRow(0)
    panel.pictures.again_button.click()  # on her other picture, "a"
    panel.portrait.scene_button.click()
    assert asked == [(jane.id, "b", "edit"), (jane.id, "a", "again"), (jane.id, "", "scene")]
    asked.clear()
    panel.select_character(john.id)
    assert panel.portrait.picture.isHidden() and not panel.portrait.empty.isHidden()
    assert panel.portrait.edit_button.isHidden()  # nothing to edit yet
    panel.portrait.draw_button.click()
    assert asked == [(john.id, "", "new")]


def test_start_counts_the_cards_without_pictures(app, story, cast):
    plain = StartDialog(story, cast)
    assert plain.pictures is None and not plain.pictures_wanted()
    town = LoreEntry(title="Hellsville", content="A walled town.")
    caves = LoreEntry(title="The caves", content="Caves.")
    offered = StartDialog(story, cast, without_pictures=(cast[:1], [town, caves]))
    assert "(1 character and 2 lore entries)" in offered.pictures.text()
    assert "Hellsville" in offered.pictures.toolTip()
    offered.pictures.setChecked(True)
    assert offered.pictures_wanted()
    lore_only = StartDialog(story, cast, without_pictures=([], [town]))
    assert "(1 lore entry)" in lore_only.pictures.text()
    # "Play one of these": whoever isn't picked is set aside, and isn't drawn.
    story.setup.choose_one_of = [cast[0].id, cast[1].id]
    pair = StartDialog(story, cast, without_pictures=(cast[:2], []))
    pair.play_as.setCurrentIndex(pair.play_as.findData(cast[0].id))
    assert "(1 character)" in pair.pictures.text()
    assert cast[1].name not in pair.pictures.toolTip()


class _Catalog(QObject):
    """Stands in for the window's ImageCatalog: a listing, no fetching."""

    changed = Signal()

    def __init__(self, models):
        super().__init__()
        self.models = models

    def ensure(self, *, force: bool = False) -> None:
        pass


def _session(tmp_path: Path, story, cast, replies=("x",)) -> StorySession:
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        Config(),
        MockChatProvider(list(replies)),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_an_edit_says_what_changes_and_needs_a_model_that_takes_a_picture(
    app, tmp_path: Path, story, cast
):
    from sealedlore.gui.picture_variant import PictureVariantDialog
    from sealedlore.providers.images import ImageModelInfo

    session = _session(tmp_path, story, cast)
    session.pictures.write("images/refs/p.png", PNG)
    portrait = ImageRef(
        id="p", file="images/refs/p.png", caption="portrait", prompt="A man.", model="words-only"
    )
    hero = cast[0]
    hero.reference_images = [portrait]
    catalog = _Catalog(
        [
            ImageModelInfo(id="words-only", name="W", resolutions=("1024x1024",)),
            # Both FLUX.3 Klein versions list pictures, as the endpoint's do.
            ImageModelInfo(
                id=K3, name="K", resolutions=("1024x1024",), max_inputs=10, per_image={"*": 0.009}
            ),
            ImageModelInfo(
                id=K3E, name="KE", resolutions=("1024x1024",), max_inputs=10, per_image={"*": 0.009}
            ),
        ]
    )
    session.config.image_models = [
        {"id": m.id, "supported_parameters": {"max_input_images": m.max_inputs}}
        for m in catalog.models
    ]
    dialog = PictureVariantDialog(session, hero, portrait, edit=True, catalog=catalog, browse=None)
    # Its own model can't be given the picture: the portrait model, which can,
    # and since the picture goes with the prompt, its edit version is sent.
    assert dialog.model.text() == K3
    assert dialog.sent_model() == K3E and "its edit version" in dialog.note.text()
    assert "this picture, as image 1" in dialog.sends.text()
    assert not dialog.draw_button.isEnabled()  # nothing said to change yet
    dialog.change.setText("twenty years older")
    assert dialog.prompt.toPlainText().startswith("Edit this picture: twenty years older.")
    assert dialog.draw_button.isEnabled()
    request = dialog.request()
    assert request.references[0].ref_id == "p" and request.card_caption == "twenty years older"
    assert request.model == K3E, "what the dialog said is what is sent"
    assert not request.card_first
    dialog.model.setText("words-only")
    assert not dialog.draw_button.isEnabled() and "can't be given a picture" in dialog.note.text()
    dialog.reject()
    # Drawing again: the picture's own prompt and model, nothing sent with it.
    again = PictureVariantDialog(session, hero, portrait, edit=False, catalog=catalog, browse=None)
    assert again.prompt.toPlainText() == "A man." and again.model.text() == "words-only"
    request = again.request()
    assert request.references == () and request.card_caption == "drawn again"
    assert "no picture, only the words" in again.sends.text()
    again.reject()


def test_the_lore_page_shows_an_entry_s_picture_large_with_the_same_actions(app):
    from sealedlore.gui.lore_panel import LorePanel

    store = MemoryPictures(lambda _kind, _body: "")
    store.write("images/refs/t.png", PNG)
    town = LoreEntry(
        title="Hellsville",
        content="A walled town.",
        reference_images=[ImageRef(id="t", file="images/refs/t.png", caption="from the road")],
    )
    bare = LoreEntry(title="The caves", content="Caves.")
    panel = LorePanel(lambda: store)
    panel.set_entries([town, bare])
    panel.list.setCurrentRow(0)
    assert not panel.picture.picture.isHidden()
    assert panel.picture.scene_button.isHidden()  # a character's alone
    asked: list[tuple] = []
    panel.picture_requested.connect(lambda *args: asked.append(args))
    panel.picture.edit_button.click()
    panel.list.setCurrentRow(1)
    assert panel.picture.picture.isHidden()
    panel.picture.draw_button.click()
    assert asked == [(town.id, "t", "edit"), (bare.id, "", "new")]


def test_the_cards_dialog_writes_for_lore_too_and_leaves_out_what_has_nothing_to_see(
    app, tmp_path: Path, story, cast
):
    town = LoreEntry(title="Hellsville", content="A walled town.")
    bite = LoreEntry(title="The bite", content="A bite turns you.")
    written = json.dumps(
        {
            "lore": [
                {"title": "Hellsville", "prompt": "A walled town in snow."},
                {"title": "The bite", "prompt": ""},
            ]
        }
    )
    session = _session(tmp_path, story, cast, [written])
    session.bundle.lore.extend([town, bite])
    dialog = PortraitsDialog(session, [], lore=[town, bite], catalog=None, browse=None)
    wait_for(app, lambda: not dialog.writing())
    app.processEvents()
    town_row, bite_row = dialog.rows
    assert town_row.prompt.toPlainText() == "A walled town in snow."
    assert not bite_row.tick.isChecked() and not bite_row.wanted()
    requests = dialog.requests()
    assert [r.card_id for r in requests] == [town.id] and requests[0].card_first
    dialog.reject()


def test_in_the_scene_asks_for_them_alone_with_their_pictures_and_lands_on_the_card(
    app, tmp_path: Path, story, cast
):
    from sealedlore.gui.image_dialog import SCENE_CAPTION, ImageDialog

    session = _session(tmp_path, story, cast)
    session.pictures.write("images/refs/p.png", PNG)
    hero = cast[0]
    hero.reference_images = [ImageRef(id="p", file="images/refs/p.png", caption="portrait")]
    dialog = ImageDialog(session, card=hero)
    assert dialog.direction.toPlainText().startswith(f"{hero.name} alone")
    assert [use.ref_id for use in dialog._refs] == ["p"]
    dialog.model.setText("some-model")
    dialog.size_choice.setEditText("1024x1024")
    dialog.prompt.setPlainText("A man by a fire.")
    dialog._approve()
    request = dialog.request
    assert request.card_id == hero.id and not request.card_first
    assert request.card_caption == SCENE_CAPTION
    assert [use.ref_id for use in request.references] == ["p"]


def test_the_media_tab_shows_every_picture_by_cast_lore_and_story(app):
    """The author: every image and video in the story in one place. A
    picture drawn for a card shows under its card, not twice; taken off the
    card, it is the story's again."""
    from sealedlore.gui.images_panel import ImagesPanel
    from sealedlore.models.image import GeneratedImage

    store = MemoryPictures(lambda _kind, _body: "")
    for name in ("p", "t", "s", "d"):
        store.write(f"images/{name}.png", PNG)
    drawn = GeneratedImage(
        id="drawn", file="images/d.png", prompt="A man.", model="m", size="1024x1536"
    )
    scene = GeneratedImage(id="scene", file="images/s.png", prompt="A road.", model="m", size="1k")
    jane = Character(
        name="Jane",
        reference_images=[
            ImageRef(id="p", file="images/p.png", caption="portrait", drawn_as="drawn")
        ],
    )
    town = LoreEntry(
        title="Hellsville",
        content="A town.",
        reference_images=[ImageRef(id="t", file="images/t.png", caption="main street")],
    )
    panel = ImagesPanel()
    panel.set_images([drawn, scene], store, set(), [], cast=[jane], lore=[town])
    assert panel.cast_list.count() == 1 and panel.lore_list.count() == 1
    assert [panel.list.item(i).data(Qt.UserRole) for i in range(panel.list.count())] == ["scene"]
    assert panel.cast_heading.text() == "CAST · 1" and panel.story_heading.text() == "STORY · 1"
    asked: list[tuple] = []
    panel.card_action_requested.connect(lambda *args: asked.append(args))
    panel.lore_list.itemActivated.emit(panel.lore_list.item(0))
    assert asked == [("open", town.id, "t")]
    # Off the card, the drawing is the story's again.
    jane.reference_images.clear()
    panel.set_images([drawn, scene], store, set(), [], cast=[jane], lore=[town])
    assert panel.list.count() == 2 and panel.cast_heading.isHidden()


def test_an_edit_of_a_story_picture_keeps_the_scene_and_goes_after_its_passage(
    app, tmp_path: Path, story, cast
):
    from sealedlore.gui.picture_variant import PictureVariantDialog, VariantSubject
    from sealedlore.models.image import GeneratedImage
    from sealedlore.providers.images import ImageModelInfo

    session = _session(tmp_path, story, cast)
    session.pictures.write("images/s.png", _png(64, 36))
    scene = GeneratedImage(
        id="s",
        anchor_node_id="a1",
        file="images/s.png",
        prompt="Two people on a road.",
        model="seedream",
        size="16:9",
    )
    catalog = _Catalog(
        [
            ImageModelInfo(
                id="flux-2-klein-9b",
                name="F",
                resolutions=("1024*1024", "1280*720", "1024*1536"),
                max_inputs=4,
                per_image={"*": 0.016},
            )
        ]
    )
    dialog = PictureVariantDialog(
        session, VariantSubject.of_picture(scene), edit=True, catalog=catalog, browse=None
    )
    assert dialog.windowTitle() == "Change the picture"
    # The only model listed here that takes a picture (the default edit model isn't).
    dialog.model.setText("flux-2-klein-9b")
    dialog.change.setText("at night")
    assert "Keep the same scene: the same people" in dialog.prompt.toPlainText()
    request = dialog.request()
    assert request.anchor_node_id == "a1" and not request.background
    assert [(use.owner_kind, use.file) for use in request.references] == [
        ("picture", "images/s.png")
    ]
    assert request.size == "1280*720"  # nearest the picture's own 16:9
    assert request.card_id is None
    dialog.reject()


def test_remove_takes_off_the_portrait_shown_when_asked(app, monkeypatch):
    """The review: a portrait landing while Remove's question was up went
    first, and Yes removed it and kept the one the author was looking at."""
    from PySide6.QtWidgets import QMessageBox

    store = MemoryPictures(lambda _kind, _body: "")
    for name in ("old", "new"):
        store.write(f"images/refs/{name}.png", PNG)
    jane = Character(name="Jane", reference_images=[ImageRef(id="old", file="images/refs/old.png")])
    panel = CastPanel(lambda: store)
    panel.set_cast([jane])
    panel.select_character(jane.id)

    def landing(*_args, **_kwargs):
        jane.reference_images.insert(0, ImageRef(id="new", file="images/refs/new.png"))
        panel.pictures.set_refs(jane.reference_images)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", landing)
    panel.portrait.remove_button.click()
    assert [ref.id for ref in jane.reference_images] == ["new"]


def test_drawn_again_a_picture_goes_with_what_it_was_drawn_from(app, tmp_path: Path, story, cast):
    """The review: Draw again on an In the scene… picture sent "the man in
    image 1" with no image 1 (an edit's copy, "Edit this picture" with no
    picture), paid for and drawn as anyone at all."""
    from sealedlore.gui.picture_variant import PictureVariantDialog
    from sealedlore.models.image import GeneratedImage, RefUse
    from sealedlore.providers.images import ImageModelInfo

    session = _session(tmp_path, story, cast)
    hero = cast[0]
    session.pictures.write("images/refs/p.png", PNG)
    session.pictures.write("images/scene.png", PNG)
    use = RefUse(
        owner_kind="character",
        owner_id=hero.id,
        owner_name=hero.name,
        ref_id="p",
        file="images/refs/p.png",
    )
    drawn = GeneratedImage(
        id="scene-1",
        anchor_node_id="a1",
        file="images/scene.png",
        prompt="The man in image 1 by a fire.",
        model="klein",
        size="1k",
        references=[use],
        card_id=hero.id,
    )
    session.pictures.add_images([drawn])
    copy = ImageRef(
        id="c",
        file="images/refs/c.png",
        prompt=drawn.prompt,
        model="klein",
        drawn_as="scene-1",
    )
    session.pictures.write("images/refs/c.png", PNG)
    hero.reference_images = [ImageRef(id="p", file="images/refs/p.png"), copy]
    catalog = _Catalog(
        [
            ImageModelInfo(id="klein", name="K", resolutions=("1024*1536",), max_inputs=4),
            ImageModelInfo(id="words-only", name="W", resolutions=("1024x1024",)),
        ]
    )
    again = PictureVariantDialog(session, hero, copy, edit=False, catalog=catalog, browse=None)
    assert "The picture it was drawn from goes with it" in again.note.text()
    request = again.request()
    assert [u.file for u in request.references] == ["images/refs/p.png"]
    again.model.setText("words-only")
    assert not again.draw_button.isEnabled() and "can't be given a picture" in again.note.text()
    again.reject()


def test_building_a_picture_strip_shows_no_window_of_its_own(app):
    """The review: Make portrait, Draw again… and Edit… were shown before
    they had a parent, each a top-level window for a moment, on every card."""
    from PySide6.QtCore import QEvent
    from PySide6.QtCore import QObject as _QObject

    from sealedlore.gui.ref_images import RefImageStrip

    class Windows(_QObject):
        shown: list[str] = []

        def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
            if event.type() == QEvent.Show and watched.isWidgetType() and watched.isWindow():
                self.shown.append(type(watched).__name__)
            return False

    spy = Windows()
    app.installEventFilter(spy)
    try:
        strip = RefImageStrip(lambda: None, after_portrait=True)
    finally:
        app.removeEventFilter(spy)
    assert spy.shown == []
    plain = RefImageStrip(lambda: None)
    assert plain.again_button.isHidden() and plain.portrait_button.isHidden()
    strip.deleteLater()
    plain.deleteLater()
