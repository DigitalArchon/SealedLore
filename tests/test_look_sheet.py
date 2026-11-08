"""A character's pictures as a sheet of looks and expressions, its gaps and
what each is drawn from (engine/look_sheet.py). Pure."""

from __future__ import annotations

from sealedlore.engine.look_sheet import (
    DEFAULT_EXPRESSIONS,
    add_label,
    gap_change,
    keep_order,
    label_key,
    move,
    name_unnamed,
    relabel,
    remove_label,
    reorder,
    set_aside,
    set_wording,
    sheet_change,
    sheet_for,
    split_wanted,
    usual_wording,
    wanted_along,
    wording,
)
from sealedlore.engine.prompt_texts import PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.node import Node


def picture(ref_id: str, look: str = "", expression: str = "") -> ImageRef:
    return ImageRef(id=ref_id, file=f"images/refs/{ref_id}.jpg", look=look, expression=expression)


def john() -> Character:
    """Doomsville's John, as the sample has him."""
    return Character(
        id="john",
        name="John",
        reference_images=[
            picture("portrait", "winter coat", "neutral"),
            picture("happy", "winter coat", "happy"),
            picture("sad", "winter coat", "sad"),
            picture("angry", "winter coat", "angry"),
            picture("afraid", "winter coat", "afraid"),
            picture("armour", "combat armour", "neutral"),
            picture("indoors", "indoor clothes", "neutral"),
        ],
    )


def test_rows_are_the_looks_and_columns_the_usual_expressions():
    sheet = sheet_for(john())
    assert sheet.looks == ["winter coat", "combat armour", "indoor clothes"]
    assert sheet.expressions == list(DEFAULT_EXPRESSIONS)
    assert [r.id for r in sheet.pictures("winter coat", "angry")] == ["angry"]
    assert sheet.portrait_id == "portrait"
    assert sheet.picture_count == 7 and sheet.unlabelled == []


def test_a_gap_is_drawn_from_its_own_looks_neutral_picture():
    """The clothes, light and background of the look stay; only the
    expression is asked for."""
    sheet = sheet_for(john())
    gaps = sheet.drawable()
    assert len(gaps) == 8
    armour = sheet.gap("combat armour", "angry")
    assert armour.source.id == "armour"
    assert gap_change(armour) == (
        "Change their expression or pose to: an angry expression, brows drawn together and the "
        "jaw set."
    )
    assert sheet.gap("indoor clothes", "sad").source.id == "indoors"
    assert sheet.gap("winter coat", "happy") is None  # not a gap: it has its picture


def test_a_planned_look_starts_from_one_cell_and_the_rest_follow_it():
    """Drawn from the portrait each, the cells of a new look would each get
    their own armour: its first picture is drawn first, the rest from it."""
    character = john()
    character.sheet_looks = ["formal clothes"]
    sheet = sheet_for(character)
    assert sheet.looks[-1] == "formal clothes"
    first = sheet.gap("formal clothes", "neutral")
    assert first.source.id == "portrait"
    assert gap_change(first) == "Change their clothes or state to: formal clothes."
    for expression in ("happy", "sad", "angry", "afraid"):
        assert sheet.gap("formal clothes", expression).source is None
    assert first in sheet.drawable()


def test_a_new_looks_first_cell_starts_from_its_expression_when_there_is_no_neutral():
    character = Character(
        id="c",
        name="C",
        reference_images=[picture("p", "coat", "grim"), picture("h", "coat", "happy")],
        sheet_looks=["armour"],
        sheet_expressions=["happy", "grim"],
    )
    sheet = sheet_for(character)
    first = sheet.gap("armour", "happy")  # the first column: no neutral one
    assert first.source.id == "h"
    assert gap_change(first) == "Change their clothes or state to: armour."
    assert sheet.gap("armour", "grim").source is None


def test_labels_match_whatever_their_case_and_spacing():
    character = Character(
        id="c",
        name="C",
        reference_images=[
            picture("a", "Combat Armour", "neutral"),
            picture("b", "combat  armour", "Happy"),
        ],
    )
    sheet = sheet_for(character)
    assert sheet.looks == ["Combat Armour"]
    assert [r.id for r in sheet.pictures("combat armour", "happy")] == ["b"]
    assert label_key(" Combat  ARMOUR ") == "combat armour"


