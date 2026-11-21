"""Which rules a lorebook's entries fire by, chosen where a story is made from
one, and what its import says in that story's words (the author's screenshot,
Nov 21 2026: a 320-entry world file's notes said "left out" of settings the
story kept, ran "left out Each entry keeps these settings", noted recursion
settings SealedLore's rules already keep, and sent a 0% entry every turn)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sealedlore.gui.lore_panel import LorePanel
from sealedlore.gui.lorebook_story import LorebookStoryDialog
from sealedlore.storage import lorebooks
from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle
from sealedlore.storage.tavern_lore import (
    KEPT_NOTE,
    ReadLorebook,
    lore_from_book,
    uses_tavern_settings,
)


def entry(uid: int, **fields) -> dict:
    return {"uid": uid, "key": [f"key{uid}"], "content": f"Entry {uid}.", **fields}


BOOK = {
    "entries": {
        "0": entry(0, comment="Signature", constant=True, probability=0, useProbability=True),
        "1": entry(1, comment="Wolves", excludeRecursion=True, preventRecursion=True),
        "2": entry(2, comment="Echo", delayUntilRecursion=True),
        "3": entry(3, comment="Seven Powers", keysecondary=["Power"], selective=True),
        "4": entry(4, comment="Setting", constant=True, position=4, depth=0),
        "5": entry(5, comment="Empty", content=""),
    }
}


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_a_zero_chance_entry_comes_in_switched_off():
    imported = lore_from_book(BOOK)
    signature = next(e for e in imported.entries if e.title == "Signature")
    assert signature.always_on and not signature.enabled
    assert imported.left_out["never"] == 1 and imported.left_out["probability"] == 0


def test_only_waiting_for_recursion_is_said_of_recursion():
    """Kept out of recursion is what SealedLore's rules do anyway: no entry
    fires from another's text there."""
    imported = lore_from_book(BOOK)
    assert imported.left_out["recursion"] == 1
    wolves = next(e for e in imported.entries if e.title == "Wolves")
    assert not uses_tavern_settings(wolves)
    echo = next(e for e in imported.entries if e.title == "Echo")
    assert uses_tavern_settings(echo)


def test_the_notes_are_sentences_in_the_words_of_the_rules():
    imported = lore_from_book(BOOK)
    own = imported.said("Lorebook")
    assert own == [
        "Lorebook: switched off entries with a 0% chance, which SillyTavern never sends: "
        "tick Enabled on the Lore page to send one (1 entry).",
        "Lorebook: left out where in the prompt it went: SealedLore places lore itself, "
        "always-on entries with the world (1 entry).",
        "Lorebook: left out entries with no text (1 entry).",
        "Lorebook: SealedLore's own rules don't use second keywords and their logic: the "
        "first keywords alone decide (1 entry).",
        "Lorebook: SealedLore's own rules don't use firing only from other entries' text: "
        "the story's own words fire it (1 entry).",
        KEPT_NOTE,
    ]
    assert not any("left out Each" in note for note in own)
    theirs = imported.said("Lorebook", "sillytavern")
    assert len(theirs) == 3 and KEPT_NOTE not in theirs
    assert "at a depth in the chat" in theirs[1]


def test_a_story_from_a_lorebook_fires_as_sillytavern_unless_chosen(tmp_path: Path):
    book = lorebooks.kept_from(ReadLorebook(BOOK), "Hellsville", "Hellsville.json")
    scenario, notes = lorebooks.scenario_from(book, title="Hellsville")
    assert scenario.lore_firing == "sillytavern"
    assert KEPT_NOTE not in notes
    own, own_notes = lorebooks.scenario_from(book, title="Hellsville", lore_firing="sealedlore")
    assert own.lore_firing == "sealedlore" and own_notes[-1] == KEPT_NOTE
    # A scenario file, Restart and Duplicate settings keep the rules.
    bundle = bundle_from_scenario(scenario)
    assert bundle.story.defaults.lore_firing == "sillytavern"
    bundle.story.defaults.tavern_world_info.scan_depth = 5
    again = bundle_from_scenario(scenario_from_bundle(bundle))
    assert again.story.defaults.lore_firing == "sillytavern"
    assert again.story.defaults.tavern_world_info.scan_depth == 5


def test_the_dialog_offers_the_rules_with_what_the_book_uses(app):
    book = lorebooks.kept_from(ReadLorebook(BOOK), "Hellsville", "Hellsville.json")
    dialog = LorebookStoryDialog(book, kept=0, can_ask=False)
    assert dialog.chosen().lore_firing == "sillytavern"
    dialog.lore_rules.set_value("sealedlore")
    assert dialog.chosen().lore_firing == "sealedlore"
    dialog.deleteLater()


def test_the_lore_page_says_what_the_rules_do_and_leave_unused(app):
    panel = LorePanel()
    panel.set_entries(lore_from_book(BOOK).entries)
    panel.set_firing("sealedlore")
    assert panel.firing_about.text() == "2 entries have SillyTavern settings these rules don't use."
    assert panel.firing_about.toolTip().startswith("A small lorebook is sent whole")
    panel.set_firing("sillytavern")
    assert panel.firing_about.text() == "Each entry fires by its own SillyTavern settings."
    panel.set_entries([])
    panel.set_firing("sealedlore")
    assert panel.firing_about.isHidden()
    panel.deleteLater()
