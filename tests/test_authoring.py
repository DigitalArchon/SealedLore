"""Drafting a story from a premise, and reviewing a story's settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.authoring import (
    apply_change,
    build_generation_messages,
    build_review_messages,
    current_value,
    describe,
    draft_system,
    parse_generated,
    parse_review,
    reply_nodes,
    review_system,
    settings_view,
)
from sealedlore.engine.catalog import ModelInfo, chat_models, matches, short_count
from sealedlore.engine.drafting import StoryDraft
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.rules import render_style_block
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.authoring import SettingsChange
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story import StyleDirectives
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)

DRAFT = {
    "title": "Winter at Hellsville",
    "description": "A lone survivor walks out of the snow.",
    "world": "The dead rose four years ago; a few towns hold out.",
    "cast": [
        {
            "name": "Jane Moss",
            "aliases": ["Corporal Moss", "Jane Moss"],
            "canon": None,
            "summary": "An ex-army medic, stranded.",
            "description": "Twenty-six, patient, out of her depth.",
            "voice": "Measured; asks questions.",
            "competence": "A capable shot; no driver.",
            "skills": {"First Aid": "expert", "rifle": "skilled", "driving": "godlike"},
            "playable": True,
        },
        {"name": "John Carver", "summary": "Her old partner, still at the Camp."},
        {"summary": "someone with no name"},
    ],
    "supporting": [
        {
            "name": "Barbra",
            "canon": "Night of the Living Dead (1968)",
            "summary": "Keeps the town's records.",
            "skills": {"combat": "expert"},
        }
    ],
    "lore": [
        {"title": "The Camp", "content": "Where John waits.", "keywords": ["camp"]},
        {"title": "Empty", "content": ""},
    ],
    "play_as": "Jane Moss",
    "opening": "Jane walks out of the tree line towards the gate.",
    "starting_scene": {
        "location": "The east gate",
        "time_of_day": "evening",
        "situation": "Alarms.",
        "present": ["John Carver", "Barbra"],
        "nearby": [{"name": "Jane Moss", "note": "in the tree line"}],
    },
    "style": {"response_style": "adaptive", "perspective": "sideways", "person": "third"},
    "agency_mode": "contested",
}


# --- JSON replies ----------------------------------------------------------------


def test_extract_json_takes_the_outermost_value_from_a_chatty_reply():
    text = 'Here you go:\n```json\n{"a": {"b": [1, 2]}}\n```\nEnjoy!'
    assert extract_json(text, dict, "thing") == {"a": {"b": [1, 2]}}
    with pytest.raises(ValueError, match="no list of things"):
        extract_json("nothing here", list, "list of things")


# --- drafting ----------------------------------------------------------------------


def test_a_draft_resolves_names_to_ids_and_reports_what_it_dropped():
    scenario, warnings = parse_generated("Sure!\n" + json.dumps(DRAFT))
    jane, john = scenario.cast
    assert jane.name == "Jane Moss" and jane.aliases == ["Corporal Moss"]
    assert jane.competence.tiers == {"first aid": "expert", "rifle": "skilled"}
    assert john.is_player_available  # cast defaults to playable
    assert scenario.suggested_character_id == jane.id
    # The author's character is moved into the scene they start in; Barbra is
    # supporting, not cast, so she can't be on the roster.
    assert scenario.starting_scene.present_character_ids == [jane.id, john.id]
    assert scenario.starting_scene.offstage_but_nearby == []
    barbra = scenario.supporting[0]
    assert barbra.canon == "Night of the Living Dead (1968)"
    assert barbra.competence.tiers == {} and not barbra.is_player_available
    assert [entry.title for entry in scenario.lore] == ["The Camp"]
    assert scenario.style.perspective == "whole_story"  # the bad value was dropped
    assert scenario.style.person == "third"
    assert scenario.world_bible.startswith("The dead rose")
    assert scenario.opening_mode == "generate"
    joined = " | ".join(warnings)
    assert "godlike" in joined and "sideways" in joined and "“Barbra” isn't in the cast" in joined
    assert "no name" in joined


def test_an_opening_that_names_someone_off_the_roster_is_flagged():
    draft = dict(DRAFT, opening="John Carver speaks first.", play_as="Jane Moss")
    draft["starting_scene"] = {"present": ["Jane Moss"]}
    _, warnings = parse_generated(json.dumps(draft))
    assert any("mentions John Carver" in warning for warning in warnings)
    draft["starting_scene"] = {"present": ["Jane Moss", "John Carver"]}
    _, warnings = parse_generated(json.dumps(draft))
    assert not any("mentions" in warning for warning in warnings)


def test_a_draft_without_a_cast_is_refused():
    with pytest.raises(ValueError, match="no cast"):
        parse_generated(json.dumps({"title": "Empty", "cast": []}))
    with pytest.raises(ValueError, match="story setup"):
        parse_generated("I'd rather not.")


def test_a_story_draft_is_saved_with_its_call_logged(tmp_path: Path):
    provider = MockChatProvider([json.dumps(DRAFT)])
    draft = StoryDraft(
        provider, "A winter at Hellsville", "anthropic/claude-opus-5", reasoning=ReasoningConfig()
    )
    events = list(draft.stream())
    assert events and draft.text == json.dumps(DRAFT)
    assert provider.last_request.params.max_tokens >= 8000

    bundle, warnings = draft.build(main_model="anthropic/claude-sonnet-4.6", root=tmp_path)
    assert warnings
    loaded = load_story_bundle(bundle.story.id, root=tmp_path)
    assert loaded.story.title == "Winter at Hellsville"
    assert loaded.story.defaults.main_model == "anthropic/claude-sonnet-4.6"
    assert loaded.story.scene == loaded.story.setup.starting_scene
    assert len(loaded.cast) == 2 and len(loaded.supporting.characters) == 1

    log = read_api_log(bundle.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log] == ["draft_request", "draft_response"]
    assert log[0]["payload"]["model"] == "anthropic/claude-opus-5"
    report = usage_report(log)
    assert [line.label for line in report.lines] == ["Story drafting"]
    assert report.total.cost == pytest.approx(0.004)


def test_an_empty_premise_is_refused():
    with pytest.raises(ValueError, match="premise"):
        StoryDraft(MockChatProvider(), "   ", "m", reasoning=ReasoningConfig())


# --- review: changes -------------------------------------------------------------------


@pytest.fixture
def bundle(story, cast) -> StoryBundle:
    return StoryBundle(
        story=story,
        cast=cast,
        lore=[LoreEntry(title="The Keep", content="Old stones.", embedding_hash="h")],
    )


def change(kind: str, **fields) -> SettingsChange:
    return SettingsChange(kind=kind, **fields)


def test_every_kind_of_change_applies(bundle: StoryBundle):
    for item in [
        change("style", field="dialogue_narration_balance", value="dialogue-led"),
        change("style", field="perspective", value="with_character"),
        change("style", field="forbidden_phrases", value=["suddenly", "a beat"]),
        change("style", field="notes", value="Show what others are thinking."),
        change("story", field="agency_mode", value="dice"),
        change("world", value="A new world."),
        change("opening", field="text", value="Begin at the gate."),
        change("character", target="the grey wolf", field="voice", value="Clipped."),
        change("character", target="Maela Orr", field="skills", value={"Locks": "expert"}),
        change("character", target="Maela Orr", field="playable", value=False),
        change(
            "character_add", target="supporting", value={"name": "Nils", "skills": {"x": "poor"}}
        ),
        change("lore_add", value={"title": "Bells", "content": "They ring.", "keywords": "bell"}),
        change("lore_edit", target="the keep", field="content", value="Older stones."),
        change("lore_disable", target="Bells"),
    ]:
        apply_change(bundle, item)
    style, story = bundle.story.style, bundle.story
    assert style.dialogue_narration_balance == "dialogue-led"
    assert style.perspective == "with_character"
    assert style.forbidden_phrases == ["suddenly", "a beat"]
    assert "Show what others are thinking." in render_style_block(style)
    assert story.defaults.agency_mode == "dice"
    assert story.world_bible == "A new world."
    assert story.setup.opening_text == "Begin at the gate."
    serrik, maela, _ = bundle.cast
    assert serrik.voice_notes == "Clipped."
    assert maela.competence.tiers == {"locks": "expert"} and not maela.is_player_available
    nils = bundle.supporting.characters[0]
    assert nils.name == "Nils" and nils.competence.tiers == {}
    keep, bells = bundle.lore
    assert keep.content == "Older stones." and keep.embedding_hash is None
    assert bells.keywords == ["bell"] and not bells.enabled


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        (change("style", field="perspective", value="sideways"), "isn't one of"),
        (change("style", field="perspective", value=None), "can't be cleared"),
        (change("style", field="colour", value="blue"), "isn't a style setting"),
        (change("style", field="custom_text", value="All new."), "isn't hand-written"),
        (change("story", field="agency_mode", value="chaos"), "isn't one of"),
        (change("character", target="Nobody", field="voice", value="x"), "no character"),
        (change("character", target="Maela Orr", field="skills", value={"a": "b"}), "tier"),
        (change("character", target="Maela Orr", field="height", value=2), "isn't a character"),
        (change("character_add", target="cast", value={"name": "Maela Orr"}), "already"),
        (change("character_add", target="villains", value={"name": "X"}), "cast or supporting"),
        (change("lore_add", value={"title": "The Keep", "content": "x"}), "already"),
        (change("lore_edit", target="Nowhere", field="content", value="x"), "no lore entry"),
    ],
)
def test_changes_that_cannot_apply_say_why(bundle: StoryBundle, item, problem):
    with pytest.raises(ValueError, match=problem):
        apply_change(bundle, item)


def test_a_hand_written_style_block_is_changed_whole(bundle: StoryBundle):
    bundle.story.style.detached = True
    bundle.story.style.custom_text = "Terse."
    apply_change(bundle, change("style", field="custom_text", value="Lush, and full of talk."))
    assert render_style_block(bundle.story.style).endswith("Lush, and full of talk.")


def test_a_review_keeps_what_applies_and_explains_what_doesnt(bundle: StoryBundle):
    reply = json.dumps(
        {
            "summary": "Too much action.",
            "changes": [
                {"kind": "style", "field": "pacing", "value": "unhurried", "reason": "slower"},
                {"kind": "character", "target": "Ghost", "field": "voice", "value": "Boo."},
                {"kind": "teleport", "value": 1},
            ],
            "cannot_fix": ["You can't make the model write your character's lines."],
        }
    )
    review = parse_review(reply, bundle)
    assert [c.field for c in review.changes] == ["pacing"]
    assert review.changes[0].reason == "slower"
    assert len(review.cannot_fix) == 3
    notes = " ".join(review.cannot_fix)
    assert "Ghost" in notes and "teleport" in notes
    # Parsing only tries changes out; nothing touches the story until applied.
    assert bundle.story.style.pacing is None


def test_current_values_are_shown_by_name(bundle: StoryBundle):
    voice = change("character", target="Serrik Vaun", field="voice", value="x")
    assert current_value(bundle, voice) is None
    assert current_value(bundle, change("world", value="x")).startswith("Calder Keep")
    assert current_value(bundle, change("lore_disable", target="The Keep")) is True


# --- review: what the reviewer sees ------------------------------------------------------


def test_the_reviewer_sees_settings_by_name_and_no_ids(bundle: StoryBundle):
    view = settings_view(bundle)
    assert [c["name"] for c in view["cast"]] == ["Serrik Vaun", "Maela Orr", "Captain Idris"]
    assert "char-serrik" not in json.dumps(view)
    assert "custom_text" not in view["style"]


def test_story_text_is_evidence_only_when_asked(bundle: StoryBundle):
    plain = build_review_messages(bundle, complaint="Too much action.", system_prompt="# RULES")
    user = plain[1].text
    assert "Too much action." in user and "# RULES" in user and "evidence" not in user
    assert "hasn't started" in user

    with_evidence = build_review_messages(
        bundle,
        complaint="x",
        system_prompt="y",
        evidence=["Swords clash."],
        messages_so_far=4,
    )[1].text
    assert "Swords clash." in with_evidence and "4 messages" in with_evidence


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="n",
    )
    return StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        MockChatProvider(),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_a_session_review_sends_the_system_block_and_the_last_three_replies(
    session: StorySession, tmp_path: Path
):
    for number in range(5):
        session.provider = MockChatProvider([f"Reply number {number}."])
        list(
            session.send(
                TurnRequest(
                    speaker_id="char-serrik",
                    user_text=f"Turn {number}",
                    controlled_character_id="char-serrik",
                )
            )
        )
    reply = {
        "summary": "Talk more.",
        "changes": [{"kind": "style", "field": "notes", "value": "More talk."}],
        "cannot_fix": [],
    }
    session.provider = MockChatProvider([json.dumps(reply)])
    session.config.authoring_model = "anthropic/claude-opus-5"
    review = session.review_settings("Too much action.")

    request = session.provider.last_request
    assert request.model == "anthropic/claude-opus-5"
    sent = request.messages[1].text
    assert session.system_prompt_text() in sent and "# SETTING" in sent
    assert "Reply number 4." in sent and "Reply number 2." in sent
    assert "Reply number 1." not in sent
    # Each reply goes with the author's turn it answered.
    assert "Turn 4" in sent and "Turn 1" not in sent
    assert session.unreported_usage

    assert session.apply_review(review.changes) == {review.changes[0].id: None}
    assert "More talk." in session.system_prompt_text()
    assert load_story_bundle(session.story.id, root=tmp_path).story.style.notes == "More talk."
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert kinds[-2:] == ["review_request", "review_response"]


def test_a_review_without_evidence_sends_no_story_text(session: StorySession):
    list(session.send(TurnRequest(speaker_id="char-serrik", user_text="Hello")))
    session.provider = MockChatProvider(['{"summary": "", "changes": [], "cannot_fix": []}'])
    session.review_settings("Too much action.", replies=0, model="x")
    sent = session.provider.last_request.messages[1].text
    assert "door gives" not in sent and "evidence" not in sent
    assert [node.content for node in reply_nodes(session.path())] == [session.path()[-1].content]


def test_changes_that_no_longer_apply_are_reported_not_raised(session: StorySession):
    ghost = change("character", target="Nobody", field="voice", value="x")
    problems = session.apply_review([ghost])
    assert problems[ghost.id] and "Nobody" in problems[ghost.id]


def test_a_blank_complaint_is_refused(session: StorySession):
    with pytest.raises(ValueError):
        session.review_settings("  ")


def test_style_notes_default_to_nothing():
    assert StyleDirectives().notes is None


# --- custom length: one change, never half of one ------------------------------------


def review_of(*changes) -> str:
    return json.dumps({"summary": "", "changes": list(changes), "cannot_fix": []})


WORDING = "Two to four paragraphs, at least half dialogue."


@pytest.mark.parametrize(
    "items",
    [
        # The shape that broke a real story: "custom" with its wording beside it.
        [{"kind": "style", "field": "response_style", "value": "custom", "length_target": WORDING}],
        [
            {
                "kind": "style",
                "field": "response_style",
                "value": {"response_style": "custom", "length_target": WORDING},
            }
        ],
        [
            {"kind": "style", "field": "response_style", "value": "custom"},
            {"kind": "style", "field": "length_target", "value": WORDING},
        ],
        [{"kind": "style", "field": "length_target", "value": WORDING}],
    ],
)
def test_a_custom_length_becomes_one_change_that_selects_custom(bundle: StoryBundle, items):
    bundle.story.style = StyleDirectives()
    review = parse_review(review_of(*items), bundle)
    assert [(c.field, c.value) for c in review.changes] == [("length_target", WORDING)]
    apply_change(bundle, review.changes[0])
    assert bundle.story.style.response_style == "custom"
    assert WORDING in render_style_block(bundle.story.style).splitlines()[2]


def test_custom_without_its_wording_is_refused_not_applied(bundle: StoryBundle):
    bundle.story.style = StyleDirectives()
    review = parse_review(
        review_of({"kind": "style", "field": "response_style", "value": "custom"}), bundle
    )
    assert review.changes == []
    assert "wording" in review.cannot_fix[0]
    assert bundle.story.style.response_style == "adaptive"


def test_clearing_the_wording_of_a_custom_length_is_refused(bundle: StoryBundle):
    with pytest.raises(ValueError, match="Custom"):
        apply_change(bundle, change("style", field="length_target", value=None))


def test_a_hand_written_style_block_refuses_fields_it_would_ignore(bundle: StoryBundle):
    bundle.story.style.detached = True
    bundle.story.style.custom_text = "Terse."
    with pytest.raises(ValueError, match="written by hand"):
        apply_change(bundle, change("style", field="notes", value="More talk."))
    # The length and perspective still reach the storyteller, so they still apply.
    apply_change(bundle, change("style", field="perspective", value="with_character"))
    apply_change(bundle, change("style", field="length_target", value=WORDING))
    assert bundle.story.style.length_target == WORDING


def test_a_drafted_custom_length_needs_its_wording():
    draft = dict(DRAFT, style={"response_style": "custom"})
    scenario, warnings = parse_generated(json.dumps(draft))
    assert scenario.style.response_style == "adaptive"
    assert any("custom length" in warning for warning in warnings)
    draft = dict(DRAFT, style={"length_target": WORDING})
    scenario, _ = parse_generated(json.dumps(draft))
    assert scenario.style.response_style == "custom"
    assert scenario.style.length_target == WORDING


# --- the model and generation settings -------------------------------------------------


SONNET = ModelInfo(
    id="anthropic/claude-sonnet-4.6",
    context_length=200_000,
    max_output_tokens=64_000,
    reasoning=True,
    reasoning_efforts=("low", "high"),
    prompt_price=3.0,
    completion_price=15.0,
)
MODELS = {SONNET.id: SONNET, "anthropic/claude-opus-5": ModelInfo(id="anthropic/claude-opus-5")}


def test_the_reviewer_sees_the_model_and_generation_settings(bundle: StoryBundle):
    cfg = Config(generation=GenerationParams(temperature=1.8), reasoning_story="low")
    view = settings_view(bundle, model=SONNET.id, facts=SONNET, config=cfg)["model"]
    assert view["main_model"] == SONNET.id
    assert view["generation"]["temperature"] == 1.8
    assert view["generation"]["reasoning"] == "low"
    assert view["endpoint_facts"]["max_output_tokens"] == 64_000
    assert view["endpoint_facts"]["usd_per_million_tokens"] == {"input": 3.0, "output": 15.0}
    assert settings_view(bundle)["model"]["endpoint_facts"] is None


def test_generation_changes_are_checked_and_never_applied(bundle: StoryBundle):
    """They are the author's settings for every story: the review only
    suggests them, and the author makes them by hand in Settings."""
    before = bundle.story.model_dump()
    for item in [
        change("generation", field="temperature", value=0.7),
        change("generation", field="top_p", value="0.9"),
        change("generation", field="frequency_penalty", value=0.3),
        change("generation", field="max_tokens", value=2000),
        change("generation", field="reasoning", value="LOW"),
        change("generation", field="reasoning", value="least"),
        change("generation", field="temperature", value=None),
    ]:
        apply_change(bundle, item, models=MODELS, current_model=SONNET.id)
    assert bundle.story.model_dump() == before


def test_a_generation_suggestions_before_is_the_authors_setting(bundle: StoryBundle):
    cfg = Config(generation=GenerationParams(temperature=0.4), reasoning_story="high")
    temperature = change("generation", field="temperature", value=0.9)
    reasoning = change("generation", field="reasoning", value="least")
    assert current_value(bundle, temperature, config=cfg) == 0.4
    assert current_value(bundle, reasoning, config=cfg) == "high"


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        (change("generation", field="temperature", value=3), "between 0 and 2"),
        (change("generation", field="temperature", value="hot"), "number"),
        (change("generation", field="max_tokens", value=100), "mid-sentence"),
        (change("generation", field="max_tokens", value=100_000), "at most 64,000"),
        (change("generation", field="reasoning", value="yes"), "reasoning must be one of"),
        (change("generation", field="reasoning", value=True), "reasoning must be one of"),
        (change("generation", field="seed", value=1), "isn't a generation setting"),
        (change("story", field="context_token_budget", value=500_000), "at most 200,000"),
        (change("story", field="context_token_budget", value=100), "can't hold a turn"),
        (change("model", field="main_model", value="gpt-9"), "doesn't offer"),
        (change("model", field="main_model", value=None), "needs a model"),
    ],
)
def test_model_and_generation_changes_are_checked(bundle: StoryBundle, item, problem):
    with pytest.raises(ValueError, match=problem):
        apply_change(bundle, item, models=MODELS, current_model=SONNET.id)


def test_a_model_suggestion_must_be_on_the_endpoint_list(bundle: StoryBundle):
    reply = review_of(
        {"kind": "model", "field": "main_model", "value": "anthropic/claude-opus-5"},
        {"kind": "model", "field": "summarization_model", "value": "made/up-model"},
    )
    review = parse_review(reply, bundle, models=MODELS, current_model=SONNET.id)
    assert [c.value for c in review.changes] == ["anthropic/claude-opus-5"]
    assert "made/up-model" in review.cannot_fix[0]

    # Without the list nothing is applied blind: the suggestion stays advice.
    blind = parse_review(reply, bundle, models=None)
    assert blind.changes == [] and len(blind.cannot_fix) == 2
    assert "couldn't be checked" in blind.cannot_fix[0]

    apply_change(bundle, review.changes[0])
    assert bundle.story.defaults.main_model == "anthropic/claude-opus-5"
    assert describe(review.changes[0]) == "Model › storyteller"


def test_the_session_review_sends_the_current_models_facts(session: StorySession):
    listing = {
        SONNET.id: {"name": "Sonnet", "context_length": 200_000, "max_output_tokens": 64_000},
        "anthropic/claude-opus-5": {"name": "Opus 5"},
        "image/maker": {"architecture": {"output_modalities": ["image"]}},
    }
    reply = review_of({"kind": "model", "field": "main_model", "value": "image/maker"})
    provider = MockChatProvider([reply])
    provider.fetch_model_prices = lambda: listing
    session.provider = provider
    session.story.defaults.main_model = SONNET.id
    review = session.review_settings("Too slow.", replies=0, model="x")
    sent = provider.last_request.messages[1].text
    assert '"max_output_tokens": 64000' in sent and "Opus 5" not in sent
    # Only chat models count: an image model isn't a storyteller.
    assert review.changes == [] and "image/maker" in review.cannot_fix[0]
    assert set(session.endpoint_models()) == {SONNET.id, "anthropic/claude-opus-5"}


# --- the catalog -------------------------------------------------------------------------


def test_the_catalog_reads_and_searches_the_model_list():
    listing = {
        "anthropic/claude-sonnet-4.6": {
            "name": "Claude Sonnet 4.6",
            "owned_by": "anthropic",
            "context_length": 1_000_000,
            "capabilities": {"reasoning": True},
            "pricing": {"prompt": 3, "completion": 15, "unit": "per_million_tokens"},
        },
        "x/decider": {"architecture": {"output_modalities": ["decisions"]}},
        "odd": "not an entry",
    }
    models = chat_models(listing)
    assert list(models) == ["anthropic/claude-sonnet-4.6"]
    sonnet = models["anthropic/claude-sonnet-4.6"]
    assert sonnet.reasoning and sonnet.prompt_price == 3 and sonnet.completion_price == 15
    assert matches(sonnet, "sonnet 4.6") and matches(sonnet, "ANTHROPIC")
    assert not matches(sonnet, "opus")
    assert [short_count(n) for n in (None, 900, 131_072, 200_000, 1_048_576)] == [
        "",
        "900",
        "131k",
        "200k",
        "1M",
    ]


# --- how much of the story the review sees ---------------------------------------

from sealedlore.engine.authoring import findings_markdown  # noqa: E402
from sealedlore.models.authoring import AppFinding, SettingsReview  # noqa: E402

EMPTY_REVIEW = '{"summary": "", "changes": [], "cannot_fix": []}'


def played(session: StorySession, turns: int) -> None:
    for number in range(turns):
        session.provider = MockChatProvider([f"Reply number {number}."])
        list(
            session.send(
                TurnRequest(
                    speaker_id="char-serrik",
                    user_text=f"Turn {number}",
                    controlled_character_id="char-serrik",
                )
            )
        )


def test_a_review_can_see_every_reply_in_the_story(session: StorySession):
    played(session, 6)
    session.provider = MockChatProvider([EMPTY_REVIEW])

    session.review_settings("Too much action.", replies=None, model="x")

    sent = session.provider.last_request.messages[1].text
    assert all(f"Reply number {n}." in sent for n in range(6))
    assert sent.index("Reply number 0.") < sent.index("Reply number 5.")  # oldest first


def test_the_whole_prompt_is_what_the_storyteller_would_receive(session: StorySession):
    played(session, 2)
    kept = session.last_prompt
    session.provider = MockChatProvider([EMPTY_REVIEW])

    session.review_settings("Too much action.", replies=5, whole_prompt=True, model="x")

    sent = session.provider.last_request.messages[1].text
    assert "=== SYSTEM MESSAGE ===" in sent and "=== USER MESSAGE ===" in sent
    assert "(The author's next turn goes here.)" in sent
    assert "REMEMBER:" in sent and "# THIS SCENE" in sent
    # The recent replies are in that prompt already, so they aren't sent twice.
    assert sent.count("Reply number 1.") == 1
    assert "exchange 1" not in sent
    # The inspector's prompt is left alone.
    assert session.last_prompt is kept


def test_older_replies_are_added_when_the_prompt_no_longer_carries_them(session: StorySession):
    played(session, 3)
    evidence, whole, in_prompt, tail = session._review_material(replies=None, whole_prompt=True)
    assert in_prompt == 3 and evidence == [] and whole and "REMEMBER:" in tail

    # Shrink the window so only the newest exchange is in the prompt.
    session.story.defaults.context_token_budget = 1
    evidence, _, in_prompt, _ = session._review_material(replies=None, whole_prompt=True)
    assert in_prompt < 3
    assert len(evidence) == 3 - in_prompt


def test_a_review_that_would_not_fit_the_model_is_refused_in_plain_words(
    session: StorySession,
):
    played(session, 2)
    provider = MockChatProvider([EMPTY_REVIEW])
    provider.fetch_model_prices = lambda: {"tiny/model": {"context_length": 9_000}}
    session.provider = provider

    with pytest.raises(ValueError, match="tiny/model takes 9,000"):
        session.review_settings("Too much action.", whole_prompt=True, model="tiny/model")
    assert provider.requests == []


def test_review_sizes_add_up_for_the_dialog(session: StorySession):
    played(session, 3)
    sizes = session.review_sizes()

    assert len(sizes.exchanges) == 3 and sizes.in_prompt == 3
    assert sizes.whole_prompt > sizes.system_prompt
    fixed = sizes.settings + sizes.system_prompt + sizes.tail + sizes.prompt_texts
    assert sizes.tail > 0 and sizes.prompt_texts > 0 and sizes.side_texts > 0
    assert sizes.total(0, False) == fixed
    assert sizes.total(2, False) == fixed + sum(sizes.exchanges[:2])
    assert sizes.total(0, False, side_texts=True) == fixed + sizes.side_texts
    # With the whole prompt, replies it carries cost nothing extra.
    assert sizes.total(3, True) == sizes.settings + sizes.whole_prompt + sizes.prompt_texts


# --- findings about SealedLore itself -------------------------------------------------


def test_the_reviewer_is_asked_for_findings_about_sealedlore():
    assert '"app_findings"' in review_system()
    assert "people who maintain" in " ".join(review_system().split())


def test_findings_are_read_in_either_shape(bundle: StoryBundle):
    reply = json.dumps(
        {
            "summary": "",
            "changes": [],
            "cannot_fix": [],
            "app_findings": [
                {
                    "where": "Never exceed about 250 words.",
                    "problem": "The fixed cap fights the author's wish for long scenes.",
                    "suggestion": "Take the cap from the story's length setting.",
                },
                "The REMEMBER line repeats the length twice.",
                {"where": "x"},  # no problem stated: not a finding
            ],
        }
    )
    review = parse_review(reply, bundle)

    assert [f.problem for f in review.app_findings] == [
        "The fixed cap fights the author's wish for long scenes.",
        "The REMEMBER line repeats the length twice.",
    ]
    assert review.app_findings[0].where == "Never exceed about 250 words."


def test_findings_copy_out_as_markdown():
    review = SettingsReview(
        app_findings=[AppFinding(where="Line one\nLine two", problem="A clash.", suggestion="Fix.")]
    )
    text = findings_markdown(review, complaint="Too short.", model="opus")

    assert "Author's complaint: Too short." in text
    assert "## 1. A clash." in text
    assert "> Line one\n> Line two" in text
    assert "Suggestion: Fix." in text


# --- what the reviewer is shown, and what it may propose (review of the review) ------

from sealedlore.engine.authoring import (  # noqa: E402
    ordered_for_apply,
    render_exchange,
    review_markdown,
)
from sealedlore.models.node import Node, NodeMeta, Roll  # noqa: E402
from sealedlore.models.scene import SceneShortcut, SceneState  # noqa: E402
from sealedlore.providers.base import StreamCompleted  # noqa: E402
from sealedlore.storage.archive import archive_data, read_archive, write_archive  # noqa: E402
from sealedlore.storage.scenario import (  # noqa: E402
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)


def test_the_default_review_sends_the_tail_and_names_the_held_character(
    session: StorySession,
):
    played(session, 1)
    session.story.held_character_id = "char-serrik"
    session.provider = MockChatProvider([EMPTY_REVIEW])

    session.review_settings("It refers to my character in the third person.", replies=0)

    sent = session.provider.last_request.messages[1].text
    assert "per-turn instructions" in sent
    assert "REMEMBER:" in sent and "# THIS SCENE" in sent
    assert "(The author's next turn goes here.)" not in sent
    assert '"author_plays": "Serrik Vaun"' in sent
    assert '"app_settings"' in sent and '"supporting_recall_turns"' in sent
    assert "=== SYSTEM MESSAGE ===" not in sent


def test_the_reviewer_is_not_told_the_old_presence_rule():
    prompt = " ".join(review_system().split())
    assert "never enter the author's scene" not in prompt
    assert "arrival rule" in prompt and "director_turn" in prompt
    assert '"selected"' in prompt and "per-turn choice" in prompt


def test_an_exchange_carries_what_shaped_the_turn_and_what_was_flagged(cast):
    turn = Node(
        id="u",
        kind="user",
        speaker_id="char-serrik",
        content="I pull the lever.",
        meta=NodeMeta(
            controlled_character_id="char-serrik",
            response_style="literary",
            agency_mode="dice",
            roll=Roll(die=100, value=97, band="critical_failure", target=45, domain="locks"),
        ),
    )
    reply = Node(
        id="a",
        parent_id="u",
        kind="assistant",
        speaker_id="__narrator__",
        content="The lever snaps.",
        meta=NodeMeta(
            scene_shortcuts=[
                SceneShortcut(
                    name="Captain Idris",
                    kind="already_there",
                    quote="Captain Idris was already by the lever.",
                )
            ]
        ),
    )
    text = render_exchange([turn, reply], reply, cast, position=2)

    assert "(message 2 of the story)" in text
    assert "Length for this turn only: Literary" in text
    assert "Outcome mode for this turn: dice" in text
    assert "Serrik Vaun at locks rolled 97 against 45 — critical failure" in text
    assert (
        "SealedLore flagged this reply: Captain Idris already there: "
        "“Captain Idris was already by the lever.”" in text
    )


def test_a_review_the_model_could_not_finish_is_refused_in_plain_words(
    session: StorySession,
):
    played(session, 1)
    provider = MockChatProvider(['{"summary": "cut off'])
    provider.complete = lambda request: (
        '{"summary": "cut',
        StreamCompleted(finish_reason="length"),
    )
    session.provider = provider

    with pytest.raises(ValueError, match="ran out of room"):
        session.review_settings("Too much action.")


def test_the_reviewer_gets_as_much_room_as_it_can_write_in(session: StorySession):
    provider = MockChatProvider([EMPTY_REVIEW])
    provider.fetch_model_prices = lambda: {
        "big/model": {"context_length": 400_000, "max_output_tokens": 64_000},
        "m": {"context_length": 200_000},
    }
    session.provider = provider

    session.review_settings("Too much action.", model="big/model")
    request = provider.last_request
    assert request.params.max_tokens == 64_000
    assert request.params.temperature == 0.3
    sent = request.messages[1].text
    assert '"alternatives"' in sent and '"id": "m"' in sent


def test_a_selected_npc_scope_is_refused_as_a_story_setting(bundle: StoryBundle):
    with pytest.raises(ValueError, match="per turn"):
        apply_change(bundle, change("story", field="npc_scope", value="selected"))
    apply_change(bundle, change("story", field="npc_scope", value="model_choice"))
    assert bundle.story.defaults.npc_scope == "model_choice"


def test_a_disabled_lore_entry_can_be_turned_back_on(bundle: StoryBundle):
    apply_change(bundle, change("lore_disable", target="The Keep"))
    assert not bundle.lore[0].enabled
    apply_change(bundle, change("lore_edit", target="The Keep", field="enabled", value=True))
    assert bundle.lore[0].enabled and bundle.lore[0].embedding_hash == "h"


def test_the_review_can_hold_an_entry_back_until_it_is_mentioned(bundle: StoryBundle):
    apply_change(
        bundle, change("lore_edit", target="The Keep", field="until_mentioned", value=True)
    )
    assert bundle.lore[0].until_mentioned
    apply_change(
        bundle,
        change("lore_add", value={"title": "Bells", "content": "Ring.", "until_mentioned": True}),
    )
    assert bundle.lore[-1].until_mentioned
    with pytest.raises(ValueError, match="true or false"):
        apply_change(
            bundle, change("lore_edit", target="The Keep", field="until_mentioned", value="yes")
        )


def test_a_draft_can_hold_back_what_has_not_happened_yet():
    draft = json.loads(json.dumps(DRAFT))
    draft["lore"].append(
        {"title": "The envoy", "content": "Arrives in spring.", "until_mentioned": True}
    )
    scenario, _ = parse_generated(json.dumps(draft))
    held = {entry.title: entry.until_mentioned for entry in scenario.lore}
    assert held["The envoy"] is True
    assert not any(v for title, v in held.items() if title != "The envoy")


def test_a_new_card_can_be_the_authors_alone_and_a_supporting_one_drops_skills(
    bundle: StoryBundle,
):
    author = change("character_add", target="cast", value={"name": "Wren", "author_only": True})
    apply_change(bundle, author)
    assert bundle.cast[-1].author_only and author.warning is None

    extra = change(
        "character_add",
        target="supporting",
        value={"name": "Tam", "skills": {"knives": "expert", "x": "meh"}, "author_only": True},
    )
    apply_change(bundle, extra)
    tam = bundle.supporting.characters[-1]
    assert tam.competence.tiers == {} and not tam.author_only and not tam.is_player_available
    assert "skills were dropped" in extra.warning and "isn't a tier" in extra.warning

    # A name that is only someone's alias is not a duplicate.
    apply_change(
        bundle, change("character_add", target="supporting", value={"name": "the Grey Wolf"})
    )


def test_a_character_moves_between_cast_and_supporting_keeping_their_place(
    bundle: StoryBundle,
):
    apply_change(bundle, change("character_add", target="supporting", value={"name": "Hollis"}))
    bundle.story.scene.present_others = ["Hollis"]
    apply_change(bundle, change("character_move", target="Hollis", value="cast"))
    hollis = bundle.cast[-1]
    assert hollis.name == "Hollis" and hollis.id in bundle.story.scene.present_character_ids
    assert "Hollis" not in bundle.story.scene.present_others

    bundle.story.held_character_id = "char-serrik"
    with pytest.raises(ValueError, match="holding"):
        apply_change(bundle, change("character_move", target="Serrik Vaun", value="supporting"))
    apply_change(bundle, change("character_move", target="Maela Orr", value="supporting"))
    assert bundle.supporting.characters[-1].name == "Maela Orr"
    assert "Maela Orr" in bundle.story.scene.present_others
    with pytest.raises(ValueError, match="already"):
        apply_change(bundle, change("character_move", target="Maela Orr", value="supporting"))


def test_renaming_a_supporting_card_renames_them_on_every_roster(bundle: StoryBundle):
    apply_change(bundle, change("character_add", target="supporting", value={"name": "Hollis"}))
    bundle.story.scene.present_others = ["Constable Hollis", "Gus"]
    bundle.nodes.append(
        Node(
            kind="user",
            speaker_id="x",
            content="",
            meta=NodeMeta(scene=SceneState(present_others=["Hollis"])),
        )
    )
    apply_change(bundle, change("character", target="Hollis", field="name", value="Hollis Ital"))
    assert bundle.story.scene.present_others == ["Hollis Ital", "Gus"]
    assert bundle.nodes[-1].meta.scene.present_others == ["Hollis Ital"]


def test_a_rename_and_an_edit_of_the_same_card_both_apply(bundle: StoryBundle):
    review = parse_review(
        review_of(
            {
                "kind": "character",
                "target": "Maela Orr",
                "field": "name",
                "value": "Maela Orr-Vane",
            },
            {"kind": "character", "target": "Maela Orr", "field": "voice", "value": "Quick."},
        ),
        bundle,
    )
    assert len(review.changes) == 2 and review.cannot_fix == []
    for item in ordered_for_apply(review.changes):
        apply_change(bundle, item)
    maela = bundle.cast[1]
    assert maela.name == "Maela Orr-Vane" and maela.voice_notes == "Quick."


def test_a_replacement_that_drops_text_is_marked(bundle: StoryBundle):
    bundle.story.world_bible = "\n".join(f"Rule {n} of the keep." for n in range(8))
    review = parse_review(
        review_of(
            {"kind": "world", "value": "Rule 0 of the keep.\nA new rule."},
            {"kind": "style", "field": "notes", "value": "Brief."},
        ),
        bundle,
    )
    world, notes = review.changes
    assert world.warning and "replaces 7 of the 8 lines" in world.warning
    assert notes.warning is None

    # An edit that keeps the text is not a loss.
    kept = parse_review(
        review_of({"kind": "world", "value": bundle.story.world_bible + "\nRule 8."}), bundle
    )
    assert kept.changes[0].warning is None


def test_a_review_may_propose_a_director_turn(bundle: StoryBundle):
    reply = json.dumps(
        {
            "summary": "History.",
            "changes": [],
            "cannot_fix": [],
            "director_turn": "Maela has had enough of running.",
            "history_bound": True,
        }
    )
    review = parse_review(reply, bundle)
    assert review.director_turn == "Maela has had enough of running."
    assert review.history_bound
    text = review_markdown(review, bundle, complaint="She keeps hiding.", model="m")
    assert "## Director turn" in text and "She keeps hiding." in text


def test_applying_a_review_can_be_undone(session: StorySession, tmp_path: Path):
    session.provider = MockChatProvider(
        [
            review_of(
                {"kind": "world", "value": "A different world."},
                {"kind": "character", "target": "Maela Orr", "field": "voice", "value": "Curt."},
                {"kind": "lore_add", "value": {"title": "Bells", "content": "Ring."}},
            )
        ]
    )
    review = session.review_settings("Wrong world.")
    assert session.last_review is not None and session.last_review.review == review

    session.apply_review(review.changes, sent_director_turn=False)
    assert session.story.world_bible == "A different world."
    assert session.last_review.applied_ids == [c.id for c in review.changes]
    assert session.review_undo is not None

    assert session.undo_review()
    assert session.story.world_bible.startswith("Calder Keep")
    assert session.cast[1].voice_notes is None and session.bundle.lore == []
    assert session.last_review.undone and session.review_undo is None
    assert not session.undo_review()

    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.world_bible.startswith("Calder Keep")
    assert saved.reviews[0].complaint == "Wrong world." and saved.reviews[0].undone


def test_reviews_travel_in_the_archive(session: StorySession, tmp_path: Path):
    session.provider = MockChatProvider([EMPTY_REVIEW])
    session.review_settings("Anything.")
    path = tmp_path / "out.sealedlore-archive.json"
    write_archive(path, session.bundle, [])
    bundle, _ = read_archive(path)
    assert bundle.reviews[0].complaint == "Anything."
    assert "reviews" in archive_data(session.bundle, [])


def test_marked_passages_are_sent_whatever_their_age(session: StorySession):
    played(session, 6)
    oldest = reply_nodes(session.path())[0]
    assert session.mark_for_review(oldest.id)
    sizes = session.review_sizes()
    assert [node_id for node_id, _ in sizes.marked] == [oldest.id]
    assert sizes.total(1, False) == sizes.total(1, False)

    session.provider = MockChatProvider([EMPTY_REVIEW])
    session.review_settings("x", replies=1)
    sent = session.provider.last_request.messages[1].text
    assert "Reply number 0." in sent and "Reply number 5." in sent
    assert "Reply number 3." not in sent
    assert not session.mark_for_review(oldest.id)


def test_a_fresh_playthrough_carries_every_reviewed_setting(bundle: StoryBundle):
    for item in [
        change("world", value="A new world."),
        change("style", field="notes", value="Terse."),
        change("style", field="perspective", value="with_character"),
        change("story", field="agency_mode", value="dice"),
        change("story", field="world_activity", value="eventful"),
        change("story", field="npc_scope", value="model_choice"),
        change("story", field="context_token_budget", value=12_000),
        change("opening", field="text", value="Begin at the gate."),
        change("character", target="Maela Orr", field="author_only", value=True),
        change("character", target="Serrik Vaun", field="playable", value=False),
        change("character_add", target="supporting", value={"name": "Hollis", "summary": "Law."}),
        change("lore_edit", target="The Keep", field="enabled", value=False),
        change("lore_add", value={"title": "Bells", "content": "Ring.", "always_on": True}),
    ]:
        apply_change(bundle, item)

    copy = fresh_playthrough(bundle, "again")
    assert copy.story.id != bundle.story.id and copy.nodes == []
    assert copy.story.world_bible == "A new world."
    assert copy.story.style.notes == "Terse." and copy.story.style.perspective == "with_character"
    defaults = copy.story.defaults
    assert (defaults.agency_mode, defaults.world_activity, defaults.npc_scope) == (
        "dice",
        "eventful",
        "model_choice",
    )
    assert defaults.context_token_budget == 12_000
    assert copy.story.setup.opening_text == "Begin at the gate."
    assert copy.cast[1].author_only and not copy.cast[0].is_player_available
    assert copy.supporting.characters[0].name == "Hollis"
    assert [(e.title, e.enabled, e.always_on) for e in copy.lore] == [
        ("The Keep", False, False),
        ("Bells", True, True),
    ]


def test_world_activity_travels_in_a_scenario_and_old_files_still_load(
    bundle: StoryBundle, tmp_path: Path
):
    bundle.story.defaults.world_activity = "quiet"
    path = tmp_path / "x.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(bundle))
    assert bundle_from_scenario(read_scenario(path)).story.defaults.world_activity == "quiet"

    data = json.loads(path.read_text())
    del data["world_activity"]
    path.write_text(json.dumps(data))
    assert bundle_from_scenario(read_scenario(path)).story.defaults.world_activity == "normal"


# The long test story's notes, as an old review wrote them.
NOTES = (
    "The story lives in the gap between two civilisations.\n\n"
    "The author now plays John Carver. Jane Moss is a character you voice freely: write "
    "her dialogue, actions, thoughts, and feelings as you would any other NPC. Do not write "
    "John's dialogue, inner thoughts, decisions, or actions unless the author states them.\n\n"
    "The author wants access to other characters' inner lives. Make regular, brief use of "
    "this. The author controls only John; the thoughts of everyone else are yours to reveal."
)


def test_style_notes_that_say_who_plays_whom_are_found():
    """They sit in the cached system block and never change: the first switch
    of character makes them contradict the roster."""
    from sealedlore.engine.authoring import stale_voicing_notes
    from sealedlore.models.character import Character

    cast = [Character(name="John Carver"), Character(name="Jane Moss")]
    found = stale_voicing_notes(NOTES, cast)
    assert found == [
        "The author now plays John Carver.",
        "Jane Moss is a character you voice freely: write her dialogue, actions, thoughts, "
        "and feelings as you would any other NPC.",
        "Do not write John's dialogue, inner thoughts, decisions, or actions unless the "
        "author states them.",
        "The author controls only John; the thoughts of everyone else are yours to reveal.",
    ]
    assert stale_voicing_notes("The author plays it straight.", cast) == []
    assert stale_voicing_notes(None, cast) == []


def test_the_reviewer_is_shown_the_stale_notes(bundle: StoryBundle):
    bundle.story.style.notes = "The author now plays Serrik Vaun."
    view = settings_view(bundle)
    assert view["playthrough"]["stale_voicing_notes"] == ["The author now plays Serrik Vaun."]
    bundle.story.style.notes = "Keep it grim."
    assert settings_view(bundle)["playthrough"]["stale_voicing_notes"] is None


def test_person_and_tense_are_fixed_once_the_story_has_begun(bundle: StoryBundle):
    """The author (Sept 2026): a switch leaves the story so far in the old
    voice. The review names it instead, pointing at a new playthrough."""
    bundle.story.style.person = "third"
    bundle.story.style.tense = None
    apply_change(bundle, change("style", field="person", value="second"))  # not begun
    bundle.story.style.person = "third"
    bundle.nodes = [Node(kind="assistant", speaker_id="__narrator__", content="It began.")]

    review = parse_review(
        review_of(
            {"kind": "style", "field": "person", "value": "second"},
            {"kind": "style", "field": "tense", "value": "present"},
            {"kind": "style", "field": "pacing", "value": "brisk"},
        ),
        bundle,
    )
    # Tense was never set, so it may be set once; person may not change.
    assert [c.field for c in review.changes] == ["tense", "pacing"]
    assert "Restart or Duplicate settings" in review.cannot_fix[0]
    assert '"person" and "tense" are fixed once the story has begun' in review_system()


def test_a_premise_draft_is_written_in_the_person_and_tense_chosen(tmp_path: Path):
    provider = MockChatProvider([json.dumps(DRAFT)])
    draft = StoryDraft(
        provider,
        "A winter at Hellsville",
        "m",
        person="second",
        tense="present",
        reasoning=ReasoningConfig(),
    )
    list(draft.stream())
    asked = provider.last_request.messages[-1].text
    assert 'second person and the present tense ("As you step inside' in asked
    assert 'write "opening" that way too, with the "play_as" character as "you"' in asked
    # The choice stands whatever the reply's style says.
    bundle, _ = draft.build(root=tmp_path)
    assert (bundle.story.style.person, bundle.story.style.tense) == ("second", "present")

    unchosen = build_generation_messages("A premise.")[-1].text
    assert "chosen how the story is told" not in unchosen


def test_setting_facts_are_kept_out_of_sentences_about_who_lacks_them():
    """Playtesting (Sept 2026): a world text saying one side "has no
    knowledge that" the other has a number of worlds had that side's
    characters quote the number in 3/5 replays, style note or not; reworded
    to name only the gap, 0/10. The drafter writes it that way, the review
    fixes the world text rather than adding a note, and the extended test
    story is written so."""

    assert "name\n  the gap without the fact" in draft_system()
    assert 'Never answer\n  this with "notes"' in review_system()
    story = (Path(__file__).parent / "data" / "doomsville_extended.md").read_text()
    assert "Hellsville has no knowledge\nof the Northern Camp." in story
    assert "Hellsville has no knowledge that" not in story
