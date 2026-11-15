"""A character's own lorebook in a story drafted around them: the drafter
reads it (engine/authoring.py `render_own_characters`), and the story's lore
takes it as it is, with the entries the draft found don't hold disabled
(engine/drafting.py). Every lorebook that comes in says where it came from
(`LoreEntry.came_from`); the window's side is tests/test_lore_origin_gui.py."""

from __future__ import annotations

import json

from sealedlore.engine import authoring
from sealedlore.engine.authoring import lorebook_lines, own_lorebook
from sealedlore.engine.drafting import put_in_characters
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.scenario import Scenario
from sealedlore.models.tavern import TavernFields
from sealedlore.storage.tavern_cards import read_card, scenario_from_card
from sealedlore.storage.tavern_lore import open_lorebook
from tests.test_tavern import png_card, v2


def entry(title: str, content: str, **more) -> dict:
    return {"keys": [title.lower()], "content": content, "comment": title, **more}


def card(name: str, entries: list[dict]) -> Character:
    return Character(
        name=name,
        summary=f"{name}, of Hellsville.",
        tavern=TavernFields(character_book={"entries": entries}),
    )


# --- what the drafter reads ------------------------------------------------------------


def test_the_drafter_reads_what_the_story_will_have_and_nothing_switched_off():
    marta = card(
        "Marta",
        [
            entry("The Brass Ledger", "{{char}} keeps it; {{user}} is not in it."),
            entry("The Cistern", "Water for blue tokens.", constant=True),
            entry("The mayor", "Dead.", enabled=False),
            entry("The bell", "@@dont_activate\nNever sent."),
            entry("Untitled", "   "),
        ],
    )
    text = authoring.render_own_characters([marta])
    assert text.endswith(
        "Their lorebook:\n"
        "- The Brass Ledger (keywords: the brass ledger): Marta keeps it; the author's "
        "character is not in it.\n"
        "- The Cistern (always on): Water for blue tokens."
    )
    assert "Dead." not in text and "Never sent." not in text
    assert [e.title for e in own_lorebook(marta, "John")] == ["The Brass Ledger", "The Cistern"]


def test_a_big_lorebook_is_read_whole_however_long():
    # The author, Nov 15 2026: a story made from a card takes all its lore,
    # and the drafter reads all of it (it used to stop at 3,000 words).
    entries = [
        LoreEntry(title=f"E{n}", content=f"fact{n} " * 400, keywords=[f"e{n}"])
        for n in range(1, 31)
    ]
    lines = lorebook_lines(entries)
    assert len(lines) == 31
    assert all(f"fact{n} " in lines[n] for n in range(1, 31))
    assert not any("by title only" in line for line in lines)


# --- where the draft puts it ------------------------------------------------------------


def drafted(*cards: Character, lore: list[LoreEntry] = (), cast: tuple[str, ...] = ()):
    scenario = Scenario(
        title="Drafted",
        cast=[Character(name="John"), *(Character(name=name) for name in cast)],
        lore=list(lore),
    )
    scenario.suggested_character_id = scenario.cast[0].id
    warnings: list[str] = []
    brought: list[str] = []
    put_in_characters(scenario, {}, list(cards), list(cards), lambda _f: None, warnings, brought)
    return scenario, warnings, brought


def test_the_first_characters_entry_keeps_a_title_two_lorebooks_share():
    marta = card("Marta", [entry("Hellsville", "Walled.")])
    elena = card("Elena", [entry("Hellsville", "Burned."), entry("The farm", "North.")])
    scenario, warnings, brought = drafted(marta, elena, cast=("Marta",))
    assert [(e.title, e.content, e.came_from) for e in scenario.lore] == [
        ("Hellsville", "Walled.", "Marta's lorebook"),
        ("The farm", "North.", "Elena's lorebook"),
    ]
    assert warnings[-1] == (
        "Elena's lore entry “Hellsville” wasn't added: another character's lorebook has one "
        "by that name"
    )
    # Elena wasn't in the draft's cast: she is put in it, and said to be.
    assert brought == [
        "Marta: their card as it is, in the cast; 1 entry of their lorebook in the story's "
        "lore (the Lore page).",
        "Elena: their card as it is, in the cast; 1 entry of their lorebook in the story's "
        "lore (the Lore page).",
    ]


