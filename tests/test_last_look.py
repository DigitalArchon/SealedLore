"""The last look before beta: the bugs the second review found. Nothing here
touches the network; the GUI parts run offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sealedlore.engine.archival import chapter_numbers
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.config import Config, ModelPrice, ProviderConfig
from sealedlore.models.node import Usage
from sealedlore.models.summary import Summary
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from tests.conftest import make_exchange
from tests.test_background_archival import Background, Storyteller
from tests.test_session import FILLER, make_config

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- one chapter numbering ----------------------------------------------------


def test_chapter_numbers_count_a_part_for_all_its_chapters():
    first = Summary(content="one", covered_node_ids=["a"])
    part = Summary(
        content="two to four", covered_node_ids=["b", "c", "d"], merged_from=["x", "y", "z"]
    )
    fifth = Summary(content="five", covered_node_ids=["e"])
    numbers = chapter_numbers([first, part, fifth])
    assert numbers == {first.id: 1, part.id: 2, fifth.id: 5}
    assert chapter_numbers([]) == {}


# --- cost ------------------------------------------------------------------------


def test_the_story_total_prices_with_the_configured_cache_lifetime():
    entries = [
        {"id": "r1", "kind": "request", "payload": {"model": "anthropic/claude-sonnet-4.6"}},
        {
            "kind": "response",
            "request_log_ref": "r1",
            "usage": {
                "prompt_tokens": 2000,
                "cache_creation_input_tokens": 1000,
                "completion_tokens": 10,
            },
        },
    ]
    prices = {"anthropic/claude-sonnet-4.6": ModelPrice(prompt=3.0, completion=15.0)}
    five = usage_report(entries, prices, cache_ttl="5m").total.cost
    hour = usage_report(entries, prices, cache_ttl="1h").total.cost
    assert hour > five > 0


def test_the_cost_strip_says_unknown_rather_than_a_zero_lower_bound(app):
    from sealedlore.gui.status import StatusStrip

    strip = StatusStrip()
    strip.set_story_cost(0.0, unpriced=3, calls=3)
    assert "story unknown" in strip.cost_label.text()
    assert "≥" not in strip.cost_label.text()
    strip.set_story_cost(0.12, unpriced=1, calls=3)
    assert "story ≥ $0.1200" in strip.cost_label.text()
    # A reported zero (a subscription's included model) is a cost, not unknown.
    assert "session unknown" in strip.cost_label.text()
    strip.add_cost(Usage(cost=0.0, cost_reported=True))
    assert "session $0.0000" in strip.cost_label.text()


# --- background work is never lost from the record --------------------------------


def test_closing_a_story_logs_the_chapters_still_out(tmp_path: Path, story, cast):
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    nodes = make_exchange(24)
    for node in nodes:
        node.content = f"{node.content} {FILLER * 12}".strip()
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    story.active_leaf_id = nodes[-1].id
    story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        make_config(),
        Storyteller(["A passage."], Background()),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    turn = TurnRequest(
        speaker_id="char-serrik", user_text="I wait.", controlled_character_id="char-serrik"
    )
    session.story.defaults.context_token_budget = int(session.assemble(turn).full_total / 0.95)
    list(session.send(turn))
    job = session._archive_job
    assert job is not None and job.done.wait(5)
    before = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert "summary_response" not in before, "not adopted yet, so not logged yet"

    session.close(wait_seconds=0)  # as a story switch calls it
    assert session._archive_job is None
    log = read_api_log(session.story.id, root=tmp_path)
    dropped = [e for e in log if e["kind"] == "summary_response" and e.get("dropped")]
    assert dropped and "closed" in dropped[0]["dropped"]
    assert any(e["kind"] == "summary_request" and e.get("background") for e in log)


# --- settings ----------------------------------------------------------------------


def _config() -> Config:
    return Config(
        providers=[
            ProviderConfig(
                name="nano", base_url="https://nano-gpt.com/api/v1", api_key="k", model="m"
            )
        ],
        active_provider_name="nano",
        private_provider=ProviderConfig(
            name="private",
            base_url="http://localhost:11434/v1",
            model="local/tiny",
            timeout_seconds=42.0,
            extra_body={"keep": True},
        ),
    )


def test_renaming_the_provider_renames_it_rather_than_adding_a_second(app, story):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config()
    dialog = SettingsDialog(config, story)
    dialog.provider_name.setText("renamed")
    dialog._save()
    assert [p.name for p in config.providers] == ["renamed"]
    assert config.providers[0].api_key == "k"
    assert config.active_provider_name == "renamed"


def test_saving_settings_keeps_the_private_endpoints_hidden_fields(app, story):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config()
    SettingsDialog(config, story)._save()
    assert config.private_provider is not None
    assert config.private_provider.timeout_seconds == 42.0
    assert config.private_provider.extra_body == {"keep": True}


def test_app_wide_archival_settings_save_without_a_story_open(app):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config()
    dialog = SettingsDialog(config, None)
    assert not dialog.budget.isEnabled(), "the budget belongs to a story"
    dialog.auto_archive.setChecked(False)
    dialog.chunk_turns.setValue(7)
    dialog._save()
    assert config.auto_archive is False
    assert config.archive_chunk_turns == 7


# --- the stories list --------------------------------------------------------------


def test_the_stories_list_reads_only_the_story_file_and_sorts_by_last_played(
    app, tmp_path: Path, cast
):
    from sealedlore.gui.stories import StoryListPanel
    from sealedlore.models.story import Story

    old = Story(id="story-old", title="Older", updated_at="2026-09-01T00:00:00+00:00")
    new = Story(id="story-new", title="Newer", updated_at="2026-09-20T00:00:00+00:00")
    save_story_bundle(StoryBundle(story=old, cast=cast, nodes=make_exchange(1)), root=tmp_path)
    save_story_bundle(StoryBundle(story=new, cast=cast, nodes=make_exchange(3)), root=tmp_path)
    # A cast file that no longer validates must not hide the story from the list.
    (tmp_path / "stories" / "story-old" / "cast.json").write_text("[{corrupt", encoding="utf-8")
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    labels = [panel.list.item(i).text() for i in range(panel.list.count())]
    assert labels[0].startswith("Newer\n6 messages")
    assert labels[1].startswith("Older\n2 messages")
    assert "nodes" not in "".join(labels)


# --- names -------------------------------------------------------------------------


# --- the advisor (Story → Review settings…) -----------------------------------------


def _reviewable(tmp_path: Path):
    from tests.test_private import build, play, turn

    session, main, held = build(tmp_path)
    play(session, turn(held, "I look around the town."))
    return session, held


def test_the_advisor_cannot_make_the_played_character_unplayable(tmp_path: Path):
    from sealedlore.engine.authoring import apply_change
    from sealedlore.models.authoring import SettingsChange

    session, held = _reviewable(tmp_path)
    name = next(c.name for c in session.cast if c.id == held)
    with pytest.raises(ValueError, match="playing"):
        apply_change(
            session.bundle,
            SettingsChange(kind="character", target=name, field="playable", value=False),
        )


def test_supporting_cards_take_only_what_their_block_sends(tmp_path: Path):
    from sealedlore.engine.authoring import apply_change
    from sealedlore.models.authoring import SettingsChange
    from sealedlore.models.character import Character

    session, _held = _reviewable(tmp_path)
    session.bundle.supporting.characters.append(Character(name="Old Tom", summary="A drunk."))
    for field, value in (
        ("playable", True),
        ("skills", {"brawling": "solid"}),
        ("description", "x"),
    ):
        with pytest.raises(ValueError, match="supporting card"):
            apply_change(
                session.bundle,
                SettingsChange(kind="character", target="Old Tom", field=field, value=value),
            )
    apply_change(
        session.bundle,
        SettingsChange(kind="character", target="Old Tom", field="voice", value="Slurred."),
    )


def test_renames_never_collide_and_lists_split_on_commas(tmp_path: Path):
    from sealedlore.engine.authoring import apply_change
    from sealedlore.models.authoring import SettingsChange
    from sealedlore.models.lore import LoreEntry

    session, _held = _reviewable(tmp_path)
    first, second = session.cast[0].name, session.cast[1].name
    with pytest.raises(ValueError, match="already a character"):
        apply_change(
            session.bundle,
            SettingsChange(kind="character", target=first, field="name", value=second),
        )
    session.bundle.lore += [
        LoreEntry(title="Dock", content="x"),
        LoreEntry(title="Mine", content="y"),
    ]
    with pytest.raises(ValueError, match="already a lore entry"):
        apply_change(
            session.bundle,
            SettingsChange(kind="lore_edit", target="Mine", field="title", value="dock"),
        )
    apply_change(
        session.bundle,
        SettingsChange(kind="lore_edit", target="Dock", field="keywords", value="dock, harbour"),
    )
    assert session.bundle.lore[-2].keywords == ["dock", "harbour"]


def test_phrases_and_sentences_are_never_split_on_commas(tmp_path: Path):
    from sealedlore.engine.authoring import apply_change, parse_review
    from sealedlore.models.authoring import SettingsChange

    session, _held = _reviewable(tmp_path)
    # "Well, well" is one phrase to avoid, not the word "Well" twice.
    apply_change(
        session.bundle,
        SettingsChange(kind="style", field="forbidden_phrases", value="Well, well; a beat"),
    )
    assert session.bundle.story.style.forbidden_phrases == ["Well, well", "a beat"]
    review = parse_review(
        '{"summary": "s", "changes": [], '
        '"cannot_fix": "The dice table is fixed, so it cannot change here."}',
        session.bundle,
    )
    assert review.cannot_fix == ["The dice table is fixed, so it cannot change here."]


def test_the_reviewer_is_not_shown_plot_hidden_cards(tmp_path: Path):
    import json as _json

    from sealedlore.engine.authoring import settings_view

    session, _held = _reviewable(tmp_path)
    hidden = session.hidden_ids()
    assert hidden, "Doomsville hides characters until the plot brings them in"
    names = {c.name for c in session.cast if c.id in hidden}
    shown = _json.dumps(settings_view(session.bundle, hidden_ids=hidden, path=session.path()))
    assert not any(name in shown for name in names)
    assert all(name in _json.dumps(settings_view(session.bundle)) for name in names)


def test_undoing_a_review_puts_the_rosters_and_the_provider_model_back(tmp_path: Path):
    from sealedlore.models.authoring import SettingsChange

    session, held = _reviewable(tmp_path)
    # The scene lives on the nearest snapshot at or above the leaf.
    leaf = next(n for n in reversed(session.path()) if n.meta.scene is not None)
    other = next(c for c in session.cast if c.id != held and c.id not in session.hidden_ids())
    leaf.meta.scene.present_character_ids.append(other.id)
    session._sync_scene()
    before = leaf.meta.scene.model_copy(deep=True)
    present = [other.id]
    name = other.name
    provider = session.config.active_provider()
    original_model = provider.model
    session.apply_review(
        [
            SettingsChange(kind="character_move", target=name, value="supporting"),
            SettingsChange(kind="model", field="main_model", value="other/model"),
        ]
    )
    provider.model = "other/model"  # as the window does after applying a model change
    assert present[0] not in leaf.meta.scene.present_character_ids
    assert name in leaf.meta.scene.present_others
    assert session.undo_review()
    assert leaf.meta.scene.present_character_ids == before.present_character_ids
    assert name not in leaf.meta.scene.present_others
    assert provider.model == original_model


def test_a_realistic_review_reply_parses_whole(tmp_path: Path):
    from sealedlore.engine.authoring import parse_review

    session, _held = _reviewable(tmp_path)
    first = session.cast[0].name
    reply = f"""Here is my review.

