"""Supporting characters: finding them, and showing their cards only when relevant."""

from __future__ import annotations

import pytest

from sealedlore.engine.characters import (
    build_extraction_messages,
    card_from_suggestion,
    known_names,
    mentioned,
    parse_suggestions,
    render_supporting_block,
)
from sealedlore.models.character import Character

JANE = Character(id="c-jane", name="Jane Moss", aliases=["Jane"])
PRICE = Character(name="Sergeant Price", canon="Night of the Living Dead (1968)", origin="model")
PATEL = Character(
    name="Corporal Patel",
    summary="Guard whose radio Jane yanked away in the struggle.",
    origin="model",
)

REPLY = """```json
[
  {"name": "Sergeant Price", "aliases": ["Price"], "canon": "Night of the Living Dead (1968)",
   "description": "Commands the town guard; took the channel when Jane spoke."},
  {"name": "Corporal Patel", "aliases": [], "canon": null,
   "description": "A young guard. His radio was pulled from his chest."},
  {"name": "Jane", "aliases": [], "canon": null, "description": "Already on file."},
  {"aliases": ["nameless"]},
  "not an object"
]
```"""


def test_suggestions_are_parsed_and_known_names_dropped():
    found = parse_suggestions(REPLY, known=known_names([JANE]))

    assert [s.name for s in found] == ["Sergeant Price", "Corporal Patel"]
    price, patel = found
    assert price.canon == "Night of the Living Dead (1968)" and price.aliases == ["Price"]
    assert patel.canon is None
    assert "radio" in patel.description


def test_a_refused_name_is_not_offered_again():
    found = parse_suggestions(REPLY, known=known_names([JANE], ["corporal patel"]))
    assert [s.name for s in found] == ["Sergeant Price"]


def test_a_title_alone_does_not_make_two_people_the_same():
    """'Sergeant' is a rank, not a name: Sergeant Hale is not Sergeant Price."""
    reply = '[{"name": "Sergeant Hale", "aliases": [], "canon": null, "description": ""}]'
    found = parse_suggestions(reply, known=known_names([PRICE]))
    assert [s.name for s in found] == ["Sergeant Hale"]


def test_the_same_person_twice_in_one_reply_is_listed_once():
    reply = (
        '[{"name": "Hollis", "aliases": ["Constable Hollis"]},'
        ' {"name": "Constable Hollis", "aliases": []}]'
    )
    assert [s.name for s in parse_suggestions(reply, known=set())] == ["Hollis"]


@pytest.mark.parametrize("reply", ["Nobody new here.", "[not json", '{"name": "Hollis"}'])
def test_a_reply_without_a_list_is_an_error(reply):
    with pytest.raises(ValueError):
        parse_suggestions(reply, known=set())


def test_an_empty_list_is_nobody_new():
    assert parse_suggestions("[]", known=set()) == []


def test_the_extraction_prompt_names_what_is_on_file_and_refused():
    messages = build_extraction_messages(
        "Price took the channel.", on_file=["Jane Moss"], declined=["corporal patel"]
    )
    user = messages[-1].text
    assert "Already on file: Jane Moss." in user
    assert "corporal patel" in user
    assert "Price took the channel." in user
    assert "JSON array" in messages[0].text


def test_a_card_from_a_suggestion_is_marked_as_the_models():
    (suggestion,) = parse_suggestions(
        '[{"name": "Corporal Patel", "description": "A guard."}]', known=set()
    )
    card = card_from_suggestion(suggestion)
    assert card.origin == "model" and card.summary == "A guard."


def test_cards_are_chosen_by_mention_including_a_bare_surname():
    chosen = mentioned([PRICE, PATEL], "Patel lowered his rifle.", limit=8)
    assert chosen == [PATEL]
    assert mentioned([PRICE, PATEL], "The corridor was empty.", limit=8) == []
    # A rank on its own names nobody.
    assert mentioned([PRICE, PATEL], "The sergeant frowned.", limit=8) == []


def test_the_limit_holds():
    many = [Character(name=f"Private Number{i}") for i in range(12)]
    text = " ".join(c.name for c in many)
    assert len(mentioned(many, text, limit=8)) == 8


def test_the_block_names_the_canon_and_defers_to_the_roster():
    block = render_supporting_block([PRICE, PATEL])
    assert "Sergeant Price (Night of the Living Dead (1968))" in block
    # It used to say they were "not on the roster"; the roster lists them now.
    assert "not on the roster" not in block and "the roster says who is here" in block
    assert render_supporting_block([]) == ""
