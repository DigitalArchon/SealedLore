"""The other apps' lorebooks SillyTavern imports (NovelAI's, Agnai's, RisuAI's),
converted exactly as its own import converts them (world-info.js,
`convertNovelLorebook`, `convertAgnaiMemoryBook`, `convertRisuLorebook`, and
`importWorldInfo` for which one a file is); and a card's lorebook entries
moved up and down in their own order, in either shape."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from sealedlore.storage import tavern_lore as lore
from sealedlore.storage.png_text import with_text
from sealedlore.storage.tavern_cards import tavern_kind
from sealedlore.storage.tavern_lore import (
    LorebookError,
    UnconvertibleLorebook,
    is_converted_lorebook,
    lorebook_of,
    open_lorebook,
    read_lorebook,
)
from tests.test_tavern import tiny_png, v2

# SillyTavern's new entry (`newWorldInfoEntryTemplate`, from
# `newWorldInfoEntryDefinition`'s defaults, 1.19.0), written out here rather
# than taken from the module, so the module's copy is checked too.
ST_TEMPLATE = {
    "key": [],
    "keysecondary": [],
    "comment": "",
    "content": "",
    "constant": False,
    "vectorized": False,
    "selective": True,
    "selectiveLogic": 0,
    "addMemo": False,
    "order": 100,
    "position": 0,
    "disable": False,
    "ignoreBudget": False,
    "excludeRecursion": False,
    "preventRecursion": False,
    "matchPersonaDescription": False,
    "matchCharacterDescription": False,
    "matchCharacterPersonality": False,
    "matchCharacterDepthPrompt": False,
    "matchScenario": False,
    "matchCreatorNotes": False,
    "delayUntilRecursion": 0,
    "probability": 100,
    "useProbability": True,
    "depth": 4,
    "outletName": "",
    "group": "",
    "groupOverride": False,
    "groupWeight": 100,
    "scanDepth": None,
    "caseSensitive": None,
    "matchWholeWords": None,
    "useGroupScoring": None,
    "automationId": "",
    "role": 0,
    "sticky": None,
    "cooldown": None,
    "delay": None,
    "triggers": [],
}
# What every one of the three converters sets after its own fields, as written
# in each of them: no recursion, before the character, the system's role.
ALIKE = {
    "vectorized": False,
    "selectiveLogic": 0,
    "position": 0,
    "excludeRecursion": False,
    "delayUntilRecursion": False,
    "outletName": "",
    "group": "",
    "groupOverride": False,
    "groupWeight": 100,
    "scanDepth": None,
    "caseSensitive": None,
    "matchWholeWords": None,
    "useGroupScoring": None,
    "automationId": "",
    "role": 0,
    "sticky": None,
    "cooldown": None,
    "delay": None,
    "triggers": [],
    "ignoreBudget": False,
}
ABSENT = object()


def converted(uid: int, **fields) -> dict:
    """An entry as `{...newWorldInfoEntryTemplate, uid, <its fields>}` comes out
    of JSON.stringify: the template's keys in their order, then uid, then
    displayIndex; a field left undefined (ABSENT here) not written at all."""
    entry = {**ST_TEMPLATE, "uid": uid, **ALIKE, "displayIndex": uid, **fields}
    return {key: value for key, value in entry.items() if value is not ABSENT}


def same_json(made, expected) -> None:
    """Compared as JSON text: key order counts, and false is not 0."""
    assert json.dumps(made, indent=1) == json.dumps(expected, indent=1)


# --- NovelAI ---------------------------------------------------------------------------

NOVEL_BOOK = {
    "lorebookVersion": 5,
    "entries": [
        {
            "text": "Hellsville is walled, and wary of strangers.",
            "contextConfig": {
                "prefix": "",
                "suffix": "\n",
                "tokenBudget": 1,
                "reservedTokens": 0,
                "budgetPriority": 400,
                "trimDirection": "trimBottom",
                "insertionType": "newline",
                "maximumTrimType": "sentence",
                "insertionPosition": -1,
            },
            "lastUpdatedAt": 1730000000000,
            "displayName": "Hellsville",
            "id": "5b1e3c2a-0000-4000-8000-000000000001",
            "keys": ["Hellsville", "/the (old )?town/i"],
            "searchRange": 1000,
            "enabled": True,
            "forceActivation": False,
            "keyRelative": False,
            "nonStoryActivatable": False,
            "category": "",
            "loreBiasGroups": [],
        },
        {
            # Always On, with no keys and no name: as there, it loses the first.
            "text": "Four winters after the end of the world.",
            "keys": [],
            "enabled": False,
            "forceActivation": True,
        },
        # Not an entry at all: SillyTavern reads every field as undefined.
        "the farmhouse",
        {"text": "The ridge.", "displayName": "   ", "keys": ["ridge"], "contextConfig": None},
    ],
    "settings": {"orderByKeyLocations": False},
    "categories": [],
}


def test_a_novelai_lorebook_converts_as_sillytavern_converts_it():
    made = lorebook_of(NOVEL_BOOK, "doomsville.lorebook")
    same_json(
        made.book,
        {
            "entries": {
                "0": converted(
                    0,
                    key=["Hellsville", "/the (old )?town/i"],
                    keysecondary=[],
                    comment="Hellsville",  # displayName || ''
                    content="Hellsville is walled, and wary of strangers.",
                    constant=False,
                    selective=False,
                    order=400,  # contextConfig?.budgetPriority ?? 0
                    disable=False,  # !enabled
                    addMemo=True,  # a name that isn't blank
                    probability=100,
                    useProbability=True,
                ),
                "1": converted(
                    1,
                    key=[],
                    keysecondary=[],
                    comment="",
                    content="Four winters after the end of the world.",
                    constant=False,  # its Always On goes
                    selective=False,
                    order=0,
                    disable=True,
                    addMemo=False,
                    probability=100,
                    useProbability=True,
                ),
                "2": converted(
                    2,
                    key=ABSENT,
                    keysecondary=[],
                    comment="",
                    content=ABSENT,
                    constant=False,
                    selective=False,
                    order=0,
                    disable=True,  # !undefined
                    addMemo=False,
                    probability=100,
                    useProbability=True,
                ),
                "3": converted(
                    3,
                    key=["ridge"],
                    keysecondary=[],
                    comment="   ",  # truthy, so kept as it is
                    content="The ridge.",
                    constant=False,
                    selective=False,
                    order=0,  # contextConfig null
                    disable=True,
                    addMemo=False,  # blank once trimmed
                    probability=100,
                    useProbability=True,
                ),
            }
        },
    )
    # What a story would have kept is said: its Always On.
    assert made.left_out == {"always_on": 1}
    assert made.notes() == [
        "NovelAI's Always On, which SillyTavern's import drops too: the entry is sent only "
        "when a keyword is mentioned (1 entry)"
    ]


def test_an_array_entry_has_no_keys_as_there():
    """`[].keys` is a function in JavaScript, which JSON leaves out."""
    made = lorebook_of({"lorebookVersion": 1, "entries": [["Hellsville"]]}, "x.lorebook")
    assert "key" not in made.book["entries"]["0"] and "content" not in made.book["entries"]["0"]


def test_a_name_trimmed_as_javascript_trims():
    """A BOM is white space to JavaScript's trim(), a file separator isn't."""
    book = {
        "lorebookVersion": 5,
        "entries": [{"displayName": "\ufeff", "keys": []}, {"displayName": "\x1c", "keys": []}],
    }
    entries = lorebook_of(book, "x.lorebook").book["entries"]
    assert [entries[n]["addMemo"] for n in ("0", "1")] == [False, True]


# --- Agnai -------------------------------------------------------------------------------

AGNAI_BOOK = {
    "kind": "memory",
    "name": "Doomsville",
    "description": "The town, the farm and the creek.",
    "entries": [
        {
            "name": "The farmhouse",
            "entry": "The farmhouse north of Hellsville has a red door.",
            "keywords": ["farmhouse", "the farm"],
            "priority": 10,
            "weight": 5,
            "enabled": True,
        },
        {"name": "", "entry": "The creek is cold all year.", "keywords": ["creek"], "weight": 1},
        {"entry": "Nobody goes out after dark.", "enabled": 1},
    ],
}


def test_an_agnai_memory_book_converts_as_sillytavern_converts_it():
    made = lorebook_of(AGNAI_BOOK, "doomsville.json")
    shared = {"keysecondary": [], "constant": False, "selective": False}
    shared |= {"probability": 100, "useProbability": True}
    same_json(
        made.book,
        {
            "entries": {
                "0": converted(
                    0,
                    key=["farmhouse", "the farm"],
                    comment="The farmhouse",
                    content="The farmhouse north of Hellsville has a red door.",
                    order=5,  # its weight; its priority goes
                    disable=False,
                    addMemo=True,
                    **shared,
                ),
                "1": converted(
                    1,
                    key=["creek"],
                    comment="",
                    content="The creek is cold all year.",
                    order=1,
                    disable=True,  # no enabled: !undefined
                    addMemo=False,
                    **shared,
                ),
                "2": converted(
                    2,
                    key=ABSENT,
                    comment=ABSENT,
                    content="Nobody goes out after dark.",
                    order=ABSENT,
                    disable=False,  # !1
                    addMemo=False,
                    **shared,
                ),
            }
        },
    )
    assert made.left_out == {}, "nothing a story would have kept is lost"


# --- RisuAI ------------------------------------------------------------------------------

RISU_BOOK = {
    "type": "risu",
    "ver": 1,
    "data": [
        {
            "key": "Hellsville, the town ,",
            "secondkey": "gate, walls",
            "insertorder": 120,
            "comment": "Hellsville",
            "content": "Hellsville is walled.",
            "mode": "normal",
            "alwaysActive": False,
            "selective": True,
            "extentions": {"risu_case_sensitive": False},
            "activationPercent": 40,
            "useRegex": False,
            "bookVersion": 2,
            "id": "risu-1",
        },
        {
            "key": "",
            "secondkey": "",
            "insertorder": 100,
            "comment": "The world",
            "content": "Four winters after the end of the world.",
            "alwaysActive": True,
            "selective": False,
            "activationPercent": 0,
        },
        {"key": "creek", "secondkey": 0, "content": "The creek.", "activationPercent": None},
    ],
}


def test_a_risu_lorebook_converts_as_sillytavern_converts_it():
    made = lorebook_of(RISU_BOOK, "doomsville.json")
    same_json(
        made.book,
        {
            "entries": {
                "0": converted(
                    0,
                    # split(',') and trimmed: the empty last part kept, as there.
                    key=["Hellsville", "the town", ""],
                    keysecondary=["gate", "walls"],
                    comment="Hellsville",
                    content="Hellsville is walled.",
                    constant=False,
                    selective=True,
                    order=120,
                    disable=False,
                    addMemo=True,
                    probability=40,
                    useProbability=40,  # its chance itself
                ),
                "1": converted(
                    1,
                    key=[""],
                    keysecondary=[],
                    comment="The world",
                    content="Four winters after the end of the world.",
                    constant=True,
                    selective=False,
                    order=100,
                    disable=False,
                    addMemo=True,
                    probability=0,
                    useProbability=0,
                ),
                "2": converted(
                    2,
                    key=["creek"],
                    keysecondary=[],  # 0 is false: no second keys
                    comment=ABSENT,
                    content="The creek.",
                    constant=ABSENT,
                    selective=ABSENT,
                    order=ABSENT,
                    disable=False,
                    addMemo=True,
                    probability=100,
                    useProbability=True,
                ),
            }
        },
    )
    assert made.left_out == {}


def test_the_rest_reads_a_converted_book_as_sillytaverns_own():
    """Read down to a story's lore, a converted book says what it leaves out as
    one of SillyTavern's own does, the conversion's losses with it."""
    risu = lorebook_of(RISU_BOOK, "risu.json").lore()
    assert [(e.title, e.keywords, e.always_on, e.enabled) for e in risu.entries] == [
        ("Hellsville", ["Hellsville", "the town"], False, True),
        ("The world", [], True, True),
        ("creek", ["creek"], False, True),
    ]
    assert risu.left_out == {"secondary": 1, "probability": 1}, "a chance of 0 rolls nothing"
    novel = lorebook_of(NOVEL_BOOK, "x.lorebook").lore()
    assert [(e.title, e.keywords, e.enabled) for e in novel.entries] == [
        ("Hellsville", ["Hellsville"], True),
        ("Entry 2", [], False),
        ("ridge", ["ridge"], False),  # a blank name is none
    ]
    assert novel.left_out == {"always_on": 1, "regex": 1, "empty": 1}
    notes = novel.notes()
    assert notes[0].startswith("NovelAI's Always On") and "(1 entry)" in notes[0]
    agnai = lorebook_of(AGNAI_BOOK, "agnai.json").lore()
    assert [(e.title, e.enabled) for e in agnai.entries] == [
        ("The farmhouse", True),
        ("creek", False),
        ("Entry 3", True),
    ]
    # Put on a card, as SillyTavern writes a lorebook file into one.
    card = lore.book_from_world(lorebook_of(RISU_BOOK, "risu.json").book, name="Doomsville")
    first = card["entries"][0]
    assert first["keys"] == ["Hellsville", "the town", ""] and first["insertion_order"] == 120
    assert first["extensions"]["probability"] == 40 and first["secondary_keys"] == ["gate", "walls"]


# --- which one a file is -----------------------------------------------------------------


def test_each_is_told_by_its_mark_as_sillytaverns_import_tells_it():
    assert lore.other_formats({"lorebookVersion": None, "entries": []}) == ["novel"]
    assert lore.other_formats({"kind": "memory", "entries": []}) == ["agnai"]
    assert lore.other_formats({"type": "risu", "data": []}) == ["risu"]
    for unmarked in (
        {"kind": "Memory", "entries": []},
        {"type": "RISU", "data": []},
        {"entries": {}},
        [{"lorebookVersion": 5}],
        "lorebookVersion",
        None,
    ):
        assert lore.other_formats(unmarked) == [], unmarked
    for marked in (NOVEL_BOOK, AGNAI_BOOK, RISU_BOOK, {"type": "risu"}):
        assert lore.looks_like_lorebook(marked)
    # Converted twice there, the pair is no file its server can read.
    with pytest.raises(UnconvertibleLorebook, match="a NovelAI and an Agnai lorebook at once"):
        lorebook_of({**AGNAI_BOOK, "lorebookVersion": 5}, "both.json")


@pytest.mark.parametrize(
    "book, said",
    [
        ({"lorebookVersion": 5}, "a NovelAI lorebook that SillyTavern can't import either: its "),
        ({"lorebookVersion": 5, "entries": {"0": {}}}, "its entries aren't a list"),
        ({"lorebookVersion": 5, "entries": [{}, None]}, "entry 2 is empty"),
        ({"lorebookVersion": 5, "entries": [{"displayName": None}]}, "entry 1's name isn't text"),
        ({"lorebookVersion": 5, "entries": [{"displayName": 7}]}, "entry 1's name isn't text"),
        ({"kind": "memory", "entries": None}, "an Agnai lorebook that SillyTavern can't import"),
        ({"type": "risu", "entries": []}, "a RisuAI lorebook that SillyTavern can't import"),
        ({"type": "risu", "data": [{"content": "c"}]}, "entry 1's keywords aren't text"),
        ({"type": "risu", "data": [{"key": ["a"]}]}, "entry 1's keywords aren't text"),
        ({"type": "risu", "data": [{"key": "a", "secondkey": []}]}, "second keywords aren't"),
        ({"type": "risu", "data": [{"key": "a", "secondkey": 5}]}, "second keywords aren't"),
    ],
)
def test_what_its_converters_stop_on_is_refused_in_words(book, said):
    with pytest.raises(UnconvertibleLorebook) as caught:
        lorebook_of(book, "book.json")
    assert said in str(caught.value) and str(caught.value).startswith("book.json is ")
    assert isinstance(caught.value, LorebookError)


def write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def novel_picture(tmp_path: Path, data, name: str = "doomsville.png") -> Path:
    """A NovelAI lorebook picture: the book as base64 JSON in a "naidata" text."""
    encoded = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
    path = tmp_path / name
    path.write_bytes(with_text(tiny_png(), {"naidata": encoded.rstrip("=")}))
    return path


def test_a_file_is_read_as_sillytavern_reads_it(tmp_path: Path):
    novel = write(tmp_path, "doomsville.lorebook", NOVEL_BOOK)
    agnai = write(tmp_path, "agnai.json", AGNAI_BOOK)
    risu = write(tmp_path, "risu.json", RISU_BOOK)
    for path, book in ((novel, NOVEL_BOOK), (agnai, AGNAI_BOOK), (risu, RISU_BOOK)):
        assert read_lorebook(path) == lorebook_of(book, path.name).book
        assert is_converted_lorebook(path)
    # SillyTavern's own, and a card's book, as they are.
    own = write(tmp_path, "own.json", {"entries": {"0": {"uid": 0, "key": ["a"], "content": "c"}}})
    assert read_lorebook(own)["entries"]["0"]["content"] == "c" and not is_converted_lorebook(own)
    assert not open_lorebook(own).left_out
    # Told apart from a card by File → Import…'s own reader too.
    assert tavern_kind(risu) == tavern_kind(write(tmp_path, "n.json", NOVEL_BOOK)) == "lorebook"


def test_a_novelai_picture_is_read_for_the_lorebook_in_it(tmp_path: Path):
    """extractDataFromPng: the base64 JSON in the "naidata" text. Read as
    UTF-8 (SillyTavern garbles what isn't English), padding or none."""
    book = {**NOVEL_BOOK, "entries": [{**NOVEL_BOOK["entries"][0], "text": "Café Ödland."}]}
    picture = novel_picture(tmp_path, book)
    read = open_lorebook(picture)
    assert read.book["entries"]["0"]["content"] == "Café Ödland."
    assert is_converted_lorebook(picture)
    # A picture holds only another app's lorebook, as there: SillyTavern's own
    # in one is no lorebook to it, nor is a card's picture.
    own = novel_picture(tmp_path, {"entries": {"0": {"uid": 0, "content": "c"}}}, "own.png")
    card = tmp_path / "card.png"
    card.write_bytes(with_text(tiny_png(), {"chara": base64.b64encode(b"{}").decode()}))
    for path in (own, card):
        with pytest.raises(LorebookError, match=f"^{path.name} has no lorebook in it.$"):
            open_lorebook(path)
        assert not is_converted_lorebook(path)
    damaged = tmp_path / "damaged.png"
    damaged.write_bytes(with_text(tiny_png(), {"naidata": "not base64 at all!"}))
    with pytest.raises(LorebookError, match="has a lorebook in it that can't be read"):
        open_lorebook(damaged)
    # A picture's lorebook its converter stops on says why, as any other.
    broken = novel_picture(tmp_path, {"type": "risu", "data": [{"key": 1}]}, "broken.png")
    with pytest.raises(UnconvertibleLorebook, match="entry 1's keywords aren't text"):
        open_lorebook(broken)


def test_a_file_of_none_of_these_shapes_is_refused_in_the_same_words(tmp_path: Path):
    for name, data in (
        ("card.json", v2()),
        ("odd.json", {"entries": "none"}),
        ("list.json", [1, 2]),
        ("nothing.json", None),
        ("dust.lorebook", {"title": "not a lorebook"}),
    ):
        path = write(tmp_path, name, data)
        with pytest.raises(LorebookError, match=f"^{name} isn't a SillyTavern lorebook.$"):
            read_lorebook(path)
        assert not isinstance(_raised(path), UnconvertibleLorebook)
    # Only NovelAI's own name for a file is taken as theirs whatever it holds.
    assert is_converted_lorebook(tmp_path / "dust.lorebook")
    assert not is_converted_lorebook(write(tmp_path, "card.json", v2()))
    assert not is_converted_lorebook(tmp_path / "missing.json")
    zipped = tmp_path / "elena.charx"
    zipped.write_bytes(b"PK\x03\x04")
    assert not is_converted_lorebook(zipped)


def _raised(path: Path) -> Exception:
    try:
        read_lorebook(path)
    except LorebookError as exc:
        return exc
    raise AssertionError(f"{path.name} was read")


# --- entries moved in their own order ------------------------------------------------------


def card_book(display_index: bool = True) -> dict:
    """A card's book as SillyTavern writes one (convertWorldInfoToCharacterBook:
    each entry's display index in its extensions), with something in its list
    that is no entry."""
    entries: list = [
        {
            "id": number,
            "keys": [name],
            "content": f"The {name}.",
            "extensions": {"display_index": number, "probability": 100}
            if display_index
            else {"probability": 100},
        }
        for number, name in enumerate(["farmhouse", "creek", "ridge"])
    ]
    entries.insert(2, "not an entry")
    return {"name": "Doomsville", "entries": entries}


def names(book) -> list[str]:
    return [raw["keys"][0] if "keys" in raw else raw["key"][0] for raw in lore.book_entries(book)]


def test_an_entry_moves_in_a_cards_list_and_its_display_index_follows():
    book = card_book()
    ridge = lore.book_entries(book)[2]
    assert lore.move_entry(book, ridge, -1)
    assert names(book) == ["farmhouse", "ridge", "creek"]
    assert book["entries"][2] == "not an entry", "what is no entry stays where it was"
    # Renumbered to its place in the list, which SillyTavern reads first.
    assert [e["extensions"]["display_index"] for e in lore.book_entries(book)] == [0, 1, 3]
    world = lore.world_from_book(book)
    assert [e["key"][0] for e in lore.entries_of(world)] == ["farmhouse", "ridge", "creek"]
    assert [e.title for e in lore.lore_from_book(book).entries] == names(book)
    # As far as it goes, and no further.
    farmhouse = lore.book_entries(book)[0]
    assert not lore.move_entry(book, farmhouse, -1) and names(book)[0] == "farmhouse"
    assert not lore.move_entry(book, lore.book_entries(book)[-1], 1)
    assert not lore.move_entry(book, {"keys": ["elsewhere"]}, 1)
    assert lore.move_entry(book, farmhouse, 1)
    assert names(book) == ["ridge", "farmhouse", "creek"]


def test_a_cards_entry_with_no_display_index_moves_in_the_list_alone():
    book = card_book(display_index=False)
    lore.move_entry(book, lore.book_entries(book)[0], 1)
    assert names(book) == ["creek", "farmhouse", "ridge"]
    assert all("display_index" not in e["extensions"] for e in lore.book_entries(book))
    assert [e["displayIndex"] for e in lore.entries_of(lore.world_from_book(book))] == [0, 1, 3]
    # A null one goes by the list too, and is left so.
    book["entries"][0]["extensions"]["display_index"] = None
    lore.move_entry(book, lore.book_entries(book)[0], 1)
    assert book["entries"][1]["extensions"]["display_index"] is None


def test_an_entry_moves_in_sillytaverns_own_shape_by_its_display_index():
    book = {
        "entries": {
            "3": {"uid": 3, "key": ["farmhouse"], "content": "a", "displayIndex": 5},
            "1": {"uid": 1, "key": ["creek"], "content": "b", "displayIndex": 6},
            "9": {"uid": 9, "key": ["ridge"], "content": "c"},
            "x": "not an entry",
        }
    }
    assert names(book) == ["farmhouse", "creek", "ridge"]
    ridge = book["entries"]["9"]
    assert lore.move_entry(book, ridge, -1)
    assert names(book) == ["farmhouse", "ridge", "creek"]
    assert [book["entries"][key].get("displayIndex") for key in ("3", "9", "1")] == [0, 1, 2]
    assert list(book["entries"]) == ["3", "1", "9", "x"], "kept under their own numbers"
    assert lore.move_entry(book, ridge, -1) and names(book) == ["ridge", "farmhouse", "creek"]
    assert not lore.move_entry(book, ridge, -1)
    # What SillyTavern lists, and a story takes, in the new order; put on a
    # card as SillyTavern writes one, by number, each with its new place.
    assert [e.title for e in lore.lore_from_book(book).entries] == names(book)
    on_card = lore.book_from_world(book)["entries"]
    assert [(e["id"], e["extensions"]["display_index"]) for e in on_card] == [
        (1, 2),
        (3, 1),
        (9, 0),
    ]


def test_by_a_link_another_apps_lorebook_is_never_taken_for_a_card(tmp_path: Path):
    """An Agnai book with a description read as an old card, and a NovelAI
    picture as a card that isn't there."""
    import io

    from sealedlore.storage.tavern_cards import TavernCard, tavern_from_file

    agnai = json.dumps(AGNAI_BOOK).encode()
    found = tavern_from_file(io.BytesIO(agnai), "doomsville.json")
    assert not isinstance(found, TavernCard) and found["kind"] == "memory"
    picture = novel_picture(tmp_path, NOVEL_BOOK).read_bytes()
    found = tavern_from_file(io.BytesIO(picture), "doomsville.png")
    assert not isinstance(found, TavernCard) and "lorebookVersion" in found


def test_a_copied_entry_is_given_its_own_display_index():
    """It took the original's, and SillyTavern listed it out of place."""
    book = card_book()
    farmhouse = lore.book_entries(book)[0]
    copy = lore.copy_entry(book, farmhouse)
    indexes = [
        entry["extensions"]["display_index"] for entry in book["entries"] if isinstance(entry, dict)
    ]
    assert [e for e in book["entries"] if isinstance(e, dict)][1] is copy
    assert indexes == sorted(set(indexes)), "each its own place, in the list's order"
