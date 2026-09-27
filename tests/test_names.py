"""Names and quotes: how the reads find people and sentences in prose."""

from __future__ import annotations

from sealedlore.engine.validators import locate_quote, mentions, name_forms, opens_elsewhere
from sealedlore.models.character import Character

SERRIK = Character(id="s", name="Serrik Vaun", aliases=["the Grey Wolf"])
IDRIS = Character(id="i", name="Captain Idris")
MARA = Character(id="m", name="Mara")


def test_a_name_is_found_by_its_parts_but_never_by_a_title_alone():
    forms = name_forms(SERRIK)
    assert {"Serrik Vaun", "Serrik", "Vaun", "the Grey Wolf"} <= set(forms)
    assert "Captain" not in name_forms(IDRIS)


def test_a_possessive_is_a_mention():
    assert mentions("Serrik's shadow fell across the table.", SERRIK)
    assert not mentions("The captain looked away.", SERRIK)


def test_a_quote_is_found_through_the_models_retyping():
    text = "She turned. “You came back,” she said, and didn't smile."
    assert locate_quote(text, '"You came back," she said, and didn\'t smile.') is not None
    assert locate_quote(text, "Nothing like this was ever written here at all.") is None


def test_a_cutaway_is_told_from_an_arrival():
    far = "Mara waited.\n\nFive miles north, Idris stood at the gate."
    assert opens_elsewhere(far, far.index("Idris"), [MARA])
    arrival = "Mara waited.\n\nIn the doorway, Idris stood watching Mara."
    assert not opens_elsewhere(arrival, arrival.index("Idris"), [MARA])
    broken = "Mara waited.\n\n---\n\nIdris read the dispatch."
    assert opens_elsewhere(broken, broken.index("Idris"), [MARA])
