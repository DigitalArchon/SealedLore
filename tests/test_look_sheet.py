"""A character's pictures as a sheet of looks and expressions, its gaps and
what each is drawn from (engine/look_sheet.py). Pure."""

from __future__ import annotations

from sealedlore.engine.look_sheet import (
    DEFAULT_EXPRESSIONS,
    add_label,
    gap_change,
    label_key,
    relabel,
    sheet_for,
    split_wanted,
    wanted_along,
)
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
    assert gap_change(armour) == "Change their expression or pose to: angry."
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
    assert gap_change(gap) == "Change their expression or pose to: happy."


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