def test_unnamed_halves_and_unlabelled_pictures():
    """Imported expression packs have no look; a picture with neither label
    is kept apart, to be labelled."""
    character = Character(
        id="c",
        name="C",
        reference_images=[
            picture("p"),
            picture("joy", "", "joy"),
            picture("coat", "coat", ""),
        ],
    )
    sheet = sheet_for(character)
    assert [r.id for r in sheet.unlabelled] == ["p"]
    assert sheet.looks == ["coat", ""]
    assert sheet.expressions == [*DEFAULT_EXPRESSIONS, "joy", ""]
    assert [r.id for r in sheet.pictures("", "joy")] == ["joy"]
    assert [r.id for r in sheet.pictures("coat", "")] == ["coat"]
    # A gap in the unnamed row asks for no clothes.
    gap = sheet.gap("", "happy")
    assert gap.source.id == "joy"
    assert gap_change(gap) == (
        "Change their expression or pose to: a happy expression with a bright smile."
    )


def test_what_the_story_asked_for_is_on_its_cell_or_listed():
    wanted = {"combat armour · angry": 3, "winter coat · tense, ready to fight": 1}
    sheet = sheet_for(john(), wanted)
    assert sheet.gap("combat armour", "angry").wanted == 3
    assert sheet.asked == [("winter coat · tense, ready to fight", 1)]
    assert split_wanted("combat armour · angry") == ("combat armour", "angry")
    assert split_wanted("grinning") == ("", "grinning")


def test_what_the_story_asked_for_is_counted_along_the_path():
    first = Node(kind="assistant", speaker_id="__narrator__", content="One.")
    first.meta.looks_wanted = {"john": "combat armour · angry"}
    second = Node(kind="assistant", speaker_id="__narrator__", content="Two.")
    second.meta.looks_wanted = {"john": "combat armour ·  angry", "elena": "apron · afraid"}
    asked = wanted_along([first, second])
    assert asked["john"] == {"combat armour · angry": 2}
    assert asked["elena"] == {"apron · afraid": 1}


def test_custom_columns_and_rows_are_added_once():
    character = john()
    assert add_label(character.sheet_looks, "riot gear")
    assert not add_label(character.sheet_looks, "Riot  Gear")
    character.sheet_expressions = [*DEFAULT_EXPRESSIONS, "laughing"]
    sheet = sheet_for(character)
    assert sheet.expressions[-1] == "laughing"
    assert sheet.looks[-1] == "riot gear"


def test_renaming_a_row_or_column_relabels_every_picture_in_it():
    character = john()
    assert relabel(character, look=("winter coat", "parka")) == 5
    assert {r.look for r in character.reference_images[:5]} == {"parka"}
    assert relabel(character, expression=("afraid", "scared")) == 1
    assert "scared" in character.sheet_expressions
    assert "afraid" not in character.sheet_expressions
    assert sheet_for(character).pictures("parka", "scared")[0].id == "afraid"
    # Into a row that exists: the two become one.
    relabel(character, look=("combat armour", "parka"))
    assert sheet_for(character).looks[:2] == ["parka", "indoor clothes"]


# --- the order stays as the author sees it (Nov 7 2026) ------------------------------------


def test_a_card_never_changed_reads_in_the_old_order():
    """Pictures' looks in card order, then the planned ones: nothing moves the
    day the order began to be kept."""
    character = john()
    character.sheet_looks = ["formal clothes"]
    assert sheet_for(character).looks == [
        "winter coat",
        "combat armour",
        "indoor clothes",
        "formal clothes",
    ]


def test_once_kept_a_portrait_changed_moves_no_row():
    character = john()
    assert keep_order(character)
    assert character.sheet_ordered
    before = sheet_for(character).looks
    # The armour picture made the portrait: it was its look's row at the top.
    character.reference_images.insert(0, character.reference_images.pop(5))
    assert sheet_for(character).looks == before
    assert not keep_order(character)  # nothing new to keep


def test_once_kept_a_row_keeps_its_place_when_its_last_picture_moves_out():
    character = john()
    keep_order(character)
    character.reference_images[6].look = "winter coat"  # the indoor clothes picture
    character.reference_images[6].expression = "surprised"
    sheet = sheet_for(character)
    assert sheet.looks == ["winter coat", "combat armour", "indoor clothes"]
    assert sheet.gap("indoor clothes", "neutral") is not None


