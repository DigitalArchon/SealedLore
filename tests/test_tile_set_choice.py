"""How a new world's tile set is chosen (the author, Oct 18 2026): "Let the
user either choose a set manually, or have the AI choose the closest set, or
let the user choose to ask the AI to create a new set with guidance of how
it should look, including optional sample images". Before, a world whose
words named no set got the first set by name: a river valley of map shops
and toll bridges was drawn as an elven forest."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from sealedlore.engine.catalog import model_info
from sealedlore.engine.tile_draw import parse_pick, pick_messages, tileset_messages
from sealedlore.engine.tile_sets import TileSetInfo
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.providers.anthropic import render_messages
from sealedlore.providers.anthropic_models import listing_entry
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.wire import to_wire_messages, without_pictures
from sealedlore.storage.repository import read_api_log
from sealedlore.storage.tile_sets import shipped_sets
from tests.test_area import make_session, turn

PICTURE = "data:image/jpeg;base64," + "A" * 4000
VALLEY = (
    "Low fantasy, late autumn: a hill village of timbered houses and a map shop, a river "
    "road down the valley past an old stone toll bridge to a market town."
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- pictures in a request ---------------------------------------------------------


def _with_picture() -> list[PromptMessage]:
    return [
        PromptMessage(role="system", parts=(ContentPart(text="You design."),)),
        PromptMessage(
            role="user",
            parts=(ContentPart(text="The world."), ContentPart(text="A bar.", image=PICTURE)),
        ),
    ]


def test_a_picture_goes_after_its_words_on_either_wire_and_never_into_the_log():
    plain = to_wire_messages(_with_picture(), use_cache_control=False)
    assert plain[0] == {"role": "system", "content": "You design."}
    assert plain[1]["content"] == [
        {"type": "text", "text": "The world."},
        {"type": "text", "text": "\n\nA bar."},
        {"type": "image_url", "image_url": {"url": PICTURE}},
    ]
    cached = to_wire_messages(_with_picture(), use_cache_control=True)
    assert cached[1]["content"][-1] == {"type": "image_url", "image_url": {"url": PICTURE}}
    system, messages = render_messages(_with_picture(), use_cache_control=False)
    assert system == [{"type": "text", "text": "You design."}]
    assert messages[0]["content"][-1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "A" * 4000},
    }
    # The log keeps a note of each picture, never its bytes.
    logged = json.dumps(without_pictures({"plain": plain, "anthropic": messages}))
    assert "AAAA" not in logged and logged.count("[a picture, 2 KB]") == 2
    # Without a picture, nothing changes.
    words = [PromptMessage(role="user", parts=(ContentPart(text="Hello."),))]
    assert to_wire_messages(words, use_cache_control=False) == [
        {"role": "user", "content": "Hello."}
    ]


def test_the_designer_sees_the_author_s_pictures_each_after_what_it_shows():
    before = tileset_messages("A valley.", description="timber and slate")
    assert len(before[1].parts) == 1  # no pictures: the request as it always was
    messages = tileset_messages(
        "A valley.",
        description="timber and slate",
        pictures=[(PICTURE, "  a typical bar in this world "), (PICTURE, "")],
    )
    parts = messages[1].parts
    assert "never refer to them" in parts[1].text and not parts[1].image
    assert (parts[2].text, parts[2].image) == ("Picture 1: a typical bar in this world", PICTURE)
    assert parts[3].text == "Picture 2: (the author said nothing of it)"


def test_a_listing_says_which_models_read_pictures():
    assert model_info("glm", {"capabilities": {"vision": False}}).vision is False
    assert model_info("k3", {"architecture": {"input_modalities": ["text", "image"]}}).vision
    assert model_info("old", {}).vision is None
    claude = listing_entry({"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"})
    assert model_info("Anthropic::claude-opus-5-5", claude).vision is True


# --- the AI's choice ---------------------------------------------------------------


def test_the_ai_is_shown_every_set_and_held_to_one_of_them():
    sets = {name: info for name, (info, _folder) in shipped_sets().items()}
    text = pick_messages(VALLEY, sets)[1].text
    assert text.startswith("THE WORLD:\n" + VALLEY)
    for name, info in sets.items():
        assert f"- {name}: {info.title}" in text
    names = list(sets)
    reply = json.dumps({"set": names[0].upper(), "why": "A timbered village by a river."})
    assert parse_pick(reply, names) == (names[0], "A timbered village by a river.")
    with pytest.raises(ValueError):
        parse_pick('{"set": "elsewhere", "why": "no"}', names)
    with pytest.raises(ValueError):
        parse_pick("I'd say the village one.", names)


class _Detaching(MockChatProvider):
    """The session's provider, with a client of its own for the choice."""

    def __init__(self, responses, picks) -> None:
        super().__init__(responses)
        self.picks = MockChatProvider(picks)

    def detached(self) -> MockChatProvider:
        return self.picks


