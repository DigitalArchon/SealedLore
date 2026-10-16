"""One provider for the session that sends each request to its model's endpoint.

The author (Oct 2026): Anthropic doesn't serve most of the models SealedLore
uses, so there may be several endpoints with a key each, and a model names
the one it is reached through ("Anthropic::claude-opus-5-5"; a bare id is the
default endpoint's, models/config.py). The session keeps one `ChatProvider`
and every side call names its model in its `ChatRequest`, so this is the one
place that resolves: a real client per endpoint (`make_chat_provider`), made
on first use, given a copy of the request with the bare id. A `private/`
model gets its endpoint's sealing client, as it always did, because the
client is chosen by the model it is first asked for.

What the session hangs on a provider (`route_watch`, `reasoning_watch`) is
passed down to each client, wrapped so the session sees the qualified
reference and not the bare id the client sent: its facts and prices are
keyed by the reference (`Config.model_reasoning`, `Config.model_prices`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from sealedlore.models.config import Config, ProviderConfig, qualify, split_model
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
)
from sealedlore.providers.tee import is_private_mode

Watch = Callable[[ChatRequest, StreamCompleted], None] | None
# Makes the client for an endpoint, given its settings with the model the
# first request names (`make_chat_provider` chooses the client by it).
Make = Callable[[ProviderConfig], ChatProvider]
# A client is one endpoint's, and a sealing client (a `private/` model's)
# is kept apart from the plain one on the same endpoint.
Key = tuple[str, bool]


def listing_key(config: Config) -> str:
    """What a fetched listing belongs to: every endpoint's name and address
    and which is the default, so a catalog kept for an hour is refetched
    when any of them changes."""
    parts = [f"{p.name}={p.base_url.strip().rstrip('/')}|{p.api}" for p in config.providers]
    return f"{config.active_provider_name or ''}@" + ";".join(parts)


class EndpointsProvider(ChatProvider):
    def __init__(
        self,
        config: Config,
        *,
        make: Make | None = None,
        providers: dict[Key, ChatProvider] | None = None,
    ) -> None:
        """`config` is read live (its endpoints and default), so the clients
        follow a Settings change; one made for settings since changed is
        made again. `make` builds a client (tests give one that hands out
        mocks); `providers` are clients already made, by (endpoint, sealed)."""
        self.config = config
        self._make: Make = make or _make_chat_provider
        self._lock = threading.Lock()
        self._providers: dict[Key, ChatProvider] = dict(providers or {})
        # The settings each client was made for, to notice a change.
        self._made_for: dict[Key, ProviderConfig] = {}
        self._cancelled = False
        self._sibling: EndpointsProvider | None = None
        self._route_watch: Watch = None
        self._reasoning_watch: Watch = None
        # Why an endpoint's listing couldn't be fetched, by name, after a
        # fetch (`fetch_model_prices`): the others' models are still listed.
        self.listing_problems: dict[str, str] = {}

    # --- the watches the session hangs on its provider ----------------------------

    @property
    def route_watch(self) -> Watch:  # type: ignore[override]
        return self._route_watch

    @route_watch.setter
    def route_watch(self, watch: Watch) -> None:
        self._route_watch = watch
        with self._lock:
            for (name, _sealed), provider in self._providers.items():
                provider.route_watch = self._wrapped(name, watch)

    @property
    def reasoning_watch(self) -> Watch:  # type: ignore[override]
        return self._reasoning_watch

    @reasoning_watch.setter
    def reasoning_watch(self, watch: Watch) -> None:
        self._reasoning_watch = watch
        with self._lock:
            for (name, _sealed), provider in self._providers.items():
                provider.reasoning_watch = self._wrapped(name, watch)

    def _wrapped(self, endpoint: str, watch: Watch) -> Watch:
        """The watch as a client calls it, with its request's model put back
        into the reference the session sent."""
        if watch is None:
            return None

        def report(request: ChatRequest, completed: StreamCompleted) -> None:
            ref = qualify(endpoint, request.model, self.config.active_provider_name)
            watch(replace(request, model=ref), completed)

        return report

    # --- resolving ----------------------------------------------------------------

    def resolve(self, model: str) -> tuple[ProviderConfig, str]:
        """(the endpoint `model` is reached through, its bare id). Raises
        ProviderError for a reference naming an endpoint that isn't set up,
        or a bare id with no default endpoint."""
        name, bare = split_model(model)
        settings = self.config.endpoint_for(model)
        if settings is None:
            if name is None:
                raise ProviderError(
                    "No endpoint is set up: set a base URL, API key and model under "
                    "File → Settings → Endpoints."
                )
            raise ProviderError(
                f"{bare} is on the endpoint {name!r}, which isn't set up (File → Settings → "
                "Endpoints); nothing was sent"
            )
        return settings, bare

    def endpoint_for(self, model: str) -> ProviderConfig | None:
        """The endpoint a reference is reached through, or None (no error)."""
        return self.config.endpoint_for(model)

    def provider_for(self, model: str) -> ChatProvider:
        """The client `model`'s endpoint is reached through, made on first use."""
        settings, bare = self.resolve(model)
        return self._provider(settings.model_copy(update={"model": bare}))

    def _provider(self, settings: ProviderConfig) -> ChatProvider:
        """The client for `settings` (its model the one asked for now): the
        plain one, or the sealing one for a `private/` model."""
        key = (settings.name, is_private_mode(settings.model))
        with self._lock:
            made = self._providers.get(key)
            made_for = self._made_for.get(key)
            if made is not None and (made_for is None or _same_endpoint(made_for, settings)):
                return made
            if made is not None:
                _close(made)  # the endpoint's settings changed under it
            provider = self._make(settings)
            self._providers[key] = provider
            self._made_for[key] = settings
            provider.route_watch = self._wrapped(settings.name, self._route_watch)
            provider.reasoning_watch = self._wrapped(settings.name, self._reasoning_watch)
            if self._cancelled:
                provider.cancel()  # a Stop pressed before any client existed still holds
        return provider

    def _forwarded(self, request: ChatRequest) -> tuple[ChatProvider, ChatRequest]:
        provider = self.provider_for(request.model)
        return provider, replace(request, model=split_model(request.model)[1])

    # --- ChatProvider -------------------------------------------------------------

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        provider, inner = self._forwarded(request)
        return provider.build_payload(inner)

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        with self._lock:
            if self._cancelled:
                raise StreamCancelled()
        provider, inner = self._forwarded(request)
        yield from provider.stream(inner)

    def complete(self, request: ChatRequest) -> tuple[str, StreamCompleted]:
        with self._lock:
            if self._cancelled:
                raise StreamCancelled()
        provider, inner = self._forwarded(request)
        return provider.complete(inner)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            made = list(self._providers.values())
        for provider in made:
            provider.cancel()
        if self._sibling is not None:
            self._sibling.cancel()

    def reset_cancel(self) -> None:
        with self._lock:
            self._cancelled = False
            made = list(self._providers.values())
        for provider in made:
            provider.reset_cancel()
        if self._sibling is not None:
            self._sibling.reset_cancel()

    def close(self) -> None:
        with self._lock:
            made, self._providers, self._made_for = list(self._providers.values()), {}, {}
        for provider in made:
            _close(provider)
        if self._sibling is not None:
            self._sibling.close()
            self._sibling = None

    def sibling(self) -> EndpointsProvider:
        """A second provider for a call made alongside one of this one's:
        each endpoint's client is that client's own sibling, made when first
        used, so an endpoint never used opens nothing. Built once; a cancel
        here reaches it."""
        if self._sibling is None:
            self._sibling = EndpointsProvider(self.config, make=self._make_sibling)
        self._sibling.route_watch = self._route_watch
        self._sibling.reasoning_watch = self._reasoning_watch
        return self._sibling

    def detached(self) -> EndpointsProvider:
        """An independent provider for background work: each endpoint's
        client is a detached copy of this one's, made when first used. The
        caller closes it."""
        detached = EndpointsProvider(self.config, make=self._make_detached)
        detached.route_watch = self._route_watch
        detached.reasoning_watch = self._reasoning_watch
        return detached

    def _make_sibling(self, settings: ProviderConfig) -> ChatProvider:
        base = self._provider(settings)
        sibling = getattr(base, "sibling", None)
        # A client with no sibling of its own (the scripted mock) serves both.
        return sibling() if callable(sibling) else base

    def _make_detached(self, settings: ProviderConfig) -> ChatProvider:
        base = self._provider(settings)
        detached = getattr(base, "detached", None)
        return detached() if callable(detached) else base

    # --- the listings -------------------------------------------------------------

    def _plain(self, settings: ProviderConfig) -> ChatProvider:
        """An endpoint's plain client (never the sealing one): the listings
        are the endpoint's, whatever model is asked for."""
        return self._provider(settings.model_copy(update={"model": ""}))

    def _endpoints(self) -> list[ProviderConfig]:
        """Every endpoint, the default first."""
        default = self.config.active_provider()
        others = [p for p in self.config.providers if default is None or p is not default]
        return ([default] if default is not None else []) + others

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """Every endpoint's listing in one, each entry keyed by its reference
        (bare on the default endpoint) and carrying `endpoint` (the name) and
        `endpoint_api` (what it speaks). An endpoint whose listing fails is
        noted in `listing_problems` and the others' models are still listed;
        only every endpoint failing raises."""
        listing: dict[str, dict[str, Any]] = {}
        problems: dict[str, str] = {}
        endpoints = self._endpoints()
        for settings in endpoints:
            try:
                found = self._plain(settings).fetch_model_prices()
            except ProviderError as exc:
                problems[settings.name] = str(exc)
                continue
            for model_id, entry in found.items():
                ref = qualify(settings.name, model_id, self.config.active_provider_name)
                listing[ref] = {
                    **entry,
                    "id": ref,
                    "endpoint": settings.name,
                    "endpoint_api": settings.speaks(),
                }
        self.listing_problems = problems
        if not listing and problems:
            raise ProviderError(
                "; ".join(f"{name}: {why}" for name, why in problems.items())
                if len(problems) > 1
                else next(iter(problems.values()))
            )
        if not endpoints:
            raise ProviderError(
                "No endpoint is set up: set a base URL, API key and model under "
                "File → Settings → Endpoints."
            )
        return listing

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        """Every endpoint's defaults in one, keyed as the listing is; None
        when no endpoint states any. Only every endpoint failing raises."""
        defaults: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        said = False
        endpoints = self._endpoints()
        for settings in endpoints:
            try:
                found = self._plain(settings).fetch_reasoning_defaults()
            except ProviderError as exc:
                failures.append(f"{settings.name}: {exc}")
                continue
            if found is None:
                continue
            said = True
            for model_id, entry in found.items():
                defaults[qualify(settings.name, model_id, self.config.active_provider_name)] = entry
        if failures and len(failures) == len(endpoints):
            raise ProviderError("; ".join(failures))
        return defaults if said else None


def _same_endpoint(first: ProviderConfig, second: ProviderConfig) -> bool:
    """The settings a client was made for still stand: everything but the
    model, which is the request's."""
    return first.model_copy(update={"model": ""}) == second.model_copy(update={"model": ""})


def _close(provider: ChatProvider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _make_chat_provider(settings: ProviderConfig) -> ChatProvider:
    # Imported here: providers/__init__.py imports this module.
    from sealedlore.providers import make_chat_provider

    return make_chat_provider(settings)
