"""Several chat endpoints, each with its own key, and a model reference that
names the one it is reached through ("Anthropic::claude-opus-5-5",
models/config.py; providers/endpoints.py routes by it). A bare id is the
default endpoint's, so every config and story from before works unchanged.
No network: mocks only."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sealedlore.cli import main as cli_main
from sealedlore.engine.catalog import chat_models, matches
from sealedlore.engine.routing import config_route, route_body
from sealedlore.engine.session import StorySession
from sealedlore.engine.speed_test import Role, plan_targets
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import price_of
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import (
    Config,
    ModelPrice,
    ProviderConfig,
    model_label,
    qualify,
    split_model,
)
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.route import ModelRoute
from sealedlore.models.story import Story
from sealedlore.providers import make_session_provider
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCancelled, TextDelta
from sealedlore.providers.endpoints import EndpointsProvider, listing_key
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.private_catalog import (
    encrypted_counterpart,
    privacy_label,
    private_alternatives,
    tee_counterpart,
)
from sealedlore.providers.tee import is_private_mode, is_tee, move_refusal
from sealedlore.providers.wire import should_use_cache_control
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle

NANO = "https://nano-gpt.com/api/v1"
ANTHROPIC = "https://api.anthropic.com"


def two_endpoints(**changes) -> Config:
    return Config(
        providers=[
            ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="z-ai/glm-5.3"),
            ProviderConfig(name="Anthropic", base_url=ANTHROPIC, api_key="ant-key"),
        ],
        active_provider_name="NanoGPT",
        **changes,
    )


def request(model: str) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[PromptMessage(role="user", parts=(ContentPart(text="Hello."),))],
        params=GenerationParams(reasoning=ReasoningConfig()),
    )


# --- the reference --------------------------------------------------------------------


def test_a_reference_splits_into_its_endpoint_and_its_bare_id():
    assert split_model("anthropic/claude-sonnet-4.6") == (None, "anthropic/claude-sonnet-4.6")
    assert split_model("Anthropic::claude-opus-5-5") == ("Anthropic", "claude-opus-5-5")
    # Endpoint names hold spaces; ids hold "/", "." and one ":".
    assert split_model("My Nano::anthropic/claude-sonnet-4.6:thinking") == (
        "My Nano",
        "anthropic/claude-sonnet-4.6:thinking",
    )
    assert split_model("::bare") == (None, "bare")


def test_qualifying_names_the_endpoint_unless_it_is_the_default():
    assert qualify("Anthropic", "claude-opus-5-5", "NanoGPT") == "Anthropic::claude-opus-5-5"
    assert qualify("NanoGPT", "z-ai/glm-5.3", "NanoGPT") == "z-ai/glm-5.3"
    assert qualify(None, "z-ai/glm-5.3", "NanoGPT") == "z-ai/glm-5.3"
    assert qualify("Anthropic", "", "NanoGPT") == "", "a blank field stays blank"
    assert model_label("Anthropic::claude-opus-5-5") == "claude-opus-5-5 · Anthropic"
    assert model_label("z-ai/glm-5.3") == "z-ai/glm-5.3"


def test_the_config_resolves_a_reference_to_its_endpoint():
    cfg = two_endpoints()
    assert cfg.endpoint_for("z-ai/glm-5.3").name == "NanoGPT", "a bare id is the default's"
    assert cfg.endpoint_for("Anthropic::claude-opus-5-5").name == "Anthropic"
    assert cfg.endpoint_for("Gone::x") is None, "never the default in a missing one's place"
    assert cfg.bare_model("Anthropic::claude-opus-5-5") == "claude-opus-5-5"
    assert cfg.qualify("Anthropic", "claude-opus-5-5") == "Anthropic::claude-opus-5-5"
    assert cfg.qualify("NanoGPT", "z-ai/glm-5.3") == "z-ai/glm-5.3"
    assert Config().endpoint_for("z-ai/glm-5.3") is None


def test_the_privacy_of_a_model_is_read_from_its_bare_id():
    assert is_tee("NanoGPT::TEE/glm-5.3") and is_private_mode("NanoGPT::private/glm-5-3")
    assert not is_tee("TEE Lab::anthropic/claude")  # the endpoint's name isn't the id
    assert privacy_label("Nano::private/glm-5-3")[0].startswith("🔐")
    assert move_refusal("Nano::private/glm-5-3", "Nano::TEE/glm-5.3") is not None
    assert should_use_cache_control(
        "Anthropic::claude-opus-5-5", mode="auto", patterns=["anthropic/", "claude-"]
    )
    assert not should_use_cache_control("Anthropic::gpt-x", mode="auto", patterns=["anthropic/"])


def test_a_models_twins_are_on_its_own_endpoint():
    ids = ["TEE/glm-5.3", "private/glm-5-3", "Nano2::TEE/glm-5.3", "Nano2::private/glm-5-3"]
    assert encrypted_counterpart("TEE/glm-5.3", ids) == "private/glm-5-3"
    assert encrypted_counterpart("Nano2::TEE/glm-5.3", ids) == "Nano2::private/glm-5-3"
    assert tee_counterpart("Nano2::private/glm-5-3", ids) == "Nano2::TEE/glm-5.3"
    assert private_alternatives("Nano2::TEE/glm-5.3", ids) == ["Nano2::private/glm-5-3"]


# --- the routing provider -------------------------------------------------------------


class Made:
    """Hands out a mock per endpoint and records what it was asked for."""

    def __init__(self) -> None:
        self.mocks: dict[str, MockChatProvider] = {}
        self.asked: list[ProviderConfig] = []

    def __call__(self, settings: ProviderConfig) -> MockChatProvider:
        self.asked.append(settings)
        return self.mocks.setdefault(settings.name, MockChatProvider([f"From {settings.name}."]))


def test_each_request_goes_to_its_models_endpoint_with_the_bare_id():
    made = Made()
    provider = EndpointsProvider(two_endpoints(), make=made)
    text, _done = provider.complete(request("Anthropic::claude-opus-5-5"))
    assert text == "From Anthropic."
    assert made.mocks["Anthropic"].last_request.model == "claude-opus-5-5"
    assert made.asked[-1].api_key == "ant-key" and made.asked[-1].model == "claude-opus-5-5"
    text, _done = provider.complete(request("z-ai/glm-5.3"))
    assert text == "From NanoGPT." and made.mocks["NanoGPT"].last_request.model == "z-ai/glm-5.3"
    assert set(made.mocks) == {"NanoGPT", "Anthropic"}
    # Streamed and built the same way; a client is made once per endpoint.
    events = list(provider.stream(request("Anthropic::claude-opus-5-5")))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "From Anthropic."
    assert provider.build_payload(request("Anthropic::claude-opus-5-5"))["model"] == (
        "claude-opus-5-5"
    )
    assert len(made.asked) == 2


def test_a_private_model_gets_its_endpoints_sealing_client_apart_from_the_plain_one():
    made = Made()
    provider = EndpointsProvider(two_endpoints(), make=made)
    provider.provider_for("z-ai/glm-5.3")
    provider.provider_for("private/glm-5-3")
    assert [s.model for s in made.asked] == ["z-ai/glm-5.3", "private/glm-5-3"]
    assert sorted(provider._providers) == [("NanoGPT", False), ("NanoGPT", True)]
    # The same kind again is the same client.
    provider.provider_for("private/kimi-k3")
    assert len(made.asked) == 2


def test_an_endpoint_that_isnt_set_up_is_refused_by_name():
    provider = EndpointsProvider(two_endpoints(), make=Made())
    with pytest.raises(ProviderError, match="'Gone'"):
        list(provider.stream(request("Gone::x")))
    with pytest.raises(ProviderError, match="Endpoints"):
        provider.complete(request("Gone::x"))
    assert provider.endpoint_for("Gone::x") is None
    nothing = EndpointsProvider(Config(), make=Made())
    with pytest.raises(ProviderError, match="No endpoint"):
        nothing.complete(request("z-ai/glm-5.3"))


def test_cancel_reset_and_close_reach_every_client_made():
    made = Made()
    provider = EndpointsProvider(two_endpoints(), make=made)
    provider.provider_for("z-ai/glm-5.3")
    provider.cancel()
    with pytest.raises(StreamCancelled):
        provider.complete(request("z-ai/glm-5.3"))
    # Nothing is opened for a stopped job; a client made after the Stop
    # (a side call's first use of an endpoint) is stopped too.
    with pytest.raises(StreamCancelled):
        provider.complete(request("Anthropic::claude-opus-5-5"))
    assert "Anthropic" not in made.mocks
    provider.provider_for("Anthropic::claude-opus-5-5")
    assert made.mocks["Anthropic"]._cancelled
    provider.reset_cancel()
    assert not made.mocks["NanoGPT"]._cancelled and not made.mocks["Anthropic"]._cancelled
    assert provider.complete(request("z-ai/glm-5.3"))[0] == "From NanoGPT."
    closed: list[str] = []
    for name, mock in made.mocks.items():
        mock.close = lambda name=name: closed.append(name)  # type: ignore[attr-defined]
    provider.close()
    assert sorted(closed) == ["Anthropic", "NanoGPT"] and provider._providers == {}


def test_a_sibling_and_a_detached_copy_are_made_from_each_endpoints_own():
    class WithTwins(MockChatProvider):
        def __init__(self, name: str) -> None:
            super().__init__([f"From {name}."])
            self.name = name
            self.twins: list[str] = []

        def sibling(self):
            self.twins.append("sibling")
            return WithTwins(self.name + " sibling")

        def detached(self):
            self.twins.append("detached")
            return WithTwins(self.name + " detached")

    bases: dict[str, WithTwins] = {}

    def make(settings: ProviderConfig) -> WithTwins:
        return bases.setdefault(settings.name, WithTwins(settings.name))

    provider = EndpointsProvider(two_endpoints(), make=make)
    sibling = provider.sibling()
    assert provider.sibling() is sibling, "built once"
    assert bases == {}, "nothing opened until an endpoint is used"
    assert sibling.complete(request("Anthropic::claude-opus-5-5"))[0] == "From Anthropic sibling."
    assert bases["Anthropic"].twins == ["sibling"] and "NanoGPT" not in bases
    detached = provider.detached()
    assert detached.complete(request("z-ai/glm-5.3"))[0] == "From NanoGPT detached."
    assert bases["NanoGPT"].twins == ["detached"]
    # A mock with no sibling of its own serves both.
    plain = EndpointsProvider(two_endpoints(), make=Made())
    assert plain.sibling().complete(request("z-ai/glm-5.3"))[0] == "From NanoGPT."


def test_the_watches_see_the_qualified_reference_not_the_bare_id():
    seen: list[str] = []
    provider = EndpointsProvider(two_endpoints(), make=Made())
    provider.route_watch = lambda req, _done: seen.append(req.model)
    provider.reasoning_watch = lambda req, _done: seen.append("reasoned " + req.model)
    routed = replace(request("Anthropic::claude-opus-5-5"), extra_body={"provider": {"sort": "x"}})
    provider.complete(routed)
    assert seen == ["Anthropic::claude-opus-5-5"]
    seen.clear()
    thinker = EndpointsProvider(
        two_endpoints(),
        make=lambda settings: MockChatProvider(["Hm."], reasoning="x" * 4000),
    )
    thinker.reasoning_watch = lambda req, _done: seen.append(req.model)
    thinker.complete(request("Anthropic::claude-opus-5-5"))
    thinker.complete(request("z-ai/glm-5.3"))
    assert seen == ["Anthropic::claude-opus-5-5", "z-ai/glm-5.3"]


class Listing(MockChatProvider):
    def __init__(
        self, listing: dict | None, defaults: dict | None = None, *, fail: str = ""
    ) -> None:
        super().__init__()
        self._listing, self._defaults, self._fail = listing, defaults, fail

    def fetch_model_prices(self):
        if self._fail:
            raise ProviderError(self._fail)
        return dict(self._listing or {})

    def fetch_reasoning_defaults(self):
        if self._fail:
            raise ProviderError(self._fail)
        return self._defaults


def test_the_listing_is_every_endpoints_in_one_keyed_by_reference():
    clients = {
        "NanoGPT": Listing(
            {"z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "pricing": {"prompt": 1, "completion": 2}}},
            {"z-ai/glm-5.3": {"reasoning_effort": "low"}},
        ),
        "Anthropic": Listing({"claude-opus-5-5": {"id": "claude-opus-5-5"}}, None),
    }
    provider = EndpointsProvider(two_endpoints(), make=lambda s: clients[s.name])
    listing = provider.fetch_model_prices()
    assert set(listing) == {"z-ai/glm-5.3", "Anthropic::claude-opus-5-5"}
    assert listing["z-ai/glm-5.3"]["endpoint"] == "NanoGPT"
    assert listing["z-ai/glm-5.3"]["endpoint_api"] == "openai"
    entry = listing["Anthropic::claude-opus-5-5"]
    assert entry["id"] == "Anthropic::claude-opus-5-5"
    assert entry["endpoint"] == "Anthropic" and entry["endpoint_api"] == "anthropic"
    assert provider.listing_problems == {}
    assert provider.fetch_reasoning_defaults() == {"z-ai/glm-5.3": {"reasoning_effort": "low"}}
    # The catalog carries the endpoint, and a search finds it by name.
    models = chat_models(listing)
    assert models["Anthropic::claude-opus-5-5"].endpoint == "Anthropic"
    assert matches(models["Anthropic::claude-opus-5-5"], "anthropic opus")
    assert not matches(models["z-ai/glm-5.3"], "anthropic")


def test_an_endpoint_whose_listing_fails_is_skipped_and_noted():
    clients = {
        "NanoGPT": Listing({"z-ai/glm-5.3": {"id": "z-ai/glm-5.3"}}),
        "Anthropic": Listing(None, fail="HTTP 401"),
    }
    provider = EndpointsProvider(two_endpoints(), make=lambda s: clients[s.name])
    assert set(provider.fetch_model_prices()) == {"z-ai/glm-5.3"}
    assert provider.listing_problems == {"Anthropic": "HTTP 401"}
    assert provider.fetch_reasoning_defaults() is None, "no endpoint stated any"
    # Every endpoint failing is a failure.
    broken = EndpointsProvider(two_endpoints(), make=lambda s: Listing(None, fail=f"{s.name} down"))
    with pytest.raises(ProviderError, match="NanoGPT: NanoGPT down; Anthropic: Anthropic down"):
        broken.fetch_model_prices()


def test_the_session_provider_is_built_from_the_config_and_its_catalog_key_names_every_endpoint():
    cfg = two_endpoints()
    provider = make_session_provider(cfg)
    assert isinstance(provider, EndpointsProvider) and provider.config is cfg
    key = listing_key(cfg)
    assert key.startswith("NanoGPT@") and "Anthropic=https://api.anthropic.com" in key
    cfg.active_provider_name = "Anthropic"
    assert listing_key(cfg) != key, "which is the default is part of it"


# --- routes, prices, speed ------------------------------------------------------------


def test_a_route_applies_only_on_a_nanogpt_endpoint(tmp_path: Path, story: Story, cast):
    cfg = two_endpoints(
        scene_model="Anthropic::claude-sonnet-4-6",
        model_routes={
            "story": ModelRoute(priority="latency"),
            "scene": ModelRoute(priority="price"),
        },
        min_cacheable_tokens=1,
    )
    assert config_route(cfg, "story", "z-ai/glm-5.3") != {}
    assert config_route(cfg, "scene", "Anthropic::claude-sonnet-4-6") == {}
    assert config_route(cfg, "scene", "Gone::x") == {}
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    session = StorySession(
        StoryBundle(story=story, cast=cast),
        cfg,
        MockChatProvider(),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    assert session.route_for("story") == {
        "provider": {"sort": "latency", "min_quantization": "fp8"}
    }
    assert session.route_for("scene") == {}, "Anthropic has no hosts to choose"
    # A host is one model's, by bare id, whichever NanoGPT endpoint names it.
    host = ModelRoute(priority="host", host="parasail", host_model="z-ai/glm-5.3")
    assert route_body(host, "Nano2::z-ai/glm-5.3") == {
        "provider": {"order": ["parasail"], "min_quantization": "fp8"}
    }
    assert route_body(host, "Nano2::other") == {}


def test_a_logged_bare_id_finds_the_price_kept_by_its_reference():
    prices = {
        "z-ai/glm-5.3": ModelPrice(prompt=1, completion=2),
        "OpenRouter::anthropic/claude-sonnet-4.6": ModelPrice(prompt=3, completion=15),
    }
    assert price_of(prices, "z-ai/glm-5.3").prompt == 1
    assert price_of(prices, "anthropic/claude-sonnet-4.6").prompt == 3, "the log holds the bare id"
    assert price_of(prices, "OpenRouter::anthropic/claude-sonnet-4.6").prompt == 3
    assert price_of(prices, "nobody") is None


def test_the_speed_test_sends_each_model_to_its_own_endpoint():
    cfg = two_endpoints()
    nano, anthropic = cfg.providers
    targets = plan_targets(
        [
            Role("Story", "Anthropic::claude-opus-5-5", anthropic, kind="story"),
            Role("Scene", "z-ai/glm-5.3", nano, "Story"),
            Role("Plot", "", nano, "Scene"),
            Role("Lore", "Gone::x", None, "Scene"),
        ],
        reasoning_for=lambda _kind, model: ReasoningConfig(enabled="::" in model),
    )
    by_label = {t.label: t for t in targets}
    story = by_label["claude-opus-5-5 · Anthropic"]
    assert story.settings.name == "Anthropic" and story.settings.model == "claude-opus-5-5"
    assert story.ref == "Anthropic::claude-opus-5-5" and story.reference == story.ref
    assert story.reasoning.enabled, "the reasoning facts are looked up by the reference"
    assert by_label["z-ai/glm-5.3"].roles == ("Scene", "Plot")
    assert "Lore" not in {role for t in targets for role in t.roles}, "an unknown endpoint"


def test_the_cli_adds_a_second_endpoint_beside_the_default(tmp_path: Path, capsys):
    data = ["--data-dir", str(tmp_path)]
    cli_main([*data, "configure", "--api-key", "nano", "--key-storage", "file"])
    cli_main(
        [
            *data,
            "configure",
            "--endpoint-name",
            "Anthropic",
            "--base-url",
            ANTHROPIC,
            "--api-key",
            "ant",
            "--api",
            "anthropic",
        ]
    )
    out = capsys.readouterr().out
    assert "Anthropic::<model id>" in out
    cfg = load_config(root=tmp_path)
    assert [p.name for p in cfg.providers] == ["default", "Anthropic"]
    assert cfg.active_provider_name == "default", "a second endpoint isn't made the default"
    assert cfg.endpoint_for("Anthropic::claude-opus-5-5").api_key == "ant"
    assert cfg.endpoint_for("Anthropic::claude-opus-5-5").speaks() == "anthropic"
    cli_main([*data, "configure", "--endpoint-name", "Anthropic", "--make-default"])
    assert load_config(root=tmp_path).active_provider_name == "Anthropic"
    with pytest.raises(SystemExit, match="not both"):
        cli_main([*data, "configure", "--name", "x", "--endpoint-name", "y"])
    with pytest.raises(SystemExit, match="::"):
        cli_main([*data, "configure", "--endpoint-name", "a::b"])
