"""Review settings with the model and Change the story with the model know
what came after them (the author, Nov 21 2026: "make sure Review Settings With
The Model and Review Story With The Model is aware of and can recommend
changes to all of these new things"): how the lore fires and SillyTavern's
settings, each entry's own, where an entry came from, characters played by
their own model and when they answer, every job's own model, the public
record, what is still open, the story's waiting suggestions, and the app's
newer settings."""

from __future__ import annotations

import json

import pytest

from sealedlore.engine.authoring import (
    apply_change,
    current_value,
    parse_review,
    review_system,
    settings_view,
)
from sealedlore.engine.catalog import ModelInfo
from sealedlore.engine.followup import followup_system, setup_view
from sealedlore.engine.tavern_firing import entry_settings, fire_entry
from sealedlore.models.authoring import SettingsChange
from sealedlore.models.config import Config
from sealedlore.models.lore import LoreEntry, TavernEntry
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.story_suggestion import StorySuggestion
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.tavern_lore import lore_from_book

WORLD = {
    "entries": {
        "0": {
            "uid": 0,
            "key": ["Seven"],
            "keysecondary": ["Power"],
            "selective": True,
            "selectiveLogic": 0,
            "comment": "Seven Great Powers",
            "content": "Seven powers rule the world.",
            "probability": 100,
            "useProbability": True,
        }
    }
}
CARD_BOOK = {
    "entries": [
        {
            "keys": ["Hellsville"],
            "content": "A walled town.",
            "extensions": {"probability": 100, "useProbability": True},
        }
    ]
}
MODELS = {"glm-5.3": ModelInfo(id="glm-5.3"), "kimi-k3": ModelInfo(id="kimi-k3")}


@pytest.fixture
def bundle(story, cast) -> StoryBundle:
    lore = lore_from_book(WORLD, came_from="the lorebook Powers").entries
    lore += lore_from_book(CARD_BOOK).entries
    lore.append(LoreEntry(title="The Keep", content="Old stones.", keywords=["keep"]))
    return StoryBundle(story=story, cast=cast, lore=lore)


def change(kind: str, **fields) -> SettingsChange:
    return SettingsChange(kind=kind, **fields)


def reply(*changes: dict) -> str:
    return json.dumps({"summary": "s", "changes": list(changes), "cannot_fix": []})


# --- what the review is shown ----------------------------------------------------------


def test_the_review_sees_how_the_lore_fires_and_where_it_came_from(bundle: StoryBundle):
    view = settings_view(bundle)
    assert view["lore_firing"] == "sealedlore"
    assert "sillytavern_settings" not in view and "round_order" not in view
    powers = view["lore"][0]
    assert powers["came_from"] == "the lorebook Powers" and "sillytavern" not in powers
    assert powers["sillytavern_unused"]["secondary_keywords"] == ["Power"]
    bundle.story.defaults.lore_firing = "sillytavern"
    view = settings_view(bundle)
    assert view["sillytavern_settings"]["scan_depth"] == 2
    assert view["lore"][0]["sillytavern"] == {
        "secondary_keywords": ["Power"],
        "secondary_logic": "and any",
    }
    # SillyTavern's defaults are left unsaid; an entry written here has none.
    assert "sillytavern" not in view["lore"][1] and "sillytavern" not in view["lore"][2]