```json
{{
  "summary": "The world text pushes for quiet; the complaint wants bustle.",
  "changes": [
    {{"kind": "story", "field": "world_activity", "value": "eventful", "reason": "livelier"}},
    {{"kind": "style", "field": "response_style", "value": "descriptive", "reason": "more"}},
    {{"kind": "character", "target": "{first}", "field": "voice",
     "value": "Dry, quick.", "reason": "voice"}},
    {{"kind": "lore_add", "reason": "colour",
     "value": {{"title": "The Bell", "content": "Rings at dusk.", "keywords": "bell, dusk"}}}},
    {{"kind": "generation", "field": "temperature", "value": 0.9, "reason": "looser"}},
    {{"kind": "model", "field": "main_model", "value": "some/model", "reason": "cheaper"}}
  ],
  "cannot_fix": ["The rule about the author's character."],
  "director_turn": "The town wakes: carts, voices, a bell.",
  "history_bound": true,
  "app_findings": [{{"where": "REMEMBER line", "problem": "too terse", "suggestion": "say why"}}]
}}
```
"""
    review = parse_review(reply, session.bundle)
    kinds = [c.kind for c in review.changes]
    assert kinds == ["story", "style", "character", "lore_add", "generation"]
    pinned = {(c.kind, c.field): c.before for c in review.changes}
    assert pinned[("story", "world_activity")] == "normal"
    assert pinned[("style", "response_style")] is not None
    assert review.director_turn and review.history_bound
    assert review.app_findings[0].problem == "too terse"
    assert any("some/model" in note for note in review.cannot_fix), "no model list: advice only"