def _pick_session(tmp_path, story, cast, picks, responses=("Rain.", "{}")):
    story.world_bible = VALLEY
    session = make_session(tmp_path, story, cast)
    session.provider = _Detaching(list(responses), picks)
    return session


def test_the_ai_chooses_the_closest_set_once_the_story_begins_and_says_why(tmp_path, story, cast):
    names = sorted(name for name in shipped_sets())
    chosen = next(n for n in names if n != "silverwood")
    why = "Timbered houses and a stone bridge in autumn."
    session = _pick_session(tmp_path, story, cast, [json.dumps({"set": chosen, "why": why})])
    list(session.send(turn("I look around.")))
    session._pick_thread.join(5)
    art = session.map_art_copy()
    assert (art.tile_set, art.tile_set_auto, art.tile_set_why) == (chosen, True, why)
    request = session.provider.picks.last_request
    assert request.model == session.map_model and VALLEY in request.messages[1].text
    title = dict(session.tile_set_choices())[chosen]
    assert session.tile_set_offer() == (
        f"The map is drawn in {title}, the set the AI found closest to this world: "
        "Timbered houses and a stone bridge in autumn. Choose another, or design one for "
        "this world."
    )
    kinds = [e["kind"] for e in read_api_log(session.story.id, session.root)]
    assert "map_tileset_pick_request" in kinds and "map_tileset_pick_response" in kinds
    # Kept: asked once.
    list(session.send(turn("I wait.")))
    assert len(session.provider.picks.requests) == 1
    # The author's choice stands over it, and "the AI's choice" asks again.
    session.set_tile_set("brasshaven")
    assert session.map_art_copy().tile_set_why == ""
    session.provider.picks.responses = [json.dumps({"set": chosen, "why": "Again."})]
    session.set_tile_set("")
    session._pick_thread.join(5)
    assert session.map_art_copy().tile_set_why == "Again."


def test_when_the_ai_can_t_choose_the_setting_s_words_do(tmp_path, story, cast):
    session = _pick_session(tmp_path, story, cast, ["the village one, I think"])
    list(session.send(turn("I look around.")))
    session._pick_thread.join(5)
    art = session.map_art_copy()
    assert art.tile_set and art.tile_set_auto and art.tile_set_why == ""
    kinds = [e["kind"] for e in read_api_log(session.story.id, session.root)]
    assert "map_tileset_pick_error" in kinds


def test_the_map_s_things_wait_while_the_set_is_chosen_or_drawn(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)

    class Alive:
        def is_alive(self) -> bool:
            return True

    assert not session.tile_set_waiting()
    session._pick_thread = Alive()
    assert session.tile_set_picking and session.tile_set_waiting()
    assert session.tile_set_offer() == "The AI is choosing the tile set closest to this world…"
    session._pick_thread = None
    session._own_set_thread = Alive()
    assert session.tile_set_waiting() and not session.tile_set_picking
    # Not chosen meanwhile: the author's set is chosen once it is in.
    session.story.defaults.world_map = True
    list(session.send(turn("I look around.")))
    assert session.map_art_copy().tile_set == ""


