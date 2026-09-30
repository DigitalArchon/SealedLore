"""Help → Model trouble: is a slow or worse model its host, the model, or
the prompt? The measuring is engine/speed_test.py's (`run_speed_test`, with
a prompt of its own); this chooses the hosts, builds the built-in test
prompt, estimates the cost, sums up each host's runs and says what they show.
Pure: the calls are made elsewhere.

Why a tool and not a list kept in code (the author, Sept 30 2026): NanoGPT
has hundreds of models, some on fifty hosts, and hosts change overnight.
Measured then (six hosts pinned per model, the same 49 scene reads each):
Morph reasoned 4-5x the model maker's own host on GLM 5.3 whatever was
asked; on Kimi K3, SCX ignored "low" entirely while Sail Research, Novita and
Baseten honoured it; Sail answered in 4s where Baseten's worst runs took over
three minutes. The subscription's routing lands on any of them.

A host is pinned with `{"only": [host], "allow_fallbacks": false}`, and the
bill says whether that host served it (NanoGPT names none in a reply). Any
pinned call is billed pay-as-you-go, even for a model the subscription
includes.
"""

from __future__ import annotations

import statistics
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.reasoning import EXCESSIVE_REASONING
from sealedlore.engine.routing import PRICE_TOLERANCE, HostPrice, describe_sent, expected_cost
from sealedlore.engine.speed_test import SpeedResult
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ModelPrice
from sealedlore.models.generation import ReasoningConfig
from sealedlore.providers.model_hosts import Host, ModelHosts

Mode = Literal["slow", "worse"]

# Hosts tried beside the current route, before the author ticks any more.
DEFAULT_OTHERS = 4
# Runs per host for the "slow" check: one reply misleads. On an honest host
# a scene read now and then reasons several times its median.
DEFAULT_RUNS = 3
# The test's ceiling: room for a model that reasons at length, since that
# is what is being looked for (and only what is used is paid for).
MAX_TOKENS = 4000
# For the estimate: what a reply is likely to cost, and at most.
TYPICAL_OUTPUT = 700

# What counts as a host's own problem, against the other hosts' medians.
OVERTHINK_RATIO = 3.0
OVERTHINK_MIN = 300
SLOW_RATIO = 2.0
SLOW_MIN_EXTRA = 5.0


def pinned(host: str) -> dict[str, Any]:
    """The request fields that send a call to `host` and nowhere else."""
    return {"provider": {"only": [host], "allow_fallbacks": False}}


@dataclass(frozen=True)
class Candidate:
    """One way to reach the model: the role's current route (`key` ""), or
    one host pinned."""

    key: str
    label: str
    route: dict[str, Any]
    host: Host | None = None
    official: bool = False
    # Another model altogether: a TEE model's equally private alternatives,
    # which have one host each and so no host to choose.
    model: str | None = None

    @property
    def current(self) -> bool:
        return self.key == ""


def current_label(route: dict[str, Any], hosts: ModelHosts | None) -> str:
    provider = route.get("provider") or {}
    if not provider:
        return "Your route now: NanoGPT's own routing"
    order = provider.get("order") or []
    if order and hosts is not None:
        host = hosts.host(str(order[0]))
        if host is not None:
            return f"Your route now: {host.name}"
    return f"Your route now: {describe_sent(provider)}"


def _host_label(host: Host, official: bool) -> str:
    return f"{host.name} (the model maker's own)" if official else host.name


def choices(
    hosts: ModelHosts | None, current_route: dict[str, Any], *, bad: Collection[str]
) -> list[Candidate]:
    """Every host that could be tried, the current route first; hosts marked
    bad are left out."""
    found = [Candidate("", current_label(current_route, hosts), dict(current_route))]
    if hosts is None:
        return found
    for host in hosts.available:
        if host.id in bad:
            continue
        official = host.id == hosts.official
        found.append(
            Candidate(host.id, _host_label(host, official), pinned(host.id), host, official)
        )
    return found


def default_picks(
    candidates: Sequence[Candidate], *, floor: bool = True, count: int = DEFAULT_OTHERS
) -> list[str]:
    """Which to try unless the author ticks otherwise: the current route,
    then hosts that keep nothing whenever any are listed (the author, Sept
    30 2026: always the zero-retention ones), the maker's own first if it is
    one, then the cheapest at FP8 or better, since NanoGPT's routing picks
    the cheapest. With no such host, any host, the maker's own first,
    whatever its precision: it is the reference."""
    picks = [c.key for c in candidates if c.current]
    hosts = [c for c in candidates if c.host is not None]
    keeping_nothing = [c for c in hosts if c.host.privacy == "zdr"]
    pool = keeping_nothing or hosts
    official = [c for c in pool if c.official][:1]
    others = sorted(
        (c for c in pool if not c.official and (c.host.fp8_or_better or not floor)),
        key=lambda c: (c.host.input_price is None, c.host.input_price or 0.0),
    )
    chosen = [*official, *others][:count]
    return picks + [c.key for c in chosen]


