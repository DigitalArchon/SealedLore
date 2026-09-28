"""A role's route as the request field NanoGPT reads (models/route.py).

A route travels as the request's `provider` object (`ChatRequest.extra_body`):

- a priority is a `sort`: "latency" (first word), "speed" (NanoGPT's own
  mix of first word and rate), "price";
- a host is a preference (`order`), never a pin: a host that is down falls
  back to another, still held to the precision floor;
- "FP8 or better" is `min_quantization`.

Only on NanoGPT, and never for a TEE or end-to-end encrypted model, whose
protection is the enclave it was attested on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sealedlore.models.config import Config, on_nanogpt
from sealedlore.models.node import Usage
from sealedlore.models.route import ROUTE_ROLES, ModelRoute
from sealedlore.providers.tee import is_tee

PRIORITY_LABELS = {
    "subscription": "Subscription routing",
    "latency": "Fastest first word",
    "speed": "Fastest overall",
    "price": "Cheapest",
    "host": "A specific host",
}


def routable(base_url: str, model: str) -> bool:
    return bool(model.strip()) and on_nanogpt(base_url) and not is_tee(model.strip())


def route_body(route: ModelRoute | None, model: str) -> dict[str, Any]:
    """The request fields for `route` on `model`: none for the subscription's
    routing, or for a host chosen for another model."""
    if route is None or route.priority == "subscription":
        return {}
    provider: dict[str, Any] = {}
    if route.priority == "host":
        if not route.host or route.host_model != model:
            return {}
        provider["order"] = [route.host]
    else:
        provider["sort"] = route.priority
    if route.fp8:
        provider["min_quantization"] = "fp8"
    return {"provider": provider}


def describe(route: ModelRoute | None, *, host_name: str | None = None) -> str:
    """ "Fastest first word · FP8+ · paid", for a field's label."""
    if route is None or route.priority == "subscription":
        return PRIORITY_LABELS["subscription"]
    what = (
        host_name or route.host or "a host"
        if route.priority == "host"
        else PRIORITY_LABELS[route.priority]
    )
    return f"{what} · {'FP8+' if route.fp8 else 'any precision'} · paid"


def role_in_use(role: str, own: dict[str, str | None]) -> str:
    """The role whose model (and so route) a call uses: `role`, or the one
    it falls back to while its own model (`own`, by role) is blank."""
    while role != "story" and not own.get(role):
        role = ROUTE_ROLES[role] or "story"
    return role


def config_route(config: Config, role: str, model: str) -> dict[str, Any]:
    """`route_body` for a call made with no story open (a premise draft):
    the role's route, or its fallback's when its own model is blank."""
    provider = config.active_provider()
    if provider is None or not routable(provider.base_url, model):
        return {}
    own = {
        "summarisation": config.summarization_model,
        "scene": config.scene_model,
        "plot": config.plot_model,
        "lore": config.lore_model,
        "authoring": config.authoring_model,
        "image_prompt": config.image_prompt_model,
    }
    return route_body(config.model_routes.get(role_in_use(role, own)), model)


# --- was the route followed? ---------------------------------------------------------

# A reply billed this far from the preferred host's price came from another
# host. Generous: a host's listed price and what it bills differ a little.
PRICE_TOLERANCE = 0.2


@dataclass(frozen=True)
class HostPrice:
    """A host's price, USD per million tokens (providers/model_hosts.py)."""

    input: float
    output: float
    cache_read: float | None = None


@dataclass(frozen=True)
class RouteCheck:
    """What one routed reply's bill says about its route. NanoGPT names no
    host in a reply, so the cost is the evidence: nothing billed means the
    route was dropped for the subscription's routing (live: a host that was
    down); a bill at another price means another host. `met` is None when
    the bill can't tell."""

    model: str
    provider: dict[str, Any]
    met: bool | None
    reason: str
    cost: float | None


def expected_cost(usage: Usage, price: HostPrice) -> float:
    cached = min(usage.cache_read_tokens, usage.prompt_tokens)
    fresh = usage.prompt_tokens - cached
    cache_price = price.cache_read if price.cache_read is not None else price.input
    written = usage.completion_tokens * price.output
    return (fresh * price.input + cached * cache_price + written) / 1e6


def check_route(
    model: str, provider: dict[str, Any], usage: Usage, preferred: HostPrice | None = None
) -> RouteCheck:
    """`provider`: the route as sent; `preferred`: the preferred host's price,
    when the route names one and its price is known."""
    if not usage.cost_reported:
        return RouteCheck(model, provider, None, "the endpoint reported no cost", None)
    cost = usage.cost
    if cost == 0:
        return RouteCheck(
            model,
            provider,
            False,
            "NanoGPT billed nothing, so it served the call on its own subscription routing "
            "and not the route (the host may be down, or not serve this model now)",
            cost,
        )
    if preferred is None or not (usage.prompt_tokens or usage.completion_tokens):
        return RouteCheck(model, provider, True, "billed as a routed call", cost)
    expected = expected_cost(usage, preferred)
    if expected > 0 and abs(cost - expected) > PRICE_TOLERANCE * expected:
        return RouteCheck(
            model,
            provider,
            False,
            f"billed ${cost:.6f} where the preferred host would bill about ${expected:.6f}: "
            "another host served it",
            cost,
        )
    return RouteCheck(model, provider, True, "billed at the preferred host's price", cost)


def describe_sent(provider: dict[str, Any]) -> str:
    """The route as it was sent, in the words the route dialog uses."""
    if provider.get("order"):
        what = str(provider["order"][0])
    else:
        what = PRIORITY_LABELS.get(str(provider.get("sort")), str(provider.get("sort")))
    return f"{what} · {'FP8+' if provider.get('min_quantization') else 'any precision'}"
