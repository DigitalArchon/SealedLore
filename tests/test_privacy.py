"""Privacy as a property of the endpoint (providers/privacy.py).

NanoGPT's `private/` and `TEE/` models read as they always did; an endpoint
that speaks Tinfoil's or Chutes' end-to-end encryption makes every model on
it sealed, and until their clients exist nothing is sent to one. When in
doubt, sealed: a plain client never reaches a sealed host, and embeddings,
pictures and videos never inherit one's address or key.
"""

from __future__ import annotations

import httpx
import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    ProviderConfig,
    chat_api,
    sealed_service,
)
from sealedlore.providers import make_chat_provider
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import ChatRequest, ProviderError
from sealedlore.providers.decisions import DecisionsClient
from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
from sealedlore.providers.endpoints import EndpointsProvider
from sealedlore.providers.media_http import MediaHttp
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.privacy import (
    CHUTES,
    NANOGPT_PRIVATE,
    NANOGPT_TEE,
    TINFOIL,
    NotBuiltProvider,
    Privacy,
    move_refusal,
    privacy_at,
    privacy_for,
    privacy_of,
)
from sealedlore.providers.private_mode import PrivateModeProvider

NANO = "https://nano-gpt.com/api/v1"
TINFOIL_URL = "https://inference.tinfoil.sh/v1"
CHUTES_URL = "https://api.chutes.ai"


def endpoints(*extra: ProviderConfig, default: str = "Nano") -> Config:
    nano = ProviderConfig(name="Nano", base_url=NANO, api_key="nano-key", model="z-ai/glm-5.3")
    return Config(providers=[nano, *extra], active_provider_name=default)


def tinfoil(**changes) -> ProviderConfig:
    return ProviderConfig(name="Tinfoil", base_url=TINFOIL_URL, api_key="tf-key", **changes)


def chutes(**changes) -> ProviderConfig:
    return ProviderConfig(name="Chutes", base_url=CHUTES_URL, api_key="ch-key", **changes)


def recording() -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that answers nothing and keeps every request it was asked."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def request(model: str) -> ChatRequest:
    message = PromptMessage(role="user", parts=(ContentPart(text="secret"),))
    return ChatRequest(model=model, messages=[message])


# --- what each model is ---------------------------------------------------------------


def test_nanogpts_models_read_as_they_always_did():
    cfg = endpoints()
    assert privacy_of("private/glm-5-3", cfg) == Privacy("sealed", NANOGPT_PRIVATE)
    assert privacy_of("TEE/glm-5.3", cfg) == Privacy("tee", NANOGPT_TEE)
    assert privacy_of("z-ai/glm-5.3", cfg) == Privacy()
    assert privacy_of("Nano::Private/glm-5-3", cfg).sealed, "capitals never make it less private"
    assert not privacy_of("TEE Lab::anthropic/claude", cfg).private, "a name isn't an id"


def test_every_model_on_a_sealed_endpoint_is_sealed():
    cfg = endpoints(tinfoil(), chutes())
    assert privacy_of("Tinfoil::glm-5-3", cfg) == Privacy("sealed", TINFOIL)
    assert privacy_of("Chutes::moonshotai/Kimi-K3-TEE", cfg) == Privacy("sealed", CHUTES)
    # The endpoint decides, whatever the id says.
    assert privacy_of("Tinfoil::TEE/glm-5.3", cfg) == Privacy("sealed", TINFOIL)
    assert privacy_of("Tinfoil::private/glm-5-3", cfg) == Privacy("sealed", TINFOIL)
    # A sealed default endpoint: bare ids are its.
    sealed_default = endpoints(tinfoil(), default="Tinfoil")
    assert privacy_of("glm-5-3", sealed_default).service == TINFOIL


@pytest.mark.parametrize(
    "url, api, service",
    [
        (TINFOIL_URL, "auto", TINFOIL),
        ("https://router-0.tinfoil.sh/v1", "auto", TINFOIL),
        ("https://INFERENCE.tinfoil.sh./v1", "auto", TINFOIL),
        (TINFOIL_URL, "openai", TINFOIL),  # set to plain, still Tinfoil's host
        (TINFOIL_URL, "anthropic", TINFOIL),
        ("https://api.chutes.ai", "auto", CHUTES),
        ("https://llm.chutes.ai/v1", "auto", CHUTES),
        ("https://api.chutes.dev", "openai", CHUTES),
        ("https://proxy.example/v1", "tinfoil", TINFOIL),  # set to Tinfoil's protocol
        ("https://proxy.example/v1", "chutes-e2ee", CHUTES),
    ],
)
def test_when_in_doubt_sealed(url, api, service):
    """By the endpoint's `api`, else by its host whatever `api` says."""
    assert privacy_at("any/model", url, api) == Privacy("sealed", service)


