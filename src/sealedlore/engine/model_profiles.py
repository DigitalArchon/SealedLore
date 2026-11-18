"""Model profiles: a named set of the text models, one for each job, with
their routes, kept to be put back with one choice (the author, Nov 18
2026: "a person may come up with favourite combos they want to save and
reuse for different story types").

Three come with the app and follow it from release to release, never stored:

- Default: what a new setup on NanoGPT starts with.
- Recommended: for each job, the model its Recommended ▾ list marks premium,
  else the one it recommends: the best measured, whatever it costs.
- Private: end-to-end encrypted models only, on NanoGPT's Private Mode or
  Tinfoil's own endpoint.

The author's own are `Config.model_profiles`. Pure: Settings → Models and
Setup → Models put them into their fields.

A profile holds each job's model as the Models page does, a blank one going
to the job it falls back to (`ROUTE_ROLES`). Settings puts it back as it is.
A story's own models (Setup) take every job filled in (`filled`): a blank
there would follow Settings, not the profile.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.models.config import (
    DEFAULT_STORY_MODEL,
    RECOMMENDED_MODELS,
    RECOMMENDED_TEXT_MODELS,
    ModelProfile,
    ProviderConfig,
    on_nanogpt,
    qualify,
    recommended_reference,
    sealed_service,
    split_model,
)
from sealedlore.models.route import ROUTE_ROLES, ModelRoute

# Every job a profile holds, in the Models page's order.
PROFILE_ROLES: tuple[str, ...] = tuple(ROUTE_ROLES)

DEFAULT = "Default"
RECOMMENDED = "Recommended"
PRIVATE = "Private"
BUILT_IN = (DEFAULT, RECOMMENDED, PRIVATE)

ABOUT = {
    DEFAULT: "What a new setup starts with: Claude Sonnet 4.6 writes, and small models "
    "included in a NanoGPT subscription do the reads after each passage.",
    RECOMMENDED: "The best measured for each job, whatever it costs: Claude Opus 5.5 writes, "
    "and every other job has the premium choice of its Recommended list. Several times the "
    "cost of Default.",
    PRIVATE: "End-to-end encrypted models only, for every job: GLM 5.3 writes and reads the "
    "scene, DeepSeek V4.1 Flash directs the plot and picks lore, on NanoGPT's Private Mode "
    "or Tinfoil's own endpoint.",
}

# Where a NanoGPT id's role is listed in RECOMMENDED_TEXT_MODELS.
_LISTED = {
    "story": "story",
    "scene": "scene_model",
    "plot": "plot_model",
    "lore": "lore_model",
    "record": "record_model",
}

# Private's models on each service, by job (read from both services' lists,
# Nov 18 2026: the same models, NanoGPT's under its Private Mode names). The
# rest of the jobs fall back to them.
NANOGPT_PRIVATE_MODELS = {
    "story": "private/glm-5-3",
    "scene": "private/glm-5-3",
    "plot": "private/deepseek-v4-1-flash",
    "lore": "private/deepseek-v4-1-flash",
}
TINFOIL_MODELS = {
    "story": "glm-5-3",
    "scene": "glm-5-3",
    "plot": "deepseek-v4-1-flash",
    "lore": "deepseek-v4-1-flash",
}

NO_NANOGPT = (
    "Its models are NanoGPT's: add a NanoGPT endpoint under Endpoints to use it here, or "
    "choose models with Choose."
)
NO_PRIVATE = (
    "Needs a NanoGPT endpoint (for its Private Mode) or a Tinfoil one: add one under Endpoints."
)


@dataclass(frozen=True)
class Profile:
    """A profile as the fields hold it, on the endpoints as they stand."""

    name: str
    # Each job's model as a field holds it ("" for blank).
    models: Mapping[str, str]
    routes: Mapping[str, ModelRoute] = field(default_factory=dict)
    built_in: bool = False
    about: str = ""
    # Why it can't be used on these endpoints, or "".
    problem: str = ""


def _premium(role: str) -> str:
    """The premium choice of a job's Recommended list, else the one it
    recommends (or its default)."""
    listed = RECOMMENDED_TEXT_MODELS[_LISTED[role]]
    for tags in (("premium", "best"), ("recommended", "default")):
        for choice in listed:
            if choice.tag in tags:
                return choice.id
    return listed[0].id


def default_ids() -> dict[str, str]:
    """Default's NanoGPT ids, by job."""
    return {
        "story": DEFAULT_STORY_MODEL,
        "scene": RECOMMENDED_MODELS["scene_model"],
        "plot": RECOMMENDED_MODELS["plot_model"],
        "lore": RECOMMENDED_MODELS["lore_model"],
    }


def recommended_ids() -> dict[str, str]:
    """Recommended's NanoGPT ids, by job; summarising, authoring and the
    picture prompts go with the storyteller."""
    return {role: _premium(role) for role in _LISTED}


def _from_nanogpt(
    name: str, ids: Mapping[str, str], providers: Sequence[ProviderConfig], default: str | None
) -> Profile:
    if not any(on_nanogpt(p.base_url) for p in providers):
        return Profile(name, _all(ids), built_in=True, about=ABOUT[name], problem=NO_NANOGPT)
    models = {role: recommended_reference(model, providers, default) for role, model in ids.items()}
    return Profile(name, _all(models), built_in=True, about=ABOUT[name])


