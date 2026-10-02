"""The session's side of reasoning levels (engine/reasoning.py): what each
model lists and does by default, which models reason unasked, and each
request's parameters.

Every request the engine builds takes its `params` from `story_params` (the
story model's own turns) or `side_params` (everything else);
tests/test_reasoning.py fails on one that doesn't.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sealedlore.engine.pricing import parse_price
from sealedlore.engine.reasoning import (
    EXCESSIVE_REASONING,
    CallKind,
    caught_note,
    level_for,
    listed,
    params_for,
    reasoning_for,
    reasons_unasked,
)
from sealedlore.ids import utc_now_iso
from sealedlore.models.config import ModelPrice, ModelReasoning
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.node import Node
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCompleted
from sealedlore.providers.private_catalog import tee_counterpart

# How long a model's listed reasoning levels are kept before they're fetched again.
LISTING_DAYS = 7


def _fresh(facts: ModelReasoning) -> bool:
    if not facts.fetched_at or not facts.defaults_checked:
        return False
    try:
        fetched = datetime.fromisoformat(facts.fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return datetime.now(UTC) - fetched < timedelta(days=LISTING_DAYS)


class ReasoningRuntime:
    """Mixed into StorySession."""

    def _init_reasoning(self) -> None:
        self._reasoning_lock = threading.Lock()
        # A memory-only chat's: it teaches the config nothing.
        self._session_reasoning: dict[str, ModelReasoning] = {}
        self._reasoning_misses: set[str] = set()
        # Both listings, read once a session into each model's facts and
        # prices (the two are ~2.7 MB; nothing else of them is kept).
        self._listing_lock = threading.Lock()
        self._listed: dict[str, ModelReasoning] | None = None
        self._listed_prices: dict[str, ModelPrice] | None = None
        # Models caught reasoning unasked since the notices were last said.
        self._caught_reasoning: list[str] = []

    def _reasoning_store(self) -> dict[str, ModelReasoning]:
        return self._session_reasoning if self.memory_only else self.config.model_reasoning

    def reasoning_facts(self, model: str) -> ModelReasoning | None:
        """What is known about `model`'s reasoning: the listing (fetched when
        missing or a week old, never in a private scene, where the main
        endpoint isn't called) and the "reasons unasked" mark."""
        known = self._known_reasoning(model)
        if known is not None and _fresh(known):
            return known
        if model in self._reasoning_misses or self.in_private:
            return known
        found = self._listing().get(model)
        if found is None:
            self._reasoning_misses.add(model)
            return known
        found = found.model_copy()
        found.fetched_at = utc_now_iso()
        found.unasked_at = known.unasked_at if known is not None else None
        with self._reasoning_lock:
            self._reasoning_store()[model] = found
        self._save_config()
        return found

    def _read_listing(self) -> None:
        """Every listed model's facts and prices, fetched on first need in
        the session; a failed fetch is tried again by the next model to
        need it. A `private/` model takes its `TEE/` counterpart's default,
        as it takes its prices. Defaults that couldn't be read leave the
        facts unchecked, so a later session tries again."""
        with self._listing_lock:
            if self._listed is not None:
                return
            try:
                listing = self.provider.fetch_model_prices()
            except ProviderError:
                return
            try:
                defaults = self.provider.fetch_reasoning_defaults()
                checked = True
            except ProviderError:
                defaults, checked = None, False
            defaults = defaults or {}
            facts_by_model: dict[str, ModelReasoning] = {}
            prices: dict[str, ModelPrice] = {}
            for model, entry in listing.items():
                own = defaults.get(model)
                if own is None:
                    twin = tee_counterpart(model, defaults)
                    own = defaults.get(twin) if twin else None
                facts = listed(entry, own)
                if facts is not None:
                    facts.defaults_checked = checked
                    facts_by_model[model] = facts
                price = parse_price(entry) if entry else None
                if price is not None:
                    prices[model] = price
            self._listed, self._listed_prices = facts_by_model, prices

    def _listing(self) -> dict[str, ModelReasoning]:
        self._read_listing()
        return self._listed or {}

    def listed_prices(self) -> dict[str, ModelPrice] | None:
        """The listing's prices, read with the facts; None when it couldn't
        be fetched."""
        self._read_listing()
        return self._listed_prices

    def _known_reasoning(self, model: str) -> ModelReasoning | None:
        with self._reasoning_lock:
            return self._reasoning_store().get(model) or self.config.model_reasoning.get(model)

    def _params(self, kind: CallKind, model: str, fixed: dict[str, Any]) -> GenerationParams:
        """Every level needs the listing now, "as low as possible" for the
        model's default: fetched once a session, kept a week per model."""
        return params_for(self.config, kind, model, self.reasoning_facts(model), **fixed)

    def reasoning_sent(self, kind: CallKind, model: str) -> ReasoningConfig:
        """What a call of `kind` to `model` would ask, from what is known now
        (nothing is fetched: for the window's tools)."""
        return reasoning_for(level_for(self.config, kind), model, self._known_reasoning(model))

    def story_params(self, model: str, **fixed: Any) -> GenerationParams:
        """A story-model turn's parameters: the author's generation settings
        and their story reasoning level."""
        return self._params("story", model, fixed)

    def side_params(self, model: str, **fixed: Any) -> GenerationParams:
        """Any other call's parameters: its own limits, and the author's level
        for other calls."""
        return self._params("side", model, fixed)

    def _watch_reasoning(self, request: ChatRequest, completed: StreamCompleted) -> None:
        """On whichever thread the reply came back: it reasoned though its
        request asked for nothing. At length, the model is marked."""
        if completed.reasoning_tokens < EXCESSIVE_REASONING:
            return
        model = request.model
        with self._reasoning_lock:
            store = self._reasoning_store()
            known = store.get(model) or self.config.model_reasoning.get(model)
            if reasons_unasked(known):
                return
            facts = (known or ModelReasoning()).model_copy()
            facts.unasked_at = utc_now_iso()
            store[model] = facts
            self._caught_reasoning.append(model)
        self._save_config()

    def reasoning_notices(self, passage: Node | None = None) -> list[str]:
        """What to tell the author about models caught reasoning unasked since
        last asked, once each. With `passage`, they are kept on it too, for
        the transcript to show under it."""
        with self._reasoning_lock:
            caught, self._caught_reasoning = self._caught_reasoning, []
        models = list(dict.fromkeys(caught))
        if models and passage is not None:
            passage.meta.reasoning_caught = list(
                dict.fromkeys([*passage.meta.reasoning_caught, *models])
            )
            self.save()
        return [caught_note(model) for model in models]
