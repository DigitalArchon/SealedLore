"""Reasoning levels (engine/reasoning.py, engine/session_reasoning.py)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.reasoning import (
    config_params,
    default_note,
    default_reasoning,
    listed,
    nearest,
    reasoning_for,
    reasons_unasked,
)
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ModelReasoning, ProviderConfig
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.node import Usage
from sealedlore.providers.base import ChatRequest, ProviderError
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.reasoning_defaults import defaults_url, trim
from sealedlore.providers.wire import build_chat_payload
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle

SRC = Path(__file__).resolve().parent.parent / "src" / "sealedlore"
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
KIMI = ModelReasoning(reasons=True, efforts=["low", "high", "max"])
DEEPSEEK = ModelReasoning(reasons=True, efforts=["none", "high", "max"])
SONNET = ModelReasoning(reasons=True, efforts=["low", "medium", "high", "xhigh", "max"])
NOTHING = ReasoningConfig()


def marked(facts: ModelReasoning, days_ago: float = 1) -> ModelReasoning:
    return facts.model_copy(update={"unasked_at": (NOW - timedelta(days=days_ago)).isoformat()})


# --- what is sent ---------------------------------------------------------------------


def test_as_low_as_possible_asks_nothing_of_a_model_not_caught_reasoning():
    """Sonnet 4.6 lists "low", but asked for it, it starts thinking (40-130
    tokens a turn, measured): a model that reasons only when asked isn't asked."""
    for facts in (SONNET, KIMI, DEEPSEEK, None):
        assert reasoning_for("least", "some/model", facts, now=NOW) == NOTHING


def test_a_model_caught_reasoning_unasked_gets_its_lowest_listed_level():
    assert reasoning_for("least", "moonshotai/kimi-k3", marked(KIMI), now=NOW) == ReasoningConfig(
        enabled=True, effort="low"
    )
    # DeepSeek's lowest is no reasoning at all, sent as such.
    assert reasoning_for("least", "deepseek/x", marked(DEEPSEEK), now=NOW).effort == "none"
    # Nothing listed: low, which endpoints commonly take.
    unlisted = marked(ModelReasoning())
    assert reasoning_for("least", "m", unlisted, now=NOW).effort == "low"


# NanoGPT's web listing, as it read on Oct 2 2026 (trimmed to what is read).
WEB = {
    "z-ai/glm-5.3": {"defaultSettings": {"reasoning_effort": "low"}},
    "z-ai/glm-5.3-flash": {
        "defaultSettings": {"reasoning_effort": "max", "thinking": {"type": "enabled"}}
    },
    "TEE/glm-5.3": {
        "defaultSettings": {"reasoning_effort": "max", "thinking": {"type": "enabled"}}
    },
    "moonshotai/kimi-k3": {"defaultSettings": {"reasoning_effort": "max"}},
    "anthropic/claude-opus-5.5": {
        "defaultSettings": {
            "reasoning_effort": "high",
            "thinking": {"type": "adaptive", "effort": "high"},
        }
    },
    "anthropic/claude-sonnet-4.6": {"defaultSettings": {}},
    "qwen/qwen3.8-27b-uncensored": {
        "defaultSettings": {"reasoning_effort": "none", "thinking": {"type": "disabled"}}
    },
    "xiaomi/mimo-v2.6-pro": {"defaultThinkingEnabled": True, "defaultSettings": {}},
    "inception/mercury-2.5-preview": {"defaultReasoningEffort": "medium", "defaultSettings": {}},
}


def test_the_web_listing_says_whether_a_model_reasons_by_default():
    assert default_reasoning(WEB["z-ai/glm-5.3"]) == (True, "low")
    assert default_reasoning(WEB["z-ai/glm-5.3-flash"]) == (True, "max")
    assert default_reasoning(WEB["anthropic/claude-opus-5.5"]) == (True, "high")
    assert default_reasoning(WEB["qwen/qwen3.8-27b-uncensored"]) == (False, None)
    assert default_reasoning(WEB["xiaomi/mimo-v2.6-pro"]) == (True, None)
    assert default_reasoning(WEB["inception/mercury-2.5-preview"]) == (True, "medium")
    # Saying nothing is not saying no: most models state no default.
    assert default_reasoning(WEB["anthropic/claude-sonnet-4.6"]) == (None, None)
    assert default_reasoning(None) == (None, None)
    assert default_reasoning({"defaultSettings": "junk"}) == (None, None)