# --- the built-in test prompt ---------------------------------------------------

_SETTING = """\
# SETTING

Doomsville is a small town on the edge of a frozen northern forest, a winter
after the world went quiet. The power comes and goes; the radio mostly
doesn't. Twenty or so people are left, living in the church, the diner and the
hardware store, trading with whoever passes. They don't trust strangers, and
they have reason not to: shamblers come out of the tree line when the snow is
deep, and two weeks ago a family on the Hellsville road stopped answering.

# CAST

- John: a drifter who arrived three days ago with a hunting rifle and not
  much else. Quiet, watchful, good with engines. Played by the author.
- Mae Carter: runs the diner, and in practice the town. Sixties, blunt,
  keeps a shotgun under the counter and a ledger of who owes what.
- Deputy Ray Holt: the only law left. Young, tired, trying to do the job
  his father did. Doesn't like John and hasn't decided why.
- Ben Stone: an old trapper who lives in the caves above town and comes down
  once a month to trade. Knows the forest better than anyone.

# LENGTH

Two to four paragraphs, about 150 to 300 words."""

_OPENING = (
    "Director: Begin in Mae's diner, late afternoon, snow coming. John has been in town three days."
)

_EARLIER = """\
The diner's windows had frosted over from the inside, and Mae had given up
scraping them. She set a mug of chicory coffee in front of John without being
asked and tapped the ledger beside it.

"You fixed the generator," she said. "That's a week of meals. Don't make me
regret it."

Across the room Deputy Holt sat with his back to the wall, hat on the table,
watching John the way he'd watch a dog he hadn't met. Outside, the wind
picked up, throwing snow against the glass in long, hissing sheets. Somewhere
past the church a door banged, once, twice, and stopped."""

_TURN = """\
John: I wrap both hands round the mug and nod at the window. "That door. Does
it always do that, or should somebody go look?" I keep my voice low, and I
don't look at the deputy when I say it."""

_REMEMBER = (
    "REMEMBER: John belongs to the author. Do not write his speech, thoughts, "
    "decisions, or unstated actions. Write everyone and everything else, and show "
    "how those present react. Length: two to four paragraphs, about 150 to 300 words. "
    "Continue the scene now, in prose only."
)


def check_messages(run: int = 1, texts: PromptTexts = DEFAULT_TEXTS) -> tuple[PromptMessage, ...]:
    """A storyteller's turn as SealedLore sends one, about 2,000 tokens, with
    nothing from any story in it. `run` makes each run's prompt start
    differently, so no host answers a later run from the cache of an earlier
    one and looks faster than it is."""
    system = f"Check {run}.\n\n{texts['storyteller.rules']}\n\n{_SETTING}"
    return (
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=(ContentPart(text=_OPENING),)),
        PromptMessage(role="assistant", parts=(ContentPart(text=_EARLIER),)),
        PromptMessage(role="user", parts=(ContentPart(text=f"{_TURN}\n\n{_REMEMBER}"),)),
    )


# --- what it will cost ----------------------------------------------------------


def estimate(
    candidates: Sequence[Candidate],
    prompt_tokens: int,
    runs: int,
    *,
    max_tokens: int = MAX_TOKENS,
    listed: ModelPrice | None = None,
) -> tuple[float, float, bool]:
    """(likely, at most, every price known) in USD. A host's own price, or the
    model's listed price for the current route; a candidate with neither
    isn't counted, and `every price known` says so."""
    likely = most = 0.0
    known = True
    for candidate in candidates:
        host = candidate.host
        price_in = host.input_price if host is not None else (listed.prompt if listed else None)
        price_out = (
            host.output_price if host is not None else (listed.completion if listed else None)
        )
        if price_in is None or price_out is None:
            known = False
            continue
        prompt = prompt_tokens * price_in
        likely += runs * (prompt + TYPICAL_OUTPUT * price_out) / 1e6
        most += runs * (prompt + max_tokens * price_out) / 1e6
    return likely, most, known


# --- what the runs show -----------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def billed_by(result: SpeedResult, host: Host | None) -> bool | None:
    """Whether the bill is the host's own price: None when it can't tell."""
    usage = result.usage
    if host is None or usage is None or not usage.cost_reported:
        return None
    if usage.cost == 0:
        return False  # the subscription served it, not the host
    if host.input_price is None or host.output_price is None:
        return None
    price = HostPrice(host.input_price, host.output_price, host.cache_read_price)
    expected = expected_cost(usage, price)
    return expected > 0 and abs(usage.cost - expected) <= PRICE_TOLERANCE * expected


