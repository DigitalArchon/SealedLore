"""A story's character and the card they came from (engine/card_link.py):
what a story is given, the changes told apart by the side they were made on,
and each side's changes saved to the other."""

from __future__ import annotations

from sealedlore.engine import card_link
from sealedlore.engine.card_link import BOTH, CARD, STORY, changes, given, story_copy
from sealedlore.models.character import Character
from sealedlore.models.collection import PictureMark
from sealedlore.models.image import ImageRef
from sealedlore.storage import collection


def elena():
    card = collection.kept("Elena")
    card.full_description = "{{char}} keeps the farmhouse. She trusts {{user}} slowly."
    card.personality = "wary"
    card.tavern.mes_example = "<START>\n{{user}}: Hello?\n{{char}}: Hands up."
    card.reference_images = [ImageRef(id="p", file="images/refs/p.png", look="coat")]
    return card


MARK_P = PictureMark(sha256="p", look="coat")


def linked(card):
    copy = story_copy(card, [card], "John")
    copy.linked.pictures = [MARK_P]
    copy.reference_images = [ImageRef(id="sp", file="images/refs/sp.png", look="coat")]
    return copy


def test_a_story_is_given_the_card_with_the_names_in():
    card = elena()
    copy = story_copy(card, [card], "John")
    assert copy.id != card.id and copy.collection is None
    assert copy.full_description == "Elena keeps the farmhouse. She trusts John slowly."
    assert copy.summary == "Elena keeps the farmhouse."
    assert copy.voice_notes == "John: Hello?\nElena: Hands up."
    assert copy.tavern.mes_example.startswith("<START>\nJohn: Hello?")
    assert copy.linked.card_id == card.id and copy.linked.user == "John"
    assert copy.linked.fields == given(card, "John")
    # The card itself keeps its names as written.
    assert "{{char}}" in card.full_description


def test_nothing_changed_is_no_change():
    card = elena()
    assert changes(linked(card), card, [MARK_P], [MARK_P]) == []


def test_each_change_is_told_by_the_side_it_was_made_on():
    card = elena()
    copy = linked(card)
    copy.full_description = "Elena keeps the farmhouse, and a dog."
    card.personality = "wary, loyal"
    copy.canon = "Doomsville"
    card.canon = "Hellsville"
    found = {c.key: c.side for c in changes(copy, card, [MARK_P], [MARK_P])}
    assert found == {"full_description": STORY, "personality": CARD, "canon": BOTH}


def test_the_same_change_on_both_sides_is_none():
    card = elena()
    copy = linked(card)
    copy.personality = card.personality = "loyal"
    assert changes(copy, card, [MARK_P], [MARK_P]) == []


def test_pictures_are_told_apart_by_their_bytes_and_labels():
    card = elena()
    copy = linked(card)
    scar = PictureMark(sha256="s", look="coat", expression="angry")
    armour = PictureMark(sha256="a", look="armour")
    relabelled = PictureMark(sha256="p", look="winter coat")
    found = changes(copy, card, [MARK_P, scar], [relabelled, armour])
    sides = {c.key: c.side for c in found}
    assert sides == {"picture:s": STORY, "picture:a": CARD, "labels:p": CARD}
    words = {c.key: c.label for c in found}
    assert words["picture:s"] == "A new picture: coat · angry"


def test_the_storys_changes_go_to_the_card_with_its_names_as_written():
    card = elena()
    copy = linked(card)
    copy.full_description = "Elena keeps the farmhouse. She trusts John now."
    scar = PictureMark(sha256="s", look="coat", expression="angry")
    scar_ref = ImageRef(id="ss", file="images/refs/ss.png", look="coat", expression="angry")
    copy.reference_images.append(scar_ref)
    found = changes(copy, card, [MARK_P, scar], [MARK_P])
    copied: list[str] = []

    def copy_in(ref: ImageRef) -> ImageRef:
        copied.append(ref.id)
        return ref.model_copy(update={"id": "new", "file": "images/refs/new.png"})

    card_link.save_to_card(
        copy,
        card,
        found,
        story_refs={"p": copy.reference_images[0], "s": scar_ref},
        card_refs={"p": card.reference_images[0]},
        copy=copy_in,
    )
    assert card.full_description == "{{char}} keeps the farmhouse. She trusts {{user}} now."
    assert copied == ["ss"] and card.reference_images[-1].expression == "angry"
    # Saved once: nothing is left to say.
    assert changes(copy, card, [MARK_P, scar], [MARK_P, scar]) == []


def test_the_cards_changes_come_into_the_story():
    card = elena()
    copy = linked(card)
    card.personality = "wary, loyal"
    card.reference_images.append(ImageRef(id="a", file="images/refs/a.png", look="armour"))
    armour = PictureMark(sha256="a", look="armour")
    found = changes(copy, card, [MARK_P], [MARK_P, armour])
    card_link.take_from_card(
        copy,
        card,
        found,
        story_refs={"p": copy.reference_images[0]},
        card_refs={"p": card.reference_images[0], "a": card.reference_images[1]},
        copy=lambda ref: ref.model_copy(update={"id": "in"}),
    )
    assert copy.personality == "wary, loyal"
    assert copy.reference_images[-1].id == "in"
    assert changes(copy, card, [MARK_P, armour], [MARK_P, armour]) == []


def test_kept_in_the_story_is_said_quietly_until_there_is_more():
    card = elena()
    copy = linked(card)
    copy.personality = "harder now"
    found = changes(copy, card, [MARK_P], [MARK_P])
    assert card_link.summary(found, copy.linked) == "Changed in this story: personality."
    copy.linked.kept_here = card_link.fingerprint(found)
    assert "(kept here)" in card_link.summary(found, copy.linked)
    copy.summary = "Someone else."
    found = changes(copy, card, [MARK_P], [MARK_P])
    assert "(kept here)" not in card_link.summary(found, copy.linked)


