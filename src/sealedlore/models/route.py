"""Which of NanoGPT's hosts serves a model: a route, per role.

NanoGPT runs an open model on whichever of several upstream hosts it
chooses; on the subscription, usually the cheapest at FP8 or better. A
route asks for another: the fastest to its first word, the fastest overall,
the cheapest, or one host in particular. Any route but the subscription's is
billed pay-as-you-go, even for a model the subscription includes.

Measured on the scene read (97 judged reads, GLM 5.3, Sept 2026): the fastest
first word at FP8 or better answered in a third of the time, as accurately;
without the floor it was slower and less accurate (an FP4 host, likely).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

RoutePriority = Literal["subscription", "latency", "speed", "price", "host"]

# What each role's route is keyed by in `Config.model_routes`, and the role
# whose route a blank one takes, as its model falls back (engine/session.py).
ROUTE_ROLES: dict[str, str | None] = {
    "story": None,
    "summarisation": "story",
    "authoring": "story",
    "scene": "summarisation",
    "plot": "scene",
    "lore": "scene",
    "image_prompt": "story",
}


class ModelRoute(BaseModel):
    priority: RoutePriority = "subscription"
    # Only hosts at FP8 or better (the default): FP4 hosts measured worse.
    fp8: bool = True
    # A host (NanoGPT's provider id), for priority "host": preferred, not
    # required, so a host that is down falls back to another.
    host: str | None = None
    # The model the host was chosen for: a host is one model's. For another
    # model the route is the subscription's.
    host_model: str | None = None

    @property
    def paid(self) -> bool:
        return self.priority != "subscription"