def test_one_lorebook_with_a_title_twice_comes_whole():
    """A story made from a card takes its whole lorebook: two entries of one
    title in one book are both kept (live, a 44-entry book lost 24 to the
    check meant for another character's book)."""
    marta = card(
        "Marta",
        [entry("Mill Street", "The smithy."), entry("Mill Street", "The bakery.")],
    )
    scenario, warnings, brought = drafted(marta, cast=("Marta",))
    assert [e.content for e in scenario.lore] == ["The smithy.", "The bakery."]
    assert not warnings
    assert "2 entries of their lorebook" in brought[0]


def test_a_character_with_no_lorebook_is_said_to_bring_only_their_card():
    plain = Character(name="Ben", summary="A trader.")
    _scenario, _warnings, brought = drafted(plain, cast=("Ben",))
    assert brought == ["Ben: their card as it is, in the cast."]


# --- where every other way in says it came from ------------------------------------------


def test_every_way_a_lorebook_comes_in_marks_where_it_came_from(tmp_path):
    path = png_card(tmp_path, {"chara": v2()})
    made = scenario_from_card(read_card(path), player="John")
    assert [e.came_from for e in made.scenario.lore] == ["Elena's lorebook"]
    book = tmp_path / "book.json"
    book.write_text(json.dumps({"entries": {"0": entry("The farm", "North.", uid=0)}}))
    lore = open_lorebook(book).lore(came_from="the lorebook book.json")
    assert [e.came_from for e in lore.entries] == ["the lorebook book.json"]


def test_an_entry_the_draft_copied_under_another_title_gives_way_to_theirs():
    """Live, Sonnet 4.6 copied whole lorebooks into its own lore, once as
    "The Quiet Deck (lorebook entry)": the title alone couldn't catch it."""
    marta = card(
        "Marta",
        [entry("The Brass Ledger", "{{char}} keeps the Brass Ledger. It holds 412 names.")],
    )
    copied = LoreEntry(
        title="The Brass Ledger (lorebook entry)",
        content="Marta keeps the Brass Ledger.  It holds 412 names.",
    )
    builds_on = LoreEntry(
        title="The Gate Procedure",
        content="When the bell rings once, Marta comes to the gate with the Brass Ledger and "
        "writes the stranger's name and what they owe in it.",
    )
    scenario, warnings, _brought = drafted(marta, lore=[copied, builds_on], cast=("Marta",))
    assert [e.title for e in scenario.lore] == ["The Gate Procedure", "The Brass Ledger"]
    assert warnings == [
        "the draft's own lore entry “The Brass Ledger (lorebook entry)” was left out: "
        "Marta's lorebook says the same in “The Brass Ledger”"
    ]


def test_entries_the_draft_finds_dont_hold_in_its_story_come_switched_off():
    """A premise that puts them elsewhere: what their lorebook says of their
    own world is kept, sent nowhere, and said, for the author to switch on."""
    marta = card(
        "Marta",
        [entry("Hellsville", "A walled town."), entry("Marta's hand", "Two fingers lost.")],
    )
    scenario = Scenario(title="A liner", cast=[Character(name="John"), Character(name="Marta")])
    scenario.suggested_character_id = scenario.cast[0].id
    data = {"cast": [{"name": "Marta", "lorebook_set_aside": ["hellsville", 7, "Nowhere"]}]}
    warnings: list[str] = []
    brought: list[str] = []
    put_in_characters(scenario, data, [marta], [marta], lambda _f: None, warnings, brought)
    assert [(e.title, e.enabled) for e in scenario.lore] == [
        ("Hellsville", False),
        ("Marta's hand", True),
    ]
    assert brought == [
        "Marta: their card as it is, in the cast; 2 entries of their lorebook in the story's "
        "lore (the Lore page); 1 of them disabled there, as the draft found they don't hold "
        "in this story: tick Enabled on any you want."
    ]


def test_the_drafter_is_told_it_may_set_entries_aside_and_whom_the_author_plays():
    marta = card("Marta", [entry("Hellsville", "A walled town.")])
    request = authoring.build_generation_messages("A liner in 1926.", characters=[marta])
    text = request[1].parts[0].text
    assert '"lorebook_set_aside"' in text and '"play_as"\nis whoever the premise says' in text
