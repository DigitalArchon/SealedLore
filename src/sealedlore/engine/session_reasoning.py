"""The session's side of reasoning levels (engine/reasoning.py): what each
model lists, which models reason unasked, and each request's parameters.

Every request the engine builds takes its `params` from `story_params` (the
story model's own turns) or `side_params` (everything else);
tests/test_reasoning.py fails on one that doesn't.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sealedlore.engine.reasoning import (
    EXCESSIVE_REASONING,
    CallKind,
    caught_note,
    level_for,
    listed,
    params_for,
    reasons_unasked,
)
from sealedlore.ids import utc_now_iso
from sealedlore.models.config import ModelReasoning
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCompleted
from sealedlore.providers.tee import is_tee

# How long a model's listed reasoning levels are kept before they're fetched again.
LISTING_DAYS = 7


def _fresh(facts: ModelReasoning) -> bool:
    if not facts.fetched_at:
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
        try:
            listing = self.provider.fetch_model_prices()
        except ProviderError:
            listing = {}
        found = listed(listing.get(model))
        if found is None:
            self._reasoning_misses.add(model)
            return known
        found.fetched_at = utc_now_iso()
        found.unasked_at = known.unasked_at if known is not None else None
        with self._reasoning_lock:
            self._reasoning_store()[model] = found
        self._save_config()
        return found

    def _known_reasoning(self, model: str) -> ModelReasoning | None:
        with self._reasoning_lock:
            return self._reasoning_store().get(model) or self.config.model_reasoning.get(model)

    def _params(self, kind: CallKind, model: str, fixed: dict[str, Any]) -> GenerationParams:
        """The listing is fetched only when the level needs it: "as low as
        possible" sends an unmarked model nothing, whatever it lists."""
        facts = self._known_reasoning(model)
        if level_for(self.config, kind) != "least" or is_tee(model) or reasons_unasked(facts):
            facts = self.reasoning_facts(model)
        return params_for(self.config, kind, model, facts, **fixed)

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

    def reasoning_notices(self) -> list[str]:
        """What to tell the author about models caught reasoning unasked since
        last asked, once each."""
        with self._reasoning_lock:
            caught, self._caught_reasoning = self._caught_reasoning, []
        return [caught_note(model) for model in dict.fromkeys(caught)]