def test_the_review_sees_who_plays_themselves_and_the_record(bundle: StoryBundle):
    serrik = bundle.cast[0]
    assert "own_model" not in settings_view(bundle)["cast"][0]
    serrik.own_model, serrik.own_notes = "kimi-k3", "Short answers."
    passage = Node(
        kind="assistant",
        speaker_id="narrator",
        content="x",
        meta=NodeMeta(public_record=["The bridge fell."]),
    )
    bundle.nodes = [passage]
    bundle.story.active_leaf_id = passage.id
    bundle.story.scene.still_open = ["Who cut the rope?"]
    bundle.story.story_suggestions = [StorySuggestion(kind="lore_new", title="The bridge")]
    view = settings_view(bundle, config=Config())
    assert view["cast"][0]["own_model"] == "kimi-k3"
    assert view["cast"][0]["own_notes"] == "Short answers."
    assert view["round_order"] == "after"  # the default since Nov 22 2026
    playthrough = view["playthrough"]
    assert playthrough["public_record"] == ["The bridge fell."]
    assert playthrough["scene"]["still_open"] == ["Who cut the rope?"]
    assert playthrough["suggestions_waiting"] == ["lore new: The bridge"]
    app = view["app_settings"]
    assert app["prose_reminders"] is True and app["storyteller_moves"] is True
    assert "lore_embeddings" in app and "record_model" in app


def test_the_review_is_told_what_it_may_change_of_them():
    system = review_system()
    for said in (
        '"lore_firing"',
        '"sillytavern_settings"',
        '"round_order"',
        '"own_model"',
        '"own_notes"',
        '"secondary_keywords"',
        '"scene_model"',
        '"record_model"',
        '"suggestions_waiting"',
        '"public_record"',
        '"still_open"',
        '"prose_reminders"',
        '"lore_embeddings"',
    ):
        assert said in system, said
    follow = followup_system()
    assert '"lore_firing"' in follow and '"secondary_keywords"' in follow
    assert '"own_model"' not in follow, "who plays a character is chosen on their card"


def test_a_follow_up_sees_the_lore_rules(bundle: StoryBundle):
    bundle.story.defaults.lore_firing = "sillytavern"
    view = setup_view(bundle)
    assert view["lore_firing"] == "sillytavern" and "sillytavern_settings" in view
    assert view["lore"][0]["sillytavern"]["secondary_keywords"] == ["Power"]


# --- what it may change ----------------------------------------------------------------


def test_the_story_s_lore_rules_and_settings_change(bundle: StoryBundle):
    apply_change(bundle, change("story", field="lore_firing", value="sillytavern"))
    apply_change(bundle, change("story", field="round_order", value="after"))
    apply_change(
        bundle,
        change("story", field="sillytavern_settings", value={"scan_depth": 4, "recursive": False}),
    )
    defaults = bundle.story.defaults
    assert defaults.lore_firing == "sillytavern" and defaults.round_order == "after"
    assert defaults.tavern_world_info.scan_depth == 4
    assert defaults.tavern_world_info.recursive is False
    assert defaults.tavern_world_info.budget_percent == 25, "the rest stay"
    for value, problem in (
        ({"scan_depth": -1}, "scan_depth"),
        ({"depth": 3}, "isn't one of SillyTavern's settings"),
        (3, "an object"),
    ):
        with pytest.raises(ValueError, match=problem):
            apply_change(bundle, change("story", field="sillytavern_settings", value=value))
    with pytest.raises(ValueError, match="isn't one of"):
        apply_change(bundle, change("story", field="lore_firing", value="novelai"))