@dataclass(frozen=True)
class HostSummary:
    candidate: Candidate
    results: tuple[SpeedResult, ...]
    asked: ReasoningConfig = field(default_factory=ReasoningConfig)

    @property
    def answered(self) -> list[SpeedResult]:
        return [r for r in self.results if r.ok]

    @property
    def errors(self) -> list[str]:
        return [r.error for r in self.results if r.error]

    @property
    def first_word(self) -> float | None:
        return _median([r.first_text for r in self.answered if r.first_text is not None])

    @property
    def worst_first_word(self) -> float | None:
        waits = [r.first_text for r in self.answered if r.first_text is not None]
        return max(waits) if waits else None

    @property
    def reasoning(self) -> float | None:
        return _median([r.reasoning_estimate for r in self.answered])

    @property
    def worst_reasoning(self) -> int | None:
        counts = [r.reasoning_estimate for r in self.answered]
        return max(counts) if counts else None

    @property
    def rate(self) -> float | None:
        return _median([r.tokens_per_second for r in self.answered if r.tokens_per_second])

    @property
    def cost(self) -> float:
        return sum(r.cost or 0.0 for r in self.results)

    @property
    def billed(self) -> bool | None:
        """Whether every bill was the host's own price (None: couldn't tell)."""
        found = [billed_by(r, self.candidate.host) for r in self.answered]
        told = [f for f in found if f is not None]
        if not told:
            return None
        return all(told)

    @property
    def ignores_level(self) -> bool:
        """Asked for a level, and reasoned at length anyway."""
        asked = self.asked.enabled or bool(self.asked.effort)
        return asked and (self.reasoning or 0) >= EXCESSIVE_REASONING


VerdictKind = Literal["failing", "overthinks", "slow", "model", "prompt", "nothing"]


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    text: str
    # The candidate to switch to, by key: the quickest that answered every
    # run and kept to the level asked.
    best: str | None = None


def _best(others: Sequence[HostSummary]) -> HostSummary | None:
    usable = [
        s
        for s in others
        if s.answered and not s.errors and not s.ignores_level and s.first_word is not None
    ]
    return min(usable, key=lambda s: (s.first_word, s.reasoning or 0.0), default=None)


def verdict(summaries: Sequence[HostSummary]) -> Verdict:
    """What the runs show about the current route (the summary whose
    candidate is current) against the rest."""
    current = next((s for s in summaries if s.candidate.current), None)
    others = [s for s in summaries if not s.candidate.current and s.answered]
    if current is None or not others:
        return Verdict("nothing", "No other host answered, so there is nothing to compare with.")
    best = _best(others)
    best_key = best.candidate.key if best is not None else None
    better = f" {best.candidate.label} did best." if best is not None else ""
    if not current.answered:
        why = current.errors[0].splitlines()[0] if current.errors else "no answer"
        return Verdict("failing", f"Your current route failed every run ({why}).{better}", best_key)
    their_words = _median([s.first_word for s in others if s.first_word is not None]) or 0.0
    their_reasoning = _median([s.reasoning or 0.0 for s in others]) or 0.0
    mine_words = current.first_word or 0.0
    mine_reasoning = current.reasoning or 0.0
    if mine_reasoning >= max(OVERTHINK_RATIO * their_reasoning, OVERTHINK_MIN):
        return Verdict(
            "overthinks",
            f"Your current route reasons far more than the other hosts (about "
            f"{mine_reasoning:,.0f} tokens against {their_reasoning:,.0f}): the host is the "
            f"problem, not the model.{better}",
            best_key,
        )
    if mine_words >= max(SLOW_RATIO * their_words, their_words + SLOW_MIN_EXTRA):
        return Verdict(
            "slow",
            f"Your current route reasons about as much as the others but is slow to start "
            f"({mine_words:.1f}s to the first word against {their_words:.1f}s): the host is "
            f"slow or overloaded.{better}",
            best_key,
        )
    if their_reasoning >= EXCESSIVE_REASONING:
        return Verdict(
            "model",
            f"Every host reasons at length on this prompt (about {their_reasoning:,.0f} tokens): "
            "that is the model, not the host. A lower reasoning level in Settings → Generation "
            "may help, if one is on offer.",
        )
    return Verdict(
        "prompt",
        "Your current route is about as quick as the other hosts on this prompt. If turns are "
        "still slow, it is likely your story's prompt: test with it.",
        best_key if best is not None and (best.first_word or 0) < mine_words * 0.7 else None,
    )