def test_start_offers_the_map_s_look_for_a_new_world_only(app, tmp_path, story, cast):
    from sealedlore.gui.start_dialog import AI_CHOOSES, DESIGN, DESIGN_ONE, StartDialog

    session = make_session(tmp_path, story, cast)
    sets = session.tile_sets_at_start()
    assert sets == session.tile_set_choices()
    dialog = StartDialog(story, cast, tile_sets=sets)
    combo = dialog.tile_set
    assert combo.itemText(0) == AI_CHOOSES and combo.itemText(combo.count() - 1) == DESIGN_ONE
    assert dialog.tile_set_choice() == ""
    combo.setCurrentIndex(combo.count() - 1)
    assert dialog.tile_set_choice() == DESIGN
    combo.setCurrentIndex(1)
    assert dialog.tile_set_choice() == sets[0][0]
    dialog.deleteLater()
    # A map with its set already (a sample's, or the author's): not asked.
    session.set_tile_set("brasshaven")
    assert session.tile_sets_at_start() == []
    assert StartDialog(story, cast).tile_set_choice() is None


def _jpeg(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(120, 90, 60))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPEG")
    return bytes(buffer.data())


def test_the_designer_sends_pictures_with_their_words_to_a_model_that_reads_them(
    app, tmp_path, story, cast, monkeypatch
):
    from sealedlore.gui.ref_images import image_of
    from sealedlore.gui.tile_set_gallery import (
        DESIGNER_PICTURE_SIDE,
        MAX_WORLD_PICTURES,
        TileSetDesigner,
    )

    session = make_session(tmp_path, story, cast)
    dialog = TileSetDesigner(session)
    dialog.add_world_picture(_jpeg(3000, 2000), "a typical bar in this world")
    dialog.add_world_picture(_jpeg(400, 300))
    sent = dialog.world_pictures()
    assert [caption for _data, caption in sent] == ["a typical bar in this world", ""]
    big = image_of(sent[0][0])
    assert max(big.width(), big.height()) == DESIGNER_PICTURE_SIDE
    for _ in range(MAX_WORLD_PICTURES):
        dialog.add_world_picture(_jpeg(10, 10))
    assert len(dialog._pictures) == MAX_WORLD_PICTURES
    assert not dialog.add_picture.isEnabled()
    # A designer that reads no pictures is sent none, and the dialog says so.
    monkeypatch.setattr(session, "reads_pictures", lambda _model: False)
    dialog._sync_pictures_note()
    assert dialog.world_pictures() == [] and "doesn't read pictures" in (
        dialog.pictures_note.text()
    )
    dialog.deleteLater()


def test_the_gallery_lets_the_ai_choose_again_and_says_why(app, tmp_path, story, cast):
    from sealedlore.gui.tile_set_gallery import TileSetGallery

    names = sorted(shipped_sets())
    session = _pick_session(
        tmp_path, story, cast, [json.dumps({"set": names[0], "why": "It fits the valley."})]
    )
    list(session.send(turn("I look around.")))
    session._pick_thread.join(5)
    gallery = TileSetGallery(session)
    assert "the closest to this world: It fits the valley." in gallery.heading.text()
    session.provider.picks.responses = [json.dumps({"set": names[1], "why": "Closer still."})]
    gallery.pick_button.click()
    session._pick_thread.join(5)
    gallery._check_pick()
    assert "Closer still." in gallery.heading.text()
    assert next(card for card in gallery.cards if card.use_button.isHidden() is False)
    assert session.map_art_copy().tile_set == names[1]
    gallery.deleteLater()


def test_a_set_s_manifest_has_what_the_ai_is_shown():
    for name, (info, _folder) in shipped_sets().items():
        assert isinstance(info, TileSetInfo)
        assert info.title and info.words.get("world") and info.words.get("style"), name