def test_an_entry_s_own_settings_change_where_sillytavern_reads_them(bundle: StoryBundle):
    powers, town, keep = bundle.lore
    edit = change(
        "lore_edit", target="Seven Great Powers", field="sillytavern", value={"probability": 40}
    )
    with pytest.raises(ValueError, match="SealedLore's rules"):
        apply_change(bundle, edit)
    bundle.story.defaults.lore_firing = "sillytavern"
    apply_change(bundle, edit)
    assert powers.tavern.fields["probability"] == 40
    apply_change(
        bundle,
        change(
            "lore_edit",
            target=town.title,
            field="sillytavern",
            value={"sticky": 2, "secondary_keywords": "wall, gate", "order": 5},
        ),
    )
    # A card's book keeps these in the entry's extensions, its order on top.
    assert town.tavern.fields["extensions"]["sticky"] == 2
    assert town.tavern.fields["insertion_order"] == 5
    assert town.tavern.fields["secondary_keys"] == ["wall", "gate"]
    read = entry_settings(town.tavern.fields, town.tavern.numbered)
    assert (read["sticky"], read["order"], read["secondary_keywords"]) == (2, 5, ["wall", "gate"])
    # An entry written in SealedLore gets SillyTavern's own around it.
    apply_change(
        bundle, change("lore_edit", target="The Keep", field="sillytavern", value={"group": "g"})
    )
    assert keep.tavern is not None and entry_settings(keep.tavern.fields, True)["group"] == "g"
    for value, problem in (
        ({"probability": 140}, "0 to 100"),
        ({"secondary_logic": "or"}, "logic"),
        ({"weight": 3}, "isn't one of an entry's"),
    ):
        with pytest.raises(ValueError, match=problem):
            apply_change(
                bundle, change("lore_edit", target="The Keep", field="sillytavern", value=value)
            )
    before = current_value(bundle, change("lore_edit", target="The Keep", field="sillytavern"))
    assert before["group"] == "g"


def test_who_plays_a_character_is_checked_against_the_endpoint(bundle: StoryBundle):
    serrik = bundle.cast[0]
    item = {"kind": "character", "target": "Serrik Vaun", "field": "own_model", "value": "x"}
    review = parse_review(reply(item), bundle, models=MODELS)
    assert not review.changes and "doesn't offer" in review.cannot_fix[0]
    review = parse_review(reply(item), bundle)
    assert not review.changes and "Played by" in review.cannot_fix[0]
    item["value"] = "kimi-k3"
    review = parse_review(reply(item), bundle, models=MODELS)
    assert len(review.changes) == 1
    apply_change(bundle, review.changes[0], models=MODELS)
    assert serrik.own_model == "kimi-k3"
    apply_change(bundle, change("character", target="Serrik Vaun", field="own_notes", value="Be"))
    assert serrik.own_notes == "Be"
    with pytest.raises(ValueError, match="isn't played by a model"):
        apply_change(bundle, change("character", target="Maela Orr", field="own_notes", value="Be"))


def test_every_job_s_own_model_may_change(bundle: StoryBundle):
    apply_change(bundle, change("model", field="scene_model", value="glm-5.3"), models=MODELS)
    assert bundle.story.defaults.scene_model == "glm-5.3"
    apply_change(bundle, change("model", field="scene_model", value=None), models=MODELS)
    assert bundle.story.defaults.scene_model is None
    with pytest.raises(ValueError, match="needs a model"):
        apply_change(bundle, change("model", field="main_model", value=None))


# --- the firing reads what was changed -------------------------------------------------


def test_an_imported_entry_fires_by_its_keywords_and_always_on_as_changed():
    """Under SillyTavern's rules the keys an entry brought were read and the
    Lore page's (or a review's) were not; nor did unticking Always on stop a
    constant entry."""
    [entry] = lore_from_book(
        {"entries": {"0": {"uid": 0, "key": ["wolf"], "content": "x", "constant": True}}}
    ).entries
    entry.keywords = ["hound"]
    entry.always_on = False
    fired = fire_entry(
        id=entry.id,
        title=entry.title,
        content=entry.content,
        keywords=entry.keywords,
        always_on=entry.always_on,
        enabled=True,
        raw=entry.tavern.fields,
        numbered=entry.tavern.numbered,
    )
    assert fired.keys == ("hound",) and not fired.constant
    patterned = TavernEntry(numbered=True, fields={"key": ["/wol(f|ves)/i", "wolf"]})
    kept = fire_entry(
        id="e",
        title="t",
        content="c",
        keywords=["wolf"],
        always_on=False,
        enabled=True,
        raw=patterned.fields,
        numbered=True,
    )
    assert kept.keys == ("/wol(f|ves)/i", "wolf"), "unchanged, the keys stay as they came"