def test_a_new_version_is_the_story_s_copy_with_the_cards_names_back():
    card = elena()
    copy = linked(card)
    copy.full_description = "Elena keeps the farmhouse. She trusts John at last."
    version = card_link.version_from_story(copy, card, [card], name="After the war")
    assert version.id not in (card.id, copy.id)
    assert version.full_description == "{{char}} keeps the farmhouse. She trusts {{user}} at last."
    assert version.collection.family == card.id
    assert version.collection.version == "After the war"
    assert version.tavern.mes_example == card.tavern.mes_example


def test_in_step_takes_what_both_sides_came_to():
    card = elena()
    copy = linked(card)
    copy.personality = card.personality = "loyal"
    card_link.in_step(copy, card, [MARK_P], [MARK_P])
    card.personality = "loyal, tired"
    assert [(c.key, c.side) for c in changes(copy, card, [MARK_P], [MARK_P])] == [
        ("personality", CARD)
    ]


# --- what the review found (Nov 4 2026) ---------------------------------------------------

MARK_A = PictureMark(sha256="a", look="armour")


def with_armour():
    card = elena()
    card.reference_images.append(ImageRef(id="a", file="images/refs/a.png", look="armour"))
    copy = linked(card)
    copy.linked.pictures = [MARK_P, MARK_A]
    return card, copy


def sides_after_in_step(copy, card, story, theirs):
    card_link.in_step(copy, card, story, theirs)
    return {c.key: c.side for c in changes(copy, card, story, theirs)}


def test_a_picture_changed_on_one_side_stays_that_sides_change():
    card, copy = with_armour()
    # Taken off in the story: still the story's change after reconciling.
    assert sides_after_in_step(copy, card, [MARK_P], [MARK_P, MARK_A]) == {"picture:a": STORY}
    card, copy = with_armour()
    assert sides_after_in_step(copy, card, [MARK_P, MARK_A], [MARK_P]) == {"picture:a": CARD}
    card, copy = with_armour()
    # The story makes the armour its portrait: the story's, and nothing re-sorted.
    assert sides_after_in_step(copy, card, [MARK_A, MARK_P], [MARK_P, MARK_A]) == {
        "portrait": STORY
    }
    assert [m.sha256 for m in copy.linked.pictures] == ["p", "a"]


def test_a_picture_that_cant_be_read_is_never_said_taken_off():
    card, copy = with_armour()
    found = changes(copy, card, [MARK_P], [MARK_P, MARK_A], story_unknown=True)
    assert found == []


def test_names_go_back_however_the_card_spells_them():
    card = elena()
    card.full_description = "{{Char}} keeps the farmhouse. She trusts <USER> slowly."
    copy = linked(card)
    copy.full_description = "Elena keeps the farmhouse. She trusts John at last."
    found = changes(copy, card, [MARK_P], [MARK_P])
    card_link.save_to_card(copy, card, found, story_refs={}, card_refs={}, copy=lambda r: None)
    assert card.full_description == "{{char}} keeps the farmhouse. She trusts {{user}} at last."


def test_a_shorter_name_never_eats_a_longer_one():
    raw = "{{char}} trusts {{user}}."
    assert card_link.unfilled("Jane Moss trusts Jane.", raw, "Jane Moss", "Jane") == (
        "{{char}} trusts {{user}}."
    )
    # The card names her outright: her name stays, only the other goes back.
    assert card_link.unfilled(
        "Jane Moss trusts Jane.", "Jane Moss trusts {{user}}.", "Jane Moss", "Jane"
    ) == ("Jane Moss trusts {{user}}.")
    assert card_link.unfilled("Dr. J. waits.", "{{char}} waits.", "Dr. J.", "") == "{{char}} waits."


def test_a_voice_saved_back_takes_its_names_from_the_examples():
    card = elena()
    copy = linked(card)
    copy.voice_notes = "John: Hello?\nElena: Hands up, stranger."
    found = changes(copy, card, [MARK_P], [MARK_P])
    card_link.save_to_card(copy, card, found, story_refs={}, card_refs={}, copy=lambda r: None)
    assert card.voice_notes == "{{user}}: Hello?\n{{char}}: Hands up, stranger."
    # Another story played as Tom gets Tom.
    assert given(card, "Tom")["voice_notes"] == "Tom: Hello?\nElena: Hands up, stranger."


def test_a_version_keeps_the_cards_own_text_where_the_story_changed_nothing():
    card = elena()
    copy = linked(card)
    copy.personality = "harder now"
    version = card_link.version_from_story(copy, card, [card], name="Later")
    assert version.voice_notes is None  # still the examples', as the card has it
    assert version.summary is None
    assert version.full_description == card.full_description
    assert version.personality == "harder now"
    assert given(version, "Tom")["voice_notes"].startswith("Tom: Hello?")


def test_a_card_kept_from_a_story_shows_no_change_nobody_made():
    story_character = Character(name="Gus", full_description="Gus runs the bar. He hears all.")
    kept = card_link.kept_from_story(story_character, "the story Doomsville")
    card_link.relink(story_character, kept, [], main_id=kept.id)
    assert changes(story_character, kept, [], []) == []


def test_after_a_save_a_summary_the_story_never_touched_follows_its_description():
    card = elena()
    copy = linked(card)
    copy.full_description = "Elena runs the clinic now. She trusts John slowly."
    found = changes(copy, card, [MARK_P], [MARK_P])
    card_link.save_to_card(copy, card, found, story_refs={}, card_refs={}, copy=lambda r: None)
    assert copy.summary == "Elena runs the clinic now."
    assert changes(copy, card, [MARK_P], [MARK_P]) == []