def test_hosts_that_only_look_alike_are_not_sealed():
    for url in ("https://tinfoil.sh.example.com/v1", "https://nottinfoil.sh/v1", NANO):
        assert sealed_service(url) == "", url
        assert not privacy_at("any/model", url).private


def test_the_hosts_are_read_as_their_protocol_from_the_address():
    assert chat_api(TINFOIL_URL) == "tinfoil"
    assert chat_api("https://llm.chutes.ai/v1") == "chutes-e2ee"
    assert chat_api(NANO) == "openai" and chat_api("https://api.anthropic.com") == "anthropic"
    assert tinfoil().speaks() == "tinfoil" and chutes().speaks() == "chutes-e2ee"
    assert chat_api(TINFOIL_URL, "openai") == "openai", "an explicit choice is kept…"
    assert tinfoil(api="openai").sealed_service() == "tinfoil", "…and still sealed"


def test_a_private_model_off_nanogpt_is_still_nanogpts_and_refused_by_its_client():
    """As always: a `private/` model on another endpoint gets the Private Mode
    client, which sends nothing anywhere but NanoGPT."""
    other = ProviderConfig(name="OR", base_url="https://openrouter.ai/api/v1", model="private/x")
    assert privacy_for("private/x", other) == Privacy("sealed", NANOGPT_PRIVATE)
    provider = make_chat_provider(other)
    assert isinstance(provider, PrivateModeProvider)
    with pytest.raises(ProviderError, match="only through NanoGPT"):
        provider.attest()


def test_an_endpoint_that_isnt_set_up_is_judged_by_the_id():
    cfg = endpoints()
    assert privacy_of("Gone::private/glm-5-3", cfg).sealed
    assert privacy_of("Gone::glm-5-3", cfg) == Privacy()
    assert privacy_for("TEE/glm-5.3", None).kind == "tee"


# --- what may move where (D8) ---------------------------------------------------------


def test_a_sealed_chat_moves_to_any_sealed_model_and_a_tee_chat_up_to_one():
    cfg = endpoints(tinfoil())

    def judge(ref: str) -> Privacy:
        return privacy_of(ref, cfg)

    assert move_refusal("private/glm-5-3", "Tinfoil::glm-5-3", privacy=judge) is None
    assert move_refusal("Tinfoil::glm-5-3", "private/kimi-k3", privacy=judge) is None
    refusal = move_refusal("Tinfoil::glm-5-3", "TEE/glm-5.3", privacy=judge)
    assert "isn't end-to-end encrypted" in refusal and "another end-to-end encrypted" in refusal
    assert move_refusal("TEE/glm-5.3", "Tinfoil::glm-5-3", privacy=judge) is None
    assert "TEE" in move_refusal("TEE/glm-5.3", "z-ai/glm-5.3", privacy=judge)
    assert move_refusal("z-ai/glm-5.3", "Tinfoil::glm-5-3", privacy=judge) is None
    # NanoGPT's words are as they were.
    assert move_refusal("private/glm-5-3", "TEE/glm-5.3", privacy=judge) == (
        "TEE/glm-5.3 isn't end-to-end encrypted, and this chat is: choose a private/ "
        "model, or start a new chat."
    )


# --- nothing in the clear to a sealed endpoint ----------------------------------------


def test_a_sealed_endpoint_without_its_client_sends_nothing_and_says_what_to_do():
    provider = make_chat_provider(tinfoil(model="glm-5-3"))
    assert isinstance(provider, NotBuiltProvider)
    with pytest.raises(ProviderError) as refused:
        list(provider.stream(request("glm-5-3")))
    words = str(refused.value)
    assert "isn't built into this version" in words and "nothing was sent" in words
    assert "private/" in words and "Settings" in words, "a way out, not a dead end"
    with pytest.raises(ProviderError, match="isn't built"):
        provider.fetch_model_prices()
    chutes_provider = make_chat_provider(chutes(model="moonshotai/Kimi-K3-TEE"))
    assert isinstance(chutes_provider, NotBuiltProvider)
    assert "Chutes E2EE" in chutes_provider.reason