def test_a_model_that_reasons_by_default_is_asked_for_its_lowest_from_the_start():
    """Kimi K3 thought ~900 tokens unasked, ~56 at low: no slow first turn
    to catch it on any more."""
    kimi = listed(KIMI_ENTRY, WEB["moonshotai/kimi-k3"])
    assert reasoning_for("least", "moonshotai/kimi-k3", kimi, now=NOW) == ReasoningConfig(
        enabled=True, effort="low"
    )
    opus = listed({"reasoning_efforts": SONNET.efforts}, WEB["anthropic/claude-opus-5.5"])
    assert reasoning_for("least", "anthropic/claude-opus-5.5", opus, now=NOW).effort == "low"


def test_a_default_that_is_already_the_lowest_is_left_alone():
    """GLM 5.3 thinks at low by default; sent low it once thought 187 tokens
    where it thought 9 unasked."""
    glm = listed({"reasoning_efforts": ["low", "high", "max"]}, WEB["z-ai/glm-5.3"])
    assert reasoning_for("least", "z-ai/glm-5.3", glm, now=NOW) == NOTHING


def test_no_default_or_nothing_to_ask_for_instead_sends_nothing():
    off = listed({"reasoning_efforts": ["none", "high"]}, WEB["qwen/qwen3.8-27b-uncensored"])
    assert reasoning_for("least", "qwen/x", off, now=NOW) == NOTHING
    # Thinks by default but lists no level: nothing known to ask for.
    mimo = listed({}, WEB["xiaomi/mimo-v2.6-pro"])
    assert reasoning_for("least", "xiaomi/mimo-v2.6-pro", mimo, now=NOW) == NOTHING
    # The defaults don't touch a chosen level.
    kimi = listed(KIMI_ENTRY, WEB["moonshotai/kimi-k3"])
    assert reasoning_for("medium", "moonshotai/kimi-k3", kimi, now=NOW).effort == "high"


def test_the_settings_note_says_what_a_model_does_left_to_itself():
    assert default_note(listed(KIMI_ENTRY, WEB["moonshotai/kimi-k3"])) == (
        "Left to itself, it reasons at max."
    )
    assert default_note(listed({}, WEB["qwen/qwen3.8-27b-uncensored"])) == (
        "It reasons only when asked."
    )
    assert default_note(listed({}, None)) == ""


def test_only_the_web_listing_fields_that_are_read_are_kept():
    entry = {
        "name": "GLM 5.3",
        "description": "long text",
        "defaultSettings": {"temperature": 1, "reasoning_effort": "low", "thinking": None},
        "defaultThinkingEnabled": True,
    }
    assert trim(entry) == {
        "defaultThinkingEnabled": True,
        "defaultSettings": {"reasoning_effort": "low", "thinking": None},
    }
    assert defaults_url("https://nano-gpt.com/api/v1") == "https://nano-gpt.com/api/models"


def test_only_nanogpt_is_asked_for_defaults():
    """Any other endpoint says nothing, and nothing is fetched for it."""
    provider = OpenAICompatibleProvider(
        ProviderConfig(name="o", base_url="https://openrouter.ai/api/v1", model="m")
    )
    try:
        assert provider.fetch_reasoning_defaults() is None
    finally:
        provider.close()


def test_the_mark_lapses_after_a_week_so_a_changed_model_is_checked_again():
    assert reasons_unasked(marked(KIMI, days_ago=6.9), now=NOW)
    assert not reasons_unasked(marked(KIMI, days_ago=7.1), now=NOW)
    assert reasoning_for("least", "m", marked(KIMI, days_ago=8), now=NOW) == NOTHING


