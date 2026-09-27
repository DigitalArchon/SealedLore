"""Token estimation.

No exact tokenizer exists across providers, so this is deliberately an
estimator: tiktoken's o200k_base, scaled by a per-model correction factor
that is updated from the provider's reported prompt_tokens after each
response. Estimates must always be presented as approximate in the UI.

tiktoken fetches its encoding on first use, so the encoder is loaded lazily
and falls back to a character heuristic when unavailable — a desktop app has
to keep working offline, and tests must never reach the network.
"""

from __future__ import annotations

import math
from collections.abc import Callable

DEFAULT_ENCODING = "o200k_base"
FALLBACK_CHARS_PER_TOKEN = 4.0

# Corrections beyond this range mean something other than tokenizer drift is
# going on (a wrong section, a provider counting images), so don't chase them.
MIN_CORRECTION_FACTOR = 0.5
MAX_CORRECTION_FACTOR = 2.0
DEFAULT_SMOOTHING = 0.25


def fallback_counter(text: str) -> int:
    return math.ceil(len(text) / FALLBACK_CHARS_PER_TOKEN)


class TokenEstimator:
    """Counts tokens in text, corrected per model.

    Pass `counter` to supply an exact counter or to keep tests off the network.
    """

    def __init__(
        self,
        *,
        factors: dict[str, float] | None = None,
        counter: Callable[[str], int] | None = None,
        encoding_name: str = DEFAULT_ENCODING,
    ) -> None:
        self.factors = factors if factors is not None else {}
        self._encoding_name = encoding_name
        self._counter = counter
        self._counter_is_exact = counter is not None
        self._loaded = counter is not None

    @property
    def is_approximate(self) -> bool:
        """True when counts come from the character heuristic, not a tokenizer."""
        self._ensure_counter()
        return not self._counter_is_exact

    def _ensure_counter(self) -> Callable[[str], int]:
        # `_loaded` is set only once the counter exists: the estimator is shared
        # with background threads (archival, the reads), and one that saw
        # `_loaded` before `_counter` was assigned found None (live, two
        # threads assembling at once).
        if not self._loaded:
            try:
                import tiktoken

                encoding = tiktoken.get_encoding(self._encoding_name)
            except Exception:
                self._counter_is_exact = False
                self._counter = fallback_counter
            else:
                self._counter_is_exact = True
                self._counter = lambda text: len(encoding.encode(text))
            self._loaded = True
        assert self._counter is not None
        return self._counter

    def factor_for(self, model: str | None) -> float:
        if model is None:
            return 1.0
        return self.factors.get(model, 1.0)

    def raw_count(self, text: str) -> int:
        if not text:
            return 0
        return self._ensure_counter()(text)

    def estimate(self, text: str, model: str | None = None) -> int:
        if not text:
            return 0
        return math.ceil(self.raw_count(text) * self.factor_for(model))


def updated_correction_factor(
    current_factor: float,
    corrected_estimate: int,
    reported_prompt_tokens: int,
    *,
    smoothing: float = DEFAULT_SMOOTHING,
) -> float:
    """Roll the per-model correction factor towards what the provider reported.

    `corrected_estimate` is what we predicted *including* `current_factor`, so
    the raw estimate is recovered by dividing it back out.
    """
    if corrected_estimate <= 0 or reported_prompt_tokens <= 0 or current_factor <= 0:
        return current_factor

    raw_estimate = corrected_estimate / current_factor
    observed = reported_prompt_tokens / raw_estimate
    rolled = current_factor * (1.0 - smoothing) + observed * smoothing
    return min(max(rolled, MIN_CORRECTION_FACTOR), MAX_CORRECTION_FACTOR)
