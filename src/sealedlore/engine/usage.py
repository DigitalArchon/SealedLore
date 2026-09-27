"""What a story has cost, from the API log (§8.3).

The log is the only complete record: node usage covers turns, but summaries,
asides, character scans, skill suggestions, story drafting and settings
reviews are paid calls too. So totals
are read from every response entry in `api_log.jsonl`, by kind.

nano-gpt reports `cost` on some responses and omits it on others. A missing
cost is estimated from the model's listed prices when those are known (the
request entry names the model), and counted as unpriced otherwise — never as
$0.00 — so a total can say "about" or "at least" honestly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.pricing import estimate_cost
from sealedlore.models.config import ModelPrice
from sealedlore.providers.base import parse_usage

# Response entry kinds, and what each call was for.
KIND_LABELS: dict[str, str] = {
    "response": "Story turns",
    "aside": "Questions (asides)",
    "summary_response": "Chapter summaries",
    "ledger_response": "Story ledger",
    "merge_response": "Chapter merges",
    "characters_response": "Character scans",
    "scene_response": "Scene reads",
    "lore_pick_response": "Lore picks",
    "lore_decision_response": "Lore picks (Jev)",
    # The plot's calls: missing here, a story's total left out every one of them.
    "chronicle_response": "Plot: clock and facts",
    "director_response": "Plot: director",
    "variant_response": "Plot: choosing how events go",
    "gap_response": "Plot: skipped-over events",
    "competence_response": "Skill suggestions",
    "draft_response": "Story drafting",
    "review_response": "Settings reviews",
    "image_prompt_response": "Image prompts",
    # Priced per picture: the reply's cost, or the listed price (marked).
    "image_response": "Images",
    # A private scene kept in memory only logs what it cost and nothing else;
    # one kept on disk logs as usual (tagged private).
    "private_response": "Private scenes",
    "handoff_response": "Private scene handoffs",
}


@dataclass
class UsageLine:
    label: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost: float = 0.0
    # Calls whose cost was worked out from listed prices, and calls with no
    # cost at all (no report, no known price).
    estimated: int = 0
    unpriced: int = 0

    def add(
        self,
        raw: dict[str, Any],
        model: str | None = None,
        price: ModelPrice | None = None,
        *,
        cache_ttl: str = "5m",
    ) -> None:
        usage = parse_usage(raw)
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens
        if raw.get("cost_estimated") and isinstance(raw.get("cost"), (int, float)):
            self.cost += float(raw["cost"])
            self.estimated += 1
        elif isinstance(raw.get("cost"), (int, float)) or isinstance(
            raw.get("total_cost"), (int, float)
        ):
            self.cost += usage.cost
        elif price is not None and model:
            # The same lifetime the session prices with: an hour's cache
            # write costs about twice a five-minute one on nano-gpt, and the
            # two totals used to drift apart on every unpriced Claude turn.
            self.cost += estimate_cost(usage, price, model, cache_ttl=cache_ttl)
            self.estimated += 1
        else:
            self.unpriced += 1

    @property
    def cache_hit_rate(self) -> float | None:
        """Share of prompt tokens read from cache; None when nothing was sent."""
        if not self.prompt_tokens:
            return None
        return self.cache_read_tokens / self.prompt_tokens


@dataclass
class UsageReport:
    lines: list[UsageLine] = field(default_factory=list)
    total: UsageLine = field(default_factory=lambda: UsageLine("Total"))


def _kind_of(entry: dict[str, Any]) -> str | None:
    kind = entry.get("kind")
    if not isinstance(kind, str):
        return None
    if kind == "response" and entry.get("aside"):
        return "aside"
    return kind if kind in KIND_LABELS else None


def _text(value: Any) -> str:
    # The log can come from an imported archive, so its fields aren't trusted.
    return value if isinstance(value, str) else ""


def usage_report(
    entries: Iterable[dict[str, Any]],
    prices: Mapping[str, ModelPrice] | None = None,
    *,
    cache_ttl: str = "5m",
) -> UsageReport:
    prices = prices or {}
    entries = list(entries)
    # Responses don't name their model; their request entries do.
    model_of: dict[str, str] = {}
    for entry in entries:
        payload = entry.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            model_of[_text(entry.get("id"))] = payload["model"]

    by_kind: dict[str, UsageLine] = {}
    total = UsageLine("Total")
    for entry in entries:
        kind = _kind_of(entry)
        if kind is None:
            continue
        raw = entry.get("usage")
        if not isinstance(raw, dict):
            raw = {}
        model = model_of.get(_text(entry.get("request_log_ref")))
        price = prices.get(model) if model else None
        by_kind.setdefault(kind, UsageLine(KIND_LABELS[kind])).add(
            raw, model, price, cache_ttl=cache_ttl
        )
        total.add(raw, model, price, cache_ttl=cache_ttl)
    lines = [by_kind[kind] for kind in KIND_LABELS if kind in by_kind]
    return UsageReport(lines=lines, total=total)