def test_a_tee_model_starts_at_low():
    """Measured: a 100-word summary took 37s (TEE GLM) and 107s (encrypted
    GLM) at the model's default, 10s and 9s at low."""
    for model in ("TEE/glm-5.3-flash", "private/glm-5-3"):
        assert reasoning_for("least", model, None, now=NOW).effort == "low"


def test_a_chosen_level_maps_to_the_nearest_the_model_lists():
    assert nearest("medium", KIMI.efforts) == "high", "a tie goes up: more was asked for"
    assert nearest("max", ["low", "medium", "high"]) == "high"
    assert nearest("low", DEEPSEEK.efforts) == "high", "none never stands in for a level"
    assert reasoning_for("medium", "m", KIMI).effort == "high"
    assert reasoning_for("high", "m", SONNET).effort == "high"
    # Nothing listed: the level itself, max as high.
    assert reasoning_for("medium", "m", None).effort == "medium"
    assert reasoning_for("max", "m", ModelReasoning()).effort == "high"
    # Listed as not reasoning at all: nothing to ask for.
    assert reasoning_for("high", "m", ModelReasoning(reasons=False)) == NOTHING


def test_an_explicit_off_is_never_sent():
    """GLM 5.3 answers {"enabled": false} with HTTP 400 and Opus 5.5 with a
    400 or a disguised 503: "as little as possible" sends nothing instead."""

    def payload(reasoning: ReasoningConfig) -> dict:
        request = ChatRequest(model="m", messages=[], params=GenerationParams(reasoning=reasoning))
        return build_chat_payload(request)

    assert "reasoning" not in payload(NOTHING)
    assert payload(ReasoningConfig(effort="none"))["reasoning"] == {"effort": "none"}
    assert payload(ReasoningConfig(enabled=True, effort="low"))["reasoning"] == {
        "enabled": True,
        "effort": "low",
    }


def test_the_story_models_turns_take_the_authors_settings_and_other_calls_their_own():
    config = Config(
        generation=GenerationParams(temperature=0.6, max_tokens=3000),
        reasoning_story="high",
        reasoning_side="least",
        model_reasoning={"m": KIMI},
    )
    story = config_params(config, "story", "m")
    assert (story.temperature, story.max_tokens, story.reasoning.effort) == (0.6, 3000, "high")
    side = config_params(config, "side", "m", max_tokens=500)
    assert (side.temperature, side.max_tokens, side.reasoning) == (None, 500, NOTHING)
    # Never the config's own object: a request's params can't leak into the next.
    story.temperature = 1.9
    assert config.generation.temperature == 0.6


# --- in play --------------------------------------------------------------------------


class ListedMock(MockChatProvider):
    """A mock with a models list (and NanoGPT's defaults when given),
    counting how often each is fetched."""

    def __init__(self, *args, listing: dict | None = None, defaults=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetches = 0
        self.default_fetches = 0
        self.listing = listing or {}
        self.defaults = defaults

    def fetch_model_prices(self):
        self.fetches += 1
        return self.listing

    def fetch_reasoning_defaults(self):
        self.default_fetches += 1
        if isinstance(self.defaults, Exception):
            raise self.defaults
        return self.defaults


KIMI_ENTRY = {
    "id": "moonshotai/kimi-k3",
    "capabilities": {"reasoning": True},
    "reasoning_efforts": ["low", "high", "max"],
}


def make_session(tmp_path: Path, story, cast, provider, **config) -> StorySession:
    story.defaults.main_model = "moonshotai/kimi-k3"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    cfg = Config(
        providers=[ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="n",
        **config,
    )
    return StorySession(
        StoryBundle(story=story, cast=cast),
        cfg,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="Go.", controlled_character_id="char-serrik"
    )


def test_a_model_that_reasons_unasked_is_caught_said_once_and_asked_for_low(
    tmp_path: Path, story, cast
):
    provider = ListedMock(
        ["Reply."],
        reasoning="Let me think about this at length. " * 80,
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        listing={"moonshotai/kimi-k3": KIMI_ENTRY},
    )
    session = make_session(tmp_path, story, cast, provider)

    notes = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]
    assert "reasoning" not in provider.payloads[0], "nothing asked the first time"
    caught = [note for note in notes if "moonshotai/kimi-k3 reasoned at length" in note]
    assert len(caught) == 1 and "lowest reasoning level" in caught[0]
    assert load_config(root=tmp_path).model_reasoning["moonshotai/kimi-k3"].unasked_at

    # Kept under the passage it slowed, for the transcript to show.
    caught_on = [n for n in session.nodes if n.meta.reasoning_caught]
    assert [n.meta.reasoning_caught for n in caught_on] == [["moonshotai/kimi-k3"]]
    assert caught_on[0].kind == "assistant"

    notes = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]
    assert provider.payloads[-1]["reasoning"] == {"enabled": True, "effort": "low"}
    assert not any("reasoned" in note for note in notes), "said once, not every turn"
    assert session.nodes[-1].meta.reasoning_caught == []


