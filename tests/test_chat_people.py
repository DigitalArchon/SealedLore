"""A simple chat's characters, kept for their pictures alone (the author,
Nov 7 2026): brought in by hand, here now or set aside, shown with the
picture chosen for them; nothing of them reaches the chat's model, and their
pictures go with a picture asked for, as a story's do."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.prompt import TurnRequest
from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.node import CHAT_USER_ID
from tests.test_chat import chat_session

MARKER = "ZEBRA-QUILL-7741"


def with_people(tmp_path: Path, replies: list[str] | None = None):
    session, provider = chat_session(tmp_path, replies or ["Hello."])
    nils = session.cast[0]
    nils.full_description = f"A tall man in a {MARKER} coat."
    nils.summary = f"The {MARKER} man."
    for ref_id, look, expression in (
        ("p", "coat", "neutral"),
        ("h", "coat", "happy"),
        ("a", "armour", "neutral"),
    ):
        session.pictures.write(f"images/refs/{ref_id}.png", b"picture")
        nils.reference_images.append(
            ImageRef(id=ref_id, file=f"images/refs/{ref_id}.png", look=look, expression=expression)
        )
    mara = Character(id="c2", name="Mara Venn", full_description=f"{MARKER} again.")
    session.pictures.write("images/refs/m.png", b"picture")
    mara.reference_images.append(ImageRef(id="m", file="images/refs/m.png"))
    session.cast.append(mara)
    return session, provider


def test_nothing_of_a_chats_characters_reaches_its_model(tmp_path: Path):
    session, provider = with_people(tmp_path)
    session.show_look("c1", "h")
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Hi there.")))
    sent = [message.text for request in provider.requests for message in request.messages]
    assert sent and not any(MARKER in text or "Nils" in text for text in sent)


def test_everyone_brought_in_is_here_until_set_aside(tmp_path: Path):
    session, _ = with_people(tmp_path)
    assert [c.id for c in session.chat_present()] == ["c1", "c2"]
    session.set_chat_presence("c2", False)
    assert [c.id for c in session.chat_present()] == ["c1"]
    assert session.story.chat_away == ["c2"]
    session.set_chat_presence("c2", True)
    assert session.story.chat_away == []


def test_a_look_is_chosen_from_the_start_and_kept_for_the_whole_chat(tmp_path: Path):
    """No message to keep it on yet: a chat keeps it for itself."""
    session, _ = with_people(tmp_path)
    assert session.nodes == []
    session.show_look("c1", "a")
    assert session.looks_now() == {"c1": "a"}
    session.show_look("c1", None)
    assert session.looks_now() == {}
    session.show_look("c1", "h")
    # A picture taken off the card is chosen no more.
    session.cast[0].reference_images = [r for r in session.cast[0].reference_images if r.id != "h"]
    assert session.looks_now() == {}
    assert session.looks_wanted_now() == {}


def test_their_pictures_go_with_a_picture_asked_for_those_here_first(tmp_path: Path):
    session, _ = with_people(tmp_path)
    session.set_chat_presence("c1", False)
    session.show_look("c1", "a")
    choices = session.image_ref_choices()
    characters = [c for c in choices if c.use.owner_kind == "character"]
    # Mara is here, so first; Nils, set aside, after, his chosen look first.
    assert [c.use.owner_id for c in characters] == ["c2", "c1", "c1", "c1"]
    assert characters[0].present and not characters[1].present
    assert characters[1].use.ref_id == "a" and characters[1].use.caption.startswith("as they look")


def test_their_card_pictures_are_drawn_and_land_on_them(tmp_path: Path):
    session, _ = with_people(tmp_path)
    assert [c.id for c in session.picture_cards()[0]] == ["c1", "c2"]
    session.pictures.write("images/drawn.png", b"drawn")
    session.pictures.add_images(
        [
            GeneratedImage(
                id="g1",
                file="images/drawn.png",
                prompt="Nils, laughing",
                model="m",
                size="1k",
                card_id="c1",
                card_look="coat",
                card_expression="laughing",
            )
        ]
    )
    assert session.take_card_pictures() == ["Nils Carrow"]
    landed = session.cast[0].reference_images[-1]
    assert (landed.look, landed.expression) == ("coat", "laughing")