def test_a_new_look_goes_after_the_rows_already_there():
    """A portrait drawn in a new look went first: it is last instead."""
    character = john()
    keep_order(character)
    character.reference_images.insert(0, picture("new", "riding gear", "neutral"))
    assert sheet_for(character).looks[-1] == "riding gear"
    assert keep_order(character)
    assert character.sheet_looks[-1] == "riding gear"


def test_the_usual_columns_stay_blank_and_a_pictures_own_is_kept():
    character = john()
    keep_order(character)
    assert character.sheet_expressions == []
    character.reference_images.append(picture("joy", "winter coat", "laughing"))
    character.reference_images.append(picture("grim", "winter coat", "grim"))
    keep_order(character)
    assert character.sheet_expressions == [*DEFAULT_EXPRESSIONS, "laughing", "grim"]
    # The laughing one made the portrait: the columns stay.
    character.reference_images.insert(0, character.reference_images.pop())
    assert sheet_for(character).expressions[-2:] == ["laughing", "grim"]


def test_the_author_moves_rows_and_columns():
    character = john()
    assert move(character, "indoor clothes", -1, look=True)
    assert sheet_for(character).looks == ["winter coat", "indoor clothes", "combat armour"]
    assert not move(character, "winter coat", -1, look=True)  # first already
    assert move(character, "afraid", -4)
    assert sheet_for(character).expressions[:2] == ["afraid", "neutral"]
    assert reorder(character, looks=["combat armour"])
    assert sheet_for(character).looks == ["combat armour", "winter coat", "indoor clothes"]
    assert not reorder(character, looks=["combat armour"])


def test_the_unnamed_row_stays_last():
    character = Character(
        id="c",
        name="C",
        reference_images=[picture("joy", "", "happy"), picture("coat", "coat", "neutral")],
    )
    reorder(character, looks=["coat"])
    assert sheet_for(character).looks == ["coat", ""]


def test_a_row_taken_off_takes_its_words_with_it():
    character = john()
    character.sheet_looks = ["formal clothes"]
    set_wording(character, "formal clothes", "a dark suit and tie", look=True)
    remove_label(character, look="formal clothes")
    assert "formal clothes" not in sheet_for(character).looks
    assert character.look_wording == {}
    # The rest stayed as they were.
    assert character.sheet_looks == ["winter coat", "combat armour", "indoor clothes"]


# --- a heading, and the words it is drawn with (Nov 7 2026) --------------------------------


def test_a_column_is_drawn_with_the_usual_words_or_the_cards_own():
    character = john()
    assert wording(character, "happy") == "a happy expression with a bright smile"
    assert wording(character, "smug") == "smug"  # no usual words: the heading
    assert set_wording(character, "Happy", "  happy but reserved, a small smile.  ")
    assert character.expression_wording == {"happy": "happy but reserved, a small smile"}
    assert wording(character, "happy") == "happy but reserved, a small smile"
    assert not set_wording(character, "happy", "happy but reserved, a small smile")
    assert set_wording(character, "happy", "")
    assert wording(character, "happy") == usual_wording("happy")


def test_a_gap_is_drawn_with_its_cards_words():
    character = john()
    set_wording(character, "angry", "furious, teeth bared")
    set_wording(character, "formal clothes", "a dark suit and tie", look=True)
    character.sheet_looks = ["formal clothes"]
    sheet = sheet_for(character)
    assert gap_change(sheet.gap("combat armour", "angry"), character=character) == (
        "Change their expression or pose to: furious, teeth bared."
    )
    assert gap_change(sheet.gap("formal clothes", "neutral"), character=character) == (
        "Change their clothes or state to: a dark suit and tie."
    )
    assert sheet_change(character, expression="laughing") == (
        "Change their expression or pose to: laughing."
    )


def test_the_usual_words_are_prompt_texts():
    texts = PromptTexts({"looks.wording.happy": "beaming"})
    assert wording(john(), "happy", texts=texts) == "beaming"
    assert wording(john(), "in a fighting stance").startswith("in a fighting stance")


def test_a_renamed_column_keeps_its_words():
    character = john()
    set_wording(character, "afraid", "terrified, trembling")
    relabel(character, expression=("afraid", "scared"))
    assert character.expression_wording == {"scared": "terrified, trembling"}
    # Renamed onto one with words of its own: those stand.
    set_wording(character, "sad", "weeping")
    relabel(character, expression=("scared", "sad"))
    assert character.expression_wording == {"sad": "weeping"}