def test_a_little_reasoning_unasked_is_left_alone(tmp_path: Path, story, cast):
    """GLM 5.3 always thinks a little (9 tokens, live); marked for it, it was
    sent low and thought 187. Only reasoning at length counts."""
    provider = ListedMock(["Reply."], reasoning="Hm, a short think. " * 20)
    session = make_session(tmp_path, story, cast, provider)
    notes = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]
    list(session.send(turn()))
    assert all("reasoning" not in payload for payload in provider.payloads)
    assert not any("reasoned at length" in note for note in notes)
    assert session.config.model_reasoning == {}


def test_a_model_that_doesnt_reason_unasked_is_never_asked(tmp_path: Path, story, cast):
    """Listed with no default, it is sent nothing; the listing is read once
    a session, not once a request."""
    provider = ListedMock(["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY})
    session = make_session(tmp_path, story, cast, provider)
    list(session.send(turn()))
    list(session.send(turn()))
    assert all("reasoning" not in payload for payload in provider.payloads)
    assert provider.fetches == 1


def test_a_chosen_level_fetches_the_listing_once_and_maps_to_it(tmp_path: Path, story, cast):
    provider = ListedMock(["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY})
    session = make_session(tmp_path, story, cast, provider, reasoning_story="medium")
    list(session.send(turn()))
    list(session.send(turn()))
    assert provider.payloads[0]["reasoning"] == {"enabled": True, "effort": "high"}
    assert provider.fetches == 1
    assert load_config(root=tmp_path).model_reasoning["moonshotai/kimi-k3"].efforts == [
        "low",
        "high",
        "max",
    ]


GLM_ENTRY = {"capabilities": {"reasoning": True}, "reasoning_efforts": ["low", "high", "max"]}


def test_the_listings_are_read_once_a_session_for_every_model(tmp_path: Path, story, cast):
    provider = ListedMock(
        ["Reply."] * 4,
        listing={"moonshotai/kimi-k3": KIMI_ENTRY, "z-ai/glm-5.3": GLM_ENTRY},
        defaults=WEB,
    )
    session = make_session(tmp_path, story, cast, provider)
    list(session.send(turn()))
    assert provider.payloads[0]["reasoning"] == {"enabled": True, "effort": "low"}, (
        "Kimi K3 reasons at max by default: asked for low from the first turn"
    )
    assert session.side_params("z-ai/glm-5.3").reasoning == NOTHING
    assert (provider.fetches, provider.default_fetches) == (1, 1)
    saved = load_config(root=tmp_path).model_reasoning["moonshotai/kimi-k3"]
    assert (saved.reasons_by_default, saved.default_effort, saved.defaults_checked) == (
        True,
        "max",
        True,
    )


def test_an_encrypted_model_takes_its_tee_twins_default(tmp_path: Path, story, cast):
    provider = ListedMock(["Reply."], listing={"private/glm-5-3": GLM_ENTRY}, defaults=WEB)
    session = make_session(tmp_path, story, cast, provider)
    facts = session.reasoning_facts("private/glm-5-3")
    assert (facts.reasons_by_default, facts.default_effort) == (True, "max")


def test_defaults_that_couldnt_be_read_are_tried_again_next_session(tmp_path: Path, story, cast):
    provider = ListedMock(
        ["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY}, defaults=ProviderError("down")
    )
    session = make_session(tmp_path, story, cast, provider)
    list(session.send(turn()))
    assert "reasoning" not in provider.payloads[0], "the turn still goes, as before"
    assert not load_config(root=tmp_path).model_reasoning["moonshotai/kimi-k3"].defaults_checked

    provider = ListedMock(["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY}, defaults=WEB)
    story.active_leaf_id = None
    session = make_session(tmp_path, story, cast, provider)
    session.config.model_reasoning = load_config(root=tmp_path).model_reasoning
    list(session.send(turn()))
    assert provider.default_fetches == 1
    assert provider.payloads[0]["reasoning"] == {"enabled": True, "effort": "low"}


def test_facts_saved_before_defaults_were_read_are_fetched_again(tmp_path: Path, story, cast):
    """A week-fresh entry from before this change has no default: read again."""
    from sealedlore.ids import utc_now_iso

    provider = ListedMock(["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY}, defaults=WEB)
    old = KIMI.model_copy(update={"fetched_at": utc_now_iso()})
    session = make_session(
        tmp_path, story, cast, provider, model_reasoning={"moonshotai/kimi-k3": old}
    )
    list(session.send(turn()))
    assert provider.fetches == 1
    assert provider.payloads[0]["reasoning"] == {"enabled": True, "effort": "low"}


def test_a_memory_only_chat_is_caught_for_the_session_only(tmp_path: Path, story, cast):
    story.mode = "chat"
    story.chat_keep = "memory"
    provider = ListedMock(["Reply."], reasoning="Thinking it over. " * 150)
    session = make_session(tmp_path, story, cast, provider)
    list(session.send(turn()))
    list(session.send(turn()))
    assert provider.payloads[-1]["reasoning"] == {"enabled": True, "effort": "low"}
    assert session.config.model_reasoning == {}
    assert load_config(root=tmp_path).model_reasoning == {}


# --- every request says how much to reason ----------------------------------------------


def _calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == name:
            yield node


def test_every_request_says_how_much_to_reason():
    """A request built with default params asks for nothing whatever the
    author chose. So every ChatRequest names its params, and a
    GenerationParams built outside engine/reasoning.py names its reasoning
    (the session's `story_params` / `side_params`, `config_params`, or a
    caller's `reasoning`)."""
    missing = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("models/") or relative in (
            "engine/reasoning.py",
            "providers/base.py",
        ):
            continue
        tree = ast.parse(path.read_text())
        for call in _calls(tree, "ChatRequest"):
            if not any(keyword.arg == "params" for keyword in call.keywords):
                missing.append(f"{relative}:{call.lineno} ChatRequest without params")
        for call in _calls(tree, "GenerationParams"):
            keywords = {keyword.arg for keyword in call.keywords}
            # The settings dialog's and the review's sampling settings carry
            # no reasoning of their own: it is set per request.
            if "reasoning" not in keywords and relative not in (
                "gui/settings_dialog.py",
                "engine/authoring.py",
            ):
                missing.append(f"{relative}:{call.lineno} GenerationParams without reasoning")
    assert not missing, "\n".join(missing)


@pytest.mark.parametrize(
    "stale",
    [
        {"generation": {"max_tokens": 1500, "reasoning": {"enabled": False, "effort": "low"}}},
    ],
)
def test_an_older_storys_own_generation_settings_are_ignored(stale):
    from sealedlore.models.story import StoryDefaults

    defaults = StoryDefaults.model_validate({**stale, "context_token_budget": 45_000})
    assert defaults.context_token_budget == 45_000
    assert "generation" not in defaults.model_dump()


def test_the_session_never_applies_a_generation_suggestion(tmp_path: Path, story, cast):
    from sealedlore.models.authoring import SettingsChange

    session = make_session(tmp_path, story, cast, MockChatProvider(["Reply."]))
    change = SettingsChange(kind="generation", field="temperature", value=0.2)
    results = session.apply_review([change])
    assert "Settings → Generation" in (results[change.id] or "")
    assert session.config.generation.temperature is None
