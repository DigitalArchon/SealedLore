"""Reasoning levels (engine/reasoning.py, engine/session_reasoning.py)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.reasoning import (
    config_params,
    nearest,
    reasoning_for,
    reasons_unasked,
)
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ModelReasoning, ProviderConfig
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.node import Usage
from sealedlore.providers.base import ChatRequest
from sealedlore.providers.mock import MockChatProvider
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
    """A mock with a models list, counting how often it is fetched."""

    def __init__(self, *args, listing: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetches = 0
        self.listing = listing or {}

    def fetch_model_prices(self):
        self.fetches += 1
        return self.listing


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


def test_a_model_that_doesnt_reason_unasked_is_never_asked_and_nothing_is_fetched(
    tmp_path: Path, story, cast
):
    provider = ListedMock(["Reply."], listing={"moonshotai/kimi-k3": KIMI_ENTRY})
    session = make_session(tmp_path, story, cast, provider)
    list(session.send(turn()))
    list(session.send(turn()))
    assert all("reasoning" not in payload for payload in provider.payloads)
    assert provider.fetches == 0


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