def test_the_routing_provider_gives_a_sealed_endpoint_no_plain_client():
    cfg = endpoints(tinfoil())
    routing = EndpointsProvider(cfg)
    assert isinstance(routing.provider_for("Tinfoil::glm-5-3"), NotBuiltProvider)
    assert isinstance(routing._plain(tinfoil()), NotBuiltProvider), "nor for its listing"
    with pytest.raises(ProviderError, match="isn't built"):
        list(routing.stream(request("Tinfoil::glm-5-3")))
    routing.close()


@pytest.mark.parametrize("url", [CHUTES_URL, "https://llm.chutes.ai/v1", TINFOIL_URL])
def test_a_plain_client_aimed_at_a_sealed_host_refuses_before_sending(url):
    """Judged by the host, not the model: even a plain model id, even set to
    speak OpenAI's protocol, and a listing too (it carries the key)."""
    client, sent = recording()
    settings = ProviderConfig(name="x", base_url=url, api_key="k", api="openai")
    provider = OpenAICompatibleProvider(settings, client=client)
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        list(provider.stream(request("z-ai/glm-5.3")))
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        provider.fetch_model_prices()
    anthropic = AnthropicProvider(settings.model_copy(update={"api": "anthropic"}), client=client)
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        list(anthropic.stream(request("claude-sonnet-4-6")))
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        anthropic.fetch_model_prices()
    assert sent == [], "nothing reached the host"


def test_a_plain_client_set_to_a_sealed_protocol_refuses_anywhere():
    client, sent = recording()
    settings = ProviderConfig(name="x", base_url="https://proxy.example/v1", api="tinfoil")
    with pytest.raises(ProviderError, match="end-to-end encrypted"):
        list(OpenAICompatibleProvider(settings, client=client).stream(request("glm-5-3")))
    assert sent == []


def test_a_lore_embedding_never_goes_to_tinfoils_embeddings():
    """Not inherited from a sealed chat endpoint, nor sent there when named."""
    cfg = endpoints(tinfoil(), default="Tinfoil")
    cfg.embedding_provider = EmbeddingProviderConfig(model="BAAI/bge-m3")
    assert cfg.embeddings() is None, "no address or key taken from Tinfoil: keywords alone"
    client, sent = recording()
    named = EmbeddingProviderConfig(base_url=TINFOIL_URL, api_key="k", model="nomic-embed-text")
    backend = OpenAICompatibleEmbeddings(named, client=client)
    with pytest.raises(ProviderError, match="the text to embed would go to it in the clear"):
        backend.embed(["The Hidden Caves lie under Hellsville."])
    assert sent == [], "inference.tinfoil.sh/v1/embeddings was never asked"
    # The same config on NanoGPT inherits as before.
    cfg.active_provider_name = "Nano"
    assert cfg.embeddings().base_url == NANO and cfg.embeddings().api_key == "nano-key"


def test_pictures_and_videos_never_inherit_a_sealed_endpoint_or_reach_one():
    cfg = endpoints(chutes(), default="Chutes")
    assert cfg.images() is None, "no address or key taken from Chutes"
    cfg.image_endpoint.base_url = NANO
    assert cfg.images().base_url == NANO and cfg.images().api_key == "", "Chutes' key stays"
    keys = {slot: url for slot, _holder, url in cfg.key_holders()}
    assert keys["images"] == NANO
    client, sent = recording()
    http = MediaHttp(TINFOIL_URL, "k", timeout_seconds=5, client=client)
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        http.request("POST", TINFOIL_URL + "/images/generations", json={"prompt": "a farmhouse"})
    assert sent == []


def test_jev_and_the_context_probe_send_nothing_to_a_sealed_host():
    from sealedlore.providers.context_probe import detect_context

    client, sent = recording()
    with pytest.raises(ProviderError, match="story's state"):
        DecisionsClient(TINFOIL_URL, "k", client=client).decide("jev", "state", {"q": "?"})
    assert detect_context(CHUTES_URL, "k", "m", client=client) is None
    assert sent == []