def test_a_gap_shows_the_words_its_edit_asks_for_and_no_more():
    """A new look's first cell is drawn from a picture with its expression
    already: it shows the look's words, not the expression's."""
    from sealedlore.engine.look_sheet import gap_words

    character = john()
    character.sheet_looks = ["formal clothes"]
    set_wording(character, "formal clothes", "a dark suit and tie", look=True)
    sheet = sheet_for(character)
    assert gap_words(sheet.gap("formal clothes", "neutral"), character=character) == (
        "a dark suit and tie"
    )
    assert gap_words(sheet.gap("formal clothes", "happy"), character=character) == (
        "a happy expression with a bright smile"
    )
    assert gap_words(sheet.gap("combat armour", "sad"), character=character).startswith(
        "a sad expression"
    )


# --- the unnamed row and column, named or taken off (the author, Nov 8 2026) -------------


def test_the_unnamed_row_and_column_are_named_where_they_stand():
    """A row "No look named" could never be named: its pictures take the
    heading, in the row's own place (last)."""
    character = Character(
        id="c",
        name="C",
        reference_images=[
            picture("coat", "coat", "neutral"),
            picture("joy", "", "joy"),
            picture("glad", "", "happy"),
            picture("plain", "armour", ""),
            picture("aside"),
        ],
    )
    assert sheet_for(character).looks == ["coat", "armour", ""]
    assert name_unnamed(character, "travelling clothes", look=True)
    assert [r.look for r in character.reference_images[1:3]] == ["travelling clothes"] * 2
    sheet = sheet_for(character)
    assert sheet.looks == ["coat", "armour", "travelling clothes"]
    assert [r.id for r in sheet.pictures("travelling clothes", "joy")] == ["joy"]
    # The unnamed column likewise; the picture set aside keeps no label.
    assert sheet.expressions[-1] == ""
    assert name_unnamed(character, "calm", look=False)
    sheet = sheet_for(character)
    assert sheet.expressions[-1] == "calm" and "" not in sheet.expressions
    assert [r.id for r in sheet.pictures("armour", "calm")] == ["plain"]
    assert [r.id for r in sheet.unlabelled] == ["aside"]
    assert not name_unnamed(character, "  ", look=True)


def test_naming_the_empty_row_of_a_sheet_with_nothing_labelled_makes_a_first_look():
    character = Character(id="c", name="C", reference_images=[picture("a"), picture("b")])
    assert sheet_for(character).looks == [""]
    assert name_unnamed(character, "work clothes", look=True)
    assert character.sheet_looks == ["work clothes"]
    sheet = sheet_for(character)
    assert sheet.looks == ["work clothes"] and len(sheet.unlabelled) == 2
    assert sheet.gap("work clothes", "neutral").source is not None


def test_the_unnamed_row_named_as_a_row_already_there_joins_it():
    character = Character(
        id="c",
        name="C",
        reference_images=[picture("coat", "coat", "neutral"), picture("glad", "", "happy")],
    )
    assert name_unnamed(character, "Coat", look=True)
    sheet = sheet_for(character)
    assert sheet.looks == ["coat"]
    assert [r.id for r in sheet.pictures("coat", "happy")] == ["glad"]


def test_a_row_or_column_taken_off_with_its_pictures_sets_them_aside():
    """Nothing is deleted: the pictures go to those set aside, unlabelled."""
    character = john()
    assert set_aside(character, look="combat armour") == 1
    sheet = sheet_for(character)
    assert "combat armour" not in sheet.looks
    assert [r.id for r in sheet.unlabelled] == ["armour"]
    assert len(character.reference_images) == 7
    # The unnamed row goes once its pictures are set aside.
    character.reference_images.append(picture("joy", "", "joy"))
    assert sheet_for(character).looks[-1] == ""
    assert set_aside(character, look="") == 1
    assert "" not in sheet_for(character).looks
    # A column too, its words with it.
    character.expression_wording["happy"] = "grinning"
    assert set_aside(character, expression="happy") == 1
    assert "happy" not in sheet_for(character).expressions
    assert "happy" not in character.expression_wording
