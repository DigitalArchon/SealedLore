"""Estimating unreported costs from listed prices (§8.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.pricing import estimate_cost, parse_price
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.config import Config, ModelPrice, ProviderConfig
from sealedlore.models.node import Usage
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle

NANO_SONNET = {
    "id": "anthropic/claude-sonnet-4.6",
    "pricing": {
        "prompt": 3,
        "completion": 15,
        "cacheReadInputPer1kTokens": 0.0003,
        "currency": "USD",
        "unit": "per_million_tokens",
    },
}


def test_nano_gpt_prices_are_per_million_with_a_per_thousand_cache_read():
    price = parse_price(NANO_SONNET)
    assert (price.prompt, price.completion) == (3, 15)
    assert price.cache_read == pytest.approx(0.3)
    assert price.cache_write is None


def test_openrouter_prices_are_per_token_strings():
    price = parse_price(
        {
            "pricing": {
                "prompt": "0.000003",
                "completion": "0.000015",
                "input_cache_read": "0.0000003",
            }
        }
    )
    assert price.prompt == pytest.approx(3) and price.completion == pytest.approx(15)
    assert price.cache_read == pytest.approx(0.3)


def test_a_model_without_prices_has_none():
    assert parse_price({"id": "x"}) is None
    assert parse_price({"pricing": {"prompt": "free?"}}) is None


def test_the_estimate_splits_fresh_cached_and_written_input():
    """A real logged turn: 6,539 in = 1,437 fresh + 4,876 read + 226 written."""
    usage = Usage(
        prompt_tokens=6539, completion_tokens=123, cache_read_tokens=4876, cache_creation_tokens=226
    )
    cost = estimate_cost(usage, parse_price(NANO_SONNET), "anthropic/claude-sonnet-4.6")
    expected = (1437 * 3 + 4876 * 0.3 + 226 * 3 * 1.25 + 123 * 15) / 1_000_000
    assert cost == pytest.approx(expected)
    # An hour's cache is written at twice the base price.
    hour = estimate_cost(
        usage, parse_price(NANO_SONNET), "anthropic/claude-sonnet-4.6", cache_ttl="1h"
    )
    assert hour == pytest.approx(expected + 226 * 3 * 0.75 / 1_000_000)


class PricedMock(MockChatProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetches = 0

    def fetch_model_prices(self):
        self.fetches += 1
        return {"anthropic/claude-sonnet-4.5": {**NANO_SONNET, "id": "anthropic/claude-sonnet-4.5"}}


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="n",
    )
    return StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        PricedMock(["Reply."], usage=Usage(prompt_tokens=1000, completion_tokens=100)),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="Go.", controlled_character_id="char-serrik"
    )


def test_an_unreported_cost_is_estimated_and_marked(session: StorySession, tmp_path: Path):
    list(session.send(turn()))

    usage = session.nodes[-1].meta.usage
    assert usage.cost_estimated
    assert usage.cost == pytest.approx((1000 * 3 + 100 * 15) / 1_000_000)
    # The price is cached in config, and fetched once.
    list(session.send(turn()))
    assert session.provider.fetches == 1
    assert "anthropic/claude-sonnet-4.5" in load_config(root=tmp_path).model_prices


def test_a_reported_cost_is_left_alone(session: StorySession):
    session.provider = PricedMock(["Reply."], usage=Usage(prompt_tokens=1000, cost=0.5))
    list(session.send(turn()))
    usage = session.nodes[-1].meta.usage
    assert usage.cost == 0.5 and not usage.cost_estimated
    assert session.provider.fetches == 0


def test_a_reported_zero_is_free_not_unpriced(session: StorySession):
    # A subscription's included model: nano-gpt reports cost 0.
    usage = Usage(prompt_tokens=1000, cost=0.0, cost_reported=True)
    session.provider = PricedMock(["Reply."], usage=usage)
    list(session.send(turn()))
    usage = session.nodes[-1].meta.usage
    assert usage.cost == 0 and not usage.cost_estimated
    assert session.provider.fetches == 0


def test_a_model_with_no_listed_price_stays_unpriced_and_is_not_asked_twice(session: StorySession):
    session.story.defaults.main_model = "someone/unlisted"
    list(session.send(turn()))
    list(session.send(turn()))
    assert session.nodes[-1].meta.usage.cost == 0
    assert session.provider.fetches == 1


def test_the_report_estimates_old_log_entries_from_their_request_model():
    entries = [
        {"id": "r1", "kind": "request", "payload": {"model": "anthropic/claude-sonnet-4.6"}},
        {"kind": "response", "request_log_ref": "r1", "usage": {"prompt_tokens": 1000}},
        {"id": "r2", "kind": "request", "payload": {"model": "someone/unlisted"}},
        {"kind": "response", "request_log_ref": "r2", "usage": {"prompt_tokens": 1000}},
    ]
    prices = {"anthropic/claude-sonnet-4.6": ModelPrice(prompt=3, completion=15)}
    total = usage_report(entries, prices).total
    assert total.cost == pytest.approx(0.003)
    assert (total.estimated, total.unpriced) == (1, 1)