def _private(providers: Sequence[ProviderConfig], default: str | None) -> Profile:
    def tinfoil(p: ProviderConfig) -> bool:
        return sealed_service(p.base_url, p.api) == "tinfoil"

    nano = next((p for p in providers if on_nanogpt(p.base_url)), None)
    tin = next((p for p in providers if tinfoil(p)), None)
    # The default endpoint's service when it is either, else NanoGPT's first.
    chosen = next(
        (p for p in providers if p.name == default and (on_nanogpt(p.base_url) or tinfoil(p))),
        None,
    )
    chosen = chosen or nano or tin
    if chosen is None:
        return Profile(
            PRIVATE,
            _all(NANOGPT_PRIVATE_MODELS),
            built_in=True,
            about=ABOUT[PRIVATE],
            problem=NO_PRIVATE,
        )
    ids = TINFOIL_MODELS if tinfoil(chosen) else NANOGPT_PRIVATE_MODELS
    models = {role: qualify(chosen.name, model, default) for role, model in ids.items()}
    return Profile(PRIVATE, _all(models), built_in=True, about=ABOUT[PRIVATE])


def _all(models: Mapping[str, str]) -> dict[str, str]:
    """Every job, blank where `models` has none."""
    return {role: models.get(role, "") for role in PROFILE_ROLES}


def built_in_profiles(providers: Sequence[ProviderConfig], default: str | None) -> list[Profile]:
    """Default, Recommended and Private on these endpoints (`default`: the
    default endpoint's name)."""
    return [
        _from_nanogpt(DEFAULT, default_ids(), providers, default),
        _from_nanogpt(RECOMMENDED, recommended_ids(), providers, default),
        _private(providers, default),
    ]


def from_saved(saved: ModelProfile, default: str | None) -> Profile:
    """One of the author's, its references as the fields hold them: bare
    for the default endpoint."""
    models = {}
    for role in PROFILE_ROLES:
        name, bare = split_model(saved.models.get(role, "").strip())
        models[role] = qualify(name, bare, default) if bare else ""
    routes = {role: saved.routes[role] for role in PROFILE_ROLES if role in saved.routes}
    return Profile(saved.name, models, routes)


def all_profiles(
    saved: Sequence[ModelProfile], providers: Sequence[ProviderConfig], default: str | None
) -> list[Profile]:
    """The three that come with the app, then the author's, as kept."""
    return built_in_profiles(providers, default) + [from_saved(p, default) for p in saved]


def to_save(
    name: str,
    models: Mapping[str, str],
    routes: Mapping[str, ModelRoute | None],
    default: str | None,
) -> ModelProfile:
    """The fields as they stand, kept as a profile: every reference names its
    endpoint, so making another endpoint the default doesn't change what it
    holds. A route goes only with a model of its own."""
    kept: dict[str, str] = {}
    kept_routes: dict[str, ModelRoute] = {}
    for role in PROFILE_ROLES:
        text = (models.get(role) or "").strip()
        if not text:
            continue
        endpoint, bare = split_model(text)
        endpoint = endpoint or default
        kept[role] = f"{endpoint}::{bare}" if endpoint else bare
        if (route := routes.get(role)) is not None:
            kept_routes[role] = route
    return ModelProfile(name=name.strip(), models=kept, routes=kept_routes)


def name_problem(name: str, saved: Sequence[ModelProfile]) -> str | None:
    """Why a profile can't be saved under `name`, or None. A name one of the
    author's has is theirs to replace (asked first)."""
    name = name.strip()
    if not name:
        return "Give the profile a name."
    if name.casefold() in {n.casefold() for n in BUILT_IN}:
        return f"{name} comes with SealedLore: choose another name."
    return None


def filled(profile: Profile) -> dict[str, tuple[str, ModelRoute | None]]:
    """Every job's model and route with nothing blank: a blank job takes the
    model (and route) of the job it falls back to, within the profile, as the
    app would. For a story's own models, where a blank follows Settings."""
    out: dict[str, tuple[str, ModelRoute | None]] = {}
    for role in PROFILE_ROLES:
        source = role
        while source != "story" and not profile.models.get(source):
            source = ROUTE_ROLES[source] or "story"
        out[role] = (profile.models.get(source, ""), profile.routes.get(source))
    return out


def matching(
    profiles: Sequence[Profile],
    models: Mapping[str, str],
    routes: Mapping[str, ModelRoute | None],
) -> Profile | None:
    """The profile the fields hold exactly, routes included, if any."""
    for profile in profiles:
        if profile.problem:
            continue
        if all(
            (models.get(role) or "").strip() == profile.models.get(role, "")
            and (routes.get(role) if (models.get(role) or "").strip() else None)
            == (profile.routes.get(role) if profile.models.get(role) else None)
            for role in PROFILE_ROLES
        ):
            return profile
    return None


def renamed(saved: Sequence[ModelProfile], old: str, new: str) -> list[ModelProfile]:
    """The author's profiles with references to endpoint `old` naming `new`."""
    out = []
    for profile in saved:
        models = {}
        for role, ref in profile.models.items():
            endpoint, bare = split_model(ref)
            models[role] = f"{new}::{bare}" if endpoint == old else ref
        out.append(profile.model_copy(update={"models": models}))
    return out
