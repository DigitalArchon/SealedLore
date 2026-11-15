"""Chutes as an ordinary endpoint too, and the three privacy levels (the author,
Nov 15 2026).

A Chutes endpoint offers every model Chutes lists, like NanoGPT's: its -TEE
models (models/config.py `chutes_tee`) always go end-to-end encrypted through
Chutes' own client (providers/chutes_provider.py), never through a plain one,
whatever the endpoint speaks; the others through the endpoint's ordinary
client, OpenAI-compatible at llm.chutes.ai or Anthropic's at claude.chutes.ai.
The key goes to the endpoint's own address, and for the -TEE models to
api.chutes.ai, nowhere else. Privacy comes in three levels (highest, partial,
not private), and a chat or scene moves only to its own level or higher.
Offline throughout: a fake Chutes (tests/test_chutes_provider.py's, answering
the ordinary routes too).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.session import StorySession
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import (
    CHUTES_ANTHROPIC_URL,
    CHUTES_BASE_URL,
    Config,
    EmbeddingProviderConfig,
    ProviderConfig,
    chutes_tee,
    is_tee_id,
    on_chutes,
)
from sealedlore.models.story import Story
from sealedlore.providers import make_chat_provider
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import ChatRequest, ProviderError
from sealedlore.providers.chutes_provider import ChutesE2EEProvider, NotOffered
from sealedlore.providers.decisions import DecisionsClient
from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
from sealedlore.providers.endpoints import EndpointsProvider
from sealedlore.providers.media_http import MediaHttp
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.privacy import (
    CHUTES,
    CHUTES_NOTE,
    CHUTES_PLAIN_NOTE,
    E2EE_NOTE,
    HIGHEST,
    NANOGPT_PRIVATE,
    NANOGPT_TEE,
    NOT_PRIVATE,
    PARTIAL,
    PRIVACY_LEVELS,
    TEE_NOTE,
    TINFOIL,
    TINFOIL_NOTE,
    Privacy,
    holds,
    level_line,
    move_refusal,
    privacy_at,
    privacy_of,
    rank,
)
from sealedlore.storage.repository import StoryBundle
from tests.test_anthropic import reply as anthropic_reply
from tests.test_chutes_provider import (
    MODEL,
    UP_TO_DATE,
    FakeChutes,
)
from tests.test_providers import delta_chunk
from tests.test_providers import sse as openai_sse

DATA = Path(__file__).parent / "data"
NANO = "https://nano-gpt.com/api/v1"
TINFOIL_URL = "https://inference.tinfoil.sh/v1"
CHUTES_HOSTS = [
    CHUTES_BASE_URL,
    CHUTES_ANTHROPIC_URL,
    "https://api.chutes.ai",
    "https://x.chutes.dev",
]
TEE_IDS = [
    "Qwen/Qwen3.8-27B-TEE",
    "Qwen/Qwen3-32B-TEE:THINKING",
    "qwen/qwen3.8-27b-tee",
    " moonshotai/Kimi-K3-TEE ",
    "Chutes::zai-org/GLM-5.2-TEE",
]
ORDINARY = "zai-org/GLM-5.3"


def chutes(**changes) -> ProviderConfig:
    fields = {"name": "Chutes", "base_url": CHUTES_BASE_URL, "api_key": "cpk_test", **changes}
    return ProviderConfig(**fields)


def config(*extra: ProviderConfig) -> Config:
    nano = ProviderConfig(name="Nano", base_url=NANO, api_key="nano-key")
    tinfoil = ProviderConfig(name="Tinfoil", base_url=TINFOIL_URL, api_key="tf-key")
    return Config(providers=[nano, tinfoil, *extra], active_provider_name="Nano")


def ask(model: str) -> ChatRequest:
    message = PromptMessage(role="user", parts=(ContentPart(text="Open the door."),))
    return ChatRequest(model=model, messages=[message])


def recording() -> tuple[httpx.Client, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


class Mixed(FakeChutes):
    """FakeChutes, answering Chutes' ordinary routes too: llm.chutes.ai's
    /v1/chat/completions and claude.chutes.ai's /v1/messages, streamed."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "llm.chutes.ai" and url.path == "/v1/chat/completions":
            self.requests.append(request)
            body = openai_sse(
                delta_chunk("The door gives."),
                {"choices": [{"finish_reason": "stop", "delta": {}}]},
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        if url.host == "claude.chutes.ai" and url.path == "/v1/messages":
            self.requests.append(request)
            return httpx.Response(
                200, content=anthropic_reply(), headers={"content-type": "text/event-stream"}
            )
        return super().handler(request)

    @staticmethod
    def models() -> dict:
        listed = FakeChutes.models()
        listed["data"].append({"id": ORDINARY, "chute_id": "1ecb73cc-909b-5334-9cd8-74985945cff5"})
        return listed


def routing(fake: Mixed, *endpoints: ProviderConfig) -> EndpointsProvider:
    """The session's routing provider, the factory choosing every client
    (`make_chat_provider`), each then given the fake's transport."""
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))

    def make(settings: ProviderConfig):
        chosen = make_chat_provider(settings)
        chosen.close()
        if isinstance(chosen, ChutesE2EEProvider):
            return ChutesE2EEProvider(
                chosen.config,
                client=client,
                listing_client=client,
                checks_client=client,
                intel=lambda raw, session: UP_TO_DATE,
                nvidia=lambda answer, nonce, session: 1,
            )
        return type(chosen)(chosen.config, client=client)

    cfg = Config(providers=list(endpoints), active_provider_name=endpoints[0].name)
    return EndpointsProvider(cfg, make=make)


# --- which models are Chutes' TEE ones ----------------------------------------------------


@pytest.mark.parametrize("model", TEE_IDS)
@pytest.mark.parametrize("url", CHUTES_HOSTS)
def test_a_tee_id_on_any_of_chutes_hosts_is_sealed_by_chutes(url, model):
    assert chutes_tee(model, url)
    assert privacy_at(model, url) == Privacy("sealed", CHUTES)
    assert privacy_at(model, url, "anthropic") == Privacy("sealed", CHUTES), "whatever it speaks"


def test_what_isnt_a_chutes_tee_model():
    assert not is_tee_id(ORDINARY) and not is_tee_id("TEE/glm-5.3") and not is_tee_id("x-TEEx")
    assert is_tee_id("a:b-TEE"), "every colon read as where a variant might begin"
    assert privacy_at(ORDINARY, CHUTES_BASE_URL) == Privacy(plain_on=CHUTES)
    assert not privacy_at(ORDINARY, CHUTES_BASE_URL).private
    for url in ("https://chutes.ai.example", "https://notchutes.ai/v1", NANO):
        assert not on_chutes(url) and not chutes_tee(MODEL, url), url
        assert privacy_at(MODEL, url) == Privacy(), "another host's -TEE id is nothing of Chutes'"
    assert on_chutes("https://LLM.Chutes.AI./v1"), "capitals and a trailing dot"


# --- the factory, and the plain clients' refusal ------------------------------------------


@pytest.mark.parametrize("api", ["auto", "openai", "anthropic"])
@pytest.mark.parametrize("url", CHUTES_HOSTS)
def test_a_chutes_tee_model_gets_chutes_client_and_an_ordinary_one_the_endpoints(url, api):
    for model in TEE_IDS:
        assert isinstance(
            make_chat_provider(chutes(base_url=url, api=api, model=model)), ChutesE2EEProvider
        )
    ordinary = make_chat_provider(chutes(base_url=url, api=api, model=ORDINARY))
    speaks = chutes(base_url=url, api=api).speaks()
    assert isinstance(
        ordinary, AnthropicProvider if speaks == "anthropic" else OpenAICompatibleProvider
    )


@pytest.mark.parametrize("model", TEE_IDS)
@pytest.mark.parametrize("url", CHUTES_HOSTS)
def test_a_chutes_tee_model_never_reaches_a_plain_client(url, model):
    """Built by hand, a plain client still refuses it before anything is sent:
    OpenAI-speaking or Anthropic-speaking, on any of Chutes' hosts."""
    client, sent = recording()
    for plain in (
        OpenAICompatibleProvider(chutes(base_url=url, api="openai"), client=client),
        AnthropicProvider(chutes(base_url=url, api="anthropic"), client=client),
    ):
        with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
            list(plain.stream(ask(model)))
    assert sent == []


def test_the_routing_provider_keeps_the_two_clients_of_one_endpoint_apart():
    fake = Mixed()
    route = routing(fake, chutes())
    assert isinstance(route.provider_for(f"Chutes::{MODEL}"), ChutesE2EEProvider)
    assert isinstance(route.provider_for(f"Chutes::{ORDINARY}"), OpenAICompatibleProvider)
    route.close()


# --- an ordinary model streams, and where the key goes ------------------------------------


def test_an_ordinary_chutes_model_streams_through_the_plain_client():
    fake = Mixed()
    route = routing(fake, chutes())
    text, done = route.complete(ask(f"Chutes::{ORDINARY}"))
    assert text == "The door gives." and done.finish_reason == "stop" and not done.sealed
    (sent,) = fake.requests
    assert (sent.url.host, sent.url.path) == ("llm.chutes.ai", "/v1/chat/completions")
    assert sent.headers["authorization"] == "Bearer cpk_test"
    assert json.loads(sent.content)["stream"] is True
    route.close()


def test_through_chutes_proxy_of_anthropics_api_an_ordinary_model_streams_too():
    """Its key as a bearer token: live (Nov 15 2026) Anthropic's own
    x-api-key reached the proxy's backend as no key at all."""
    fake = Mixed()
    route = routing(fake, chutes(base_url=CHUTES_ANTHROPIC_URL, api="anthropic"))
    text, _done = route.complete(ask(f"Chutes::{ORDINARY}"))
    assert text == "The door gives."
    (sent,) = fake.requests
    assert (sent.url.host, sent.url.path) == ("claude.chutes.ai", "/v1/messages")
    assert sent.headers["authorization"] == "Bearer cpk_test" and "x-api-key" not in sent.headers
    route.close()


def _proxy_stream(*texts: str, stop: str) -> bytes:
    """Chutes' proxy's stream as it came live: each text its own block, then
    the stop reason (its own "error" for a refusal of its backend)."""
    events: list[dict] = [{"type": "message_start", "message": {"id": "msg_1", "usage": {}}}]
    for index, text in enumerate(texts):
        events += [
            {"type": "content_block_start", "index": index, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def test_the_proxys_error_is_never_taken_for_the_models_words():
    """Live (Nov 15 2026), a request the proxy's backend refused came back
    as a passage: "⚠️ Backend Error … Authentication required." and the
    stop reason "error". It is said as the proxy's, and no text of it is
    passed on; the model's own text before it stays, and a model that
    writes those words itself, ending well, is passed on whole."""
    from sealedlore.providers.base import TextDelta

    error = '⚠️ Backend Error\n\nError: {"detail":"Authentication required."}\n\n'

    def streamed(body: bytes) -> tuple[list[str], BaseException | None]:
        provider = AnthropicProvider(
            chutes(base_url=CHUTES_ANTHROPIC_URL, api="anthropic"),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
            ),
        )
        texts: list[str] = []
        try:
            for event in provider.stream(ask(ORDINARY)):
                if isinstance(event, TextDelta):
                    texts.append(event.text)
        except ProviderError as exc:
            return texts, exc
        return texts, None

    texts, raised = streamed(_proxy_stream(error, stop="error"))
    assert texts == [] and raised is not None
    assert "error of its own, not the model's words" in str(raised)
    assert "Authentication required" in str(raised) and "\n" not in str(raised)
    texts, raised = streamed(_proxy_stream("The door gives.", error, stop="error"))
    assert texts == ["The door gives."] and raised is not None
    texts, raised = streamed(_proxy_stream(error, stop="end_turn"))
    assert "".join(texts) == error and raised is None


@pytest.mark.parametrize(
    "base, api", [(CHUTES_BASE_URL, "openai"), (CHUTES_ANTHROPIC_URL, "anthropic")]
)
def test_the_key_goes_to_the_endpoint_for_ordinary_models_and_api_chutes_for_tee_ones(base, api):
    """The one stated exception to a key going only where it was entered:
    Chutes' own hosts (models/config.py `CHUTES_KEY_ORIGINS`)."""
    fake = Mixed()
    route = routing(fake, chutes(base_url=base, api=api, key_saved_for=base.split("/v1")[0]))
    route.complete(ask(f"Chutes::{ORDINARY}"))
    plain_hosts = {r.url.host for r in fake.requests}
    assert plain_hosts == {httpx.URL(base).host}
    fake.requests.clear()
    text, done = route.complete(ask(f"Chutes::{MODEL}"))
    assert text == "The door opens." and done.sealed
    keyed = {
        r.url.host
        for r in fake.requests
        if "authorization" in r.headers or "x-api-key" in r.headers
    }
    assert keyed == {"api.chutes.ai"}, "the -TEE model's key went to api.chutes.ai alone"
    assert all(r.url.scheme == "https" for r in fake.requests)
    assert not [
        r for r in fake.requests if r.url.host == httpx.URL(base).host and r.method == "POST"
    ]
    route.close()


def test_a_key_saved_elsewhere_never_reaches_chutes_nor_chutes_key_elsewhere():
    fake = Mixed()
    elsewhere = chutes(key_saved_for="https://api.openai.com")
    route = routing(fake, elsewhere)
    for model in (ORDINARY, MODEL):
        with pytest.raises(ProviderError, match="was saved for https://api.openai.com"):
            route.complete(ask(f"Chutes::{model}"))
    assert not [r for r in fake.requests if "authorization" in r.headers]
    route.close()
    # And the reverse: a Chutes key on an endpoint edited to another host.
    client, sent = recording()
    moved = chutes(base_url="https://api.example.com/v1", key_saved_for="https://llm.chutes.ai")
    plain = make_chat_provider(moved.model_copy(update={"model": ORDINARY}))
    assert plain.config.key_withheld and not plain.config.api_key
    with pytest.raises(ProviderError, match="was saved for https://llm.chutes.ai"):
        OpenAICompatibleProvider(plain.config, client=client).complete(ask(ORDINARY))
    assert sent == []


def test_the_e2ee_client_takes_the_key_only_from_chutes_own_hosts():
    from sealedlore.providers.chutes_provider import not_chutes

    for url in CHUTES_HOSTS[:3]:
        assert not_chutes(url, httpx.URL(url).scheme + "://" + httpx.URL(url).host) is None
    assert "isn't one of Chutes' own addresses" in not_chutes("https://x.chutes.dev")
    assert "isn't one of Chutes' own addresses" in not_chutes("https://proxy.example/v1")
    assert "was saved for https://evil.example" in not_chutes(
        CHUTES_BASE_URL, "https://evil.example"
    )


def test_a_variant_of_a_tee_model_isnt_sent_and_says_why():
    fake = Mixed()
    route = routing(fake, chutes())
    with pytest.raises(NotOffered, match="choose the model itself"):
        route.complete(ask("Chutes::test/Model-TEE:THINKING"))
    assert not [r for r in fake.requests if r.url.path.startswith("/e2e/")]
    route.close()


# --- the listing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base, api", [(CHUTES_BASE_URL, "openai"), (CHUTES_ANTHROPIC_URL, "anthropic")]
)
def test_browse_lists_every_chutes_model_from_its_public_list_without_the_key(base, api):
    listing = json.loads((DATA / "chutes_models.json").read_text(encoding="utf-8"))
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=listing)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = chutes(base_url=base, api=api)
    provider = (AnthropicProvider if api == "anthropic" else OpenAICompatibleProvider)(
        settings, client=client
    )
    found = provider.fetch_model_prices()
    assert len(found) == 19
    assert [str(r.url) for r in sent] == ["https://llm.chutes.ai/v1/models"]
    assert "authorization" not in sent[0].headers and "x-api-key" not in sent[0].headers
    sealed = {m for m in found if privacy_at(m, base, api).sealed}
    assert len(sealed) == 11 and sealed == {
        m for m, e in found.items() if e["confidential_compute"]
    }
    assert all(privacy_at(m, base, api).plain_on == CHUTES for m in set(found) - sealed)
    priced = found["Qwen/Qwen3.8-27B-TEE"]["pricing"]
    assert priced["unit"] == "per_million_tokens" and priced["prompt"] > 0
    if api == "anthropic":
        assert provider.fetch_reasoning_defaults() is None, "Chutes' models aren't Claude"


# --- the three levels ---------------------------------------------------------------------


def test_each_model_has_its_level():
    cfg = config(chutes())

    def level(ref: str) -> str:
        return privacy_of(ref, cfg).level

    assert level("private/glm-5-3") == level("Tinfoil::glm-5-3") == HIGHEST
    assert level("TEE/glm-5.3") == level(f"Chutes::{MODEL}") == PARTIAL
    assert level(f"Chutes::{ORDINARY}") == level("z-ai/glm-5.3") == NOT_PRIVATE
    assert rank(Privacy("sealed", TINFOIL)) == rank(Privacy("sealed", NANOGPT_PRIVATE)) == 2
    assert rank(Privacy("sealed", CHUTES)) == rank(Privacy("tee", NANOGPT_TEE)) == 1
    assert rank(Privacy(plain_on=CHUTES)) == rank(Privacy()) == 0
    assert Privacy("sealed", "a-future-service").level == HIGHEST, "unknown: kept highest"


def test_what_holds_by_level():
    tee, chutes_tee_, tinfoil = (
        Privacy("tee", NANOGPT_TEE),
        Privacy("sealed", CHUTES),
        Privacy("sealed", TINFOIL),
    )
    assert holds(tee, chutes_tee_) and holds(tee, tinfoil), "a TEE chat at its level or higher"
    assert not holds(chutes_tee_, tee), "a sealed record needs its own service's client"
    assert not holds(chutes_tee_, tinfoil), "…however private the other is"
    assert not holds(tinfoil, chutes_tee_), "lower"
    assert not holds(chutes_tee_, Privacy(plain_on=CHUTES))


@pytest.mark.parametrize(
    "current, new, allowed",
    [
        ("private/glm-5-3", "Tinfoil::glm-5-3", True),
        ("Tinfoil::glm-5-3", "private/kimi-k3", True),
        ("private/glm-5-3", f"Chutes::{MODEL}", False),
        ("Tinfoil::glm-5-3", f"Chutes::{MODEL}", False),
        ("private/glm-5-3", "TEE/glm-5.3", False),
        ("TEE/glm-5.3", f"Chutes::{MODEL}", True),
        (f"Chutes::{MODEL}", "TEE/glm-5.3", True),
        (f"Chutes::{MODEL}", "Chutes::other/Model-TEE", True),
        (f"Chutes::{MODEL}", "private/glm-5-3", True),
        (f"Chutes::{MODEL}", "Tinfoil::glm-5-3", True),
        ("TEE/glm-5.3", "private/glm-5-3", True),
        (f"Chutes::{MODEL}", f"Chutes::{ORDINARY}", False),
        ("TEE/glm-5.3", f"Chutes::{ORDINARY}", False),
        ("TEE/glm-5.3", "z-ai/glm-5.3", False),
        ("z-ai/glm-5.3", f"Chutes::{ORDINARY}", True),
    ],
)
def test_a_chat_moves_only_to_its_own_level_or_higher(current, new, allowed):
    cfg = config(chutes())
    refusal = move_refusal(current, new, privacy=lambda ref: privacy_of(ref, cfg))
    assert (refusal is None) == allowed, refusal
    if refusal:
        assert "privacy" in refusal and "start a new chat" in refusal, "the levels, in words"


def test_the_refusals_name_the_levels_in_plain_words():
    cfg = config(chutes())

    def judge(ref: str) -> Privacy:
        return privacy_of(ref, cfg)

    assert move_refusal("private/glm-5-3", f"Chutes::{MODEL}", privacy=judge) == (
        f"Chutes::{MODEL} is end-to-end encrypted through Chutes, but has partial privacy, and "
        "this chat is kept to the highest privacy: choose one of NanoGPT's private/ models or "
        "Tinfoil's, or start a new chat."
    )
    words = move_refusal(f"Chutes::{MODEL}", f"Chutes::{ORDINARY}", privacy=judge)
    assert "so it has no privacy" in words and "kept to partial privacy" in words
    assert "Chutes' -TEE models" in words


def chutes_chat(tmp_path: Path, cfg: Config, model: str = f"Chutes::{MODEL}") -> StorySession:
    from tests.conftest import make_exchange

    story = Story(title="Sealed", mode="chat", chat_keep="memory")
    story.defaults.main_model = model
    nodes = make_exchange(1)
    story.active_leaf_id = nodes[-1].id
    return StorySession(
        StoryBundle(story=story, nodes=nodes),
        cfg,
        MockChatProvider(["A reply."]),
        root=tmp_path,
        learn_corrections=False,
    )


def test_a_tee_chat_and_a_chutes_chat_move_between_them_and_say_so(tmp_path: Path):
    cfg = config(chutes())
    session = chutes_chat(tmp_path, cfg, model="TEE/glm-5.3")
    assert session.story.chat_privacy.kind == "tee"
    session.check_chat_model(f"Chutes::{MODEL}")  # partial to partial
    said = session.note_model_move("TEE/glm-5.3", f"Chutes::{MODEL}")
    assert "NanoGPT's TEE models: partial privacy" in said
    assert "Chutes, end-to-end encrypted: partial privacy" in said
    session.story.defaults.main_model = f"Chutes::{MODEL}"
    session.adopt_chat_privacy()
    assert (session.story.chat_privacy.kind, session.story.chat_privacy.service) == (
        "sealed",
        CHUTES,
    )
    assert session.privacy_held() is None
    session.check_chat_model("TEE/glm-5.3")
    with pytest.raises(ValueError, match="no privacy"):
        session.check_chat_model(f"Chutes::{ORDINARY}")
    # A chat with the highest privacy never moves to Chutes.
    sealed = chutes_chat(tmp_path / "two", cfg, model="private/glm-5-3")
    with pytest.raises(ValueError, match="kept to the highest privacy"):
        sealed.check_chat_model(f"Chutes::{MODEL}")


def test_a_chutes_chat_is_held_when_its_endpoint_goes_plain_or_its_model_ordinary(tmp_path: Path):
    cfg = config(chutes())
    session = chutes_chat(tmp_path, cfg)
    record = session.story.chat_privacy
    assert (record.kind, record.service, record.origin) == (
        "sealed",
        CHUTES,
        "https://llm.chutes.ai",
    )
    assert session.privacy_held() is None
    # The endpoint edited to a plain host: the model id goes there in the clear.
    cfg.providers[-1] = cfg.providers[-1].model_copy(
        update={"base_url": "https://openrouter.ai/api/v1"}
    )
    reason = session.privacy_held()
    assert (
        "began end-to-end encrypted through Chutes (llm.chutes.ai), with partial privacy" in reason
    )
    assert "openrouter.ai" in reason and "not private" in reason and "nothing was sent" in reason
    assert session._privacy_lock(session.model, session.privacy(session.model))
    # Back on Chutes, but the chat's model set to one of Chutes' ordinary ones
    # without its check (a hand-edited story): held too.
    cfg.providers[-1] = chutes()
    assert session.privacy_held() is None
    session.story.defaults.main_model = f"Chutes::{ORDINARY}"
    assert "nothing was sent" in session.privacy_held()


# --- what a saved config from this branch's first build reads as --------------------------


def test_an_old_chutes_e2ee_endpoint_loads_as_chutes_openai_one():
    saved = {
        "providers": [
            {
                "name": "Chutes",
                "base_url": "https://api.chutes.ai",
                "api": "chutes-e2ee",
                "api_key": "cpk_test",
                "key_saved_for": "https://api.chutes.ai",
            }
        ],
        "active_provider_name": "Chutes",
        "private_provider": {
            "name": "private",
            "base_url": "https://api.chutes.ai",
            "api": "chutes-e2ee",
            "model": MODEL,
        },
    }
    cfg = Config.model_validate_json(json.dumps(saved))
    endpoint, private = cfg.providers[0], cfg.private_provider
    assert (endpoint.api, endpoint.base_url) == ("openai", CHUTES_BASE_URL)
    assert endpoint.key_saved_for == "https://llm.chutes.ai"
    assert endpoint.sendable().api_key == "cpk_test", "the key moved with it"
    assert (private.api, private.base_url) == ("openai", CHUTES_BASE_URL)
    assert privacy_of(MODEL, cfg) == Privacy("sealed", CHUTES)
    elsewhere = ProviderConfig.model_validate(
        {"name": "x", "base_url": "https://proxy.example/v1", "api": "chutes-e2ee"}
    )
    assert (elsewhere.api, elsewhere.base_url) == ("openai", "https://proxy.example/v1")


# --- the notes ----------------------------------------------------------------------------


def test_each_level_leads_its_note_and_chutes_says_every_limit():
    assert E2EE_NOTE.startswith("Highest privacy.") and TINFOIL_NOTE.startswith("Highest privacy.")
    assert TEE_NOTE.startswith("Partial privacy.") and CHUTES_NOTE.startswith("Partial privacy.")
    assert CHUTES_PLAIN_NOTE.startswith("Not private.")
    for limit in (
        "aegis library, a closed binary",
        "not measured",
        "Chutes' own",
        "Replies stream, each piece held to its number",
        "Chutes' closed library writes",
        "account, the model, timing, sizes and cost",
        "Why partial",
        "NanoGPT's gateway in the clear",
    ):
        assert limit in CHUTES_NOTE, limit
    assert Privacy(plain_on=CHUTES).label() == ("", CHUTES_PLAIN_NOTE)
    assert Privacy().label() == ("", "")
    for words in ("highest", "partial", "not private", "private/", "TEE/", "-TEE", "Tinfoil"):
        assert words in PRIVACY_LEVELS, words
    assert PRIVACY_LEVELS.count(".") <= 1 or PRIVACY_LEVELS.endswith("text."), "one sentence"
    assert level_line(Privacy("sealed", CHUTES)).startswith("Partial privacy")
    assert level_line(Privacy("tee", NANOGPT_TEE)).startswith("Partial privacy")
    assert level_line(Privacy("sealed", TINFOIL)).startswith("Highest privacy")
    assert level_line(Privacy()).startswith("Not private")


# --- embeddings, pictures, videos, Jev and the context probe ------------------------------


def test_the_other_clients_refuse_a_chutes_tee_model_and_take_an_ordinary_one():
    from sealedlore.providers.context_probe import detect_context

    client, sent = recording()
    tee = EmbeddingProviderConfig(
        base_url=CHUTES_BASE_URL, api_key="k", model="Qwen/Qwen3-Embedding-8B-TEE"
    )
    with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
        OpenAICompatibleEmbeddings(tee, client=client).embed(["The Hidden Caves."])
    http = MediaHttp(CHUTES_BASE_URL, "k", timeout_seconds=5, client=client)
    with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
        http.request("POST", CHUTES_BASE_URL + "/images/generations", json={"model": "x/Image-TEE"})
    with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
        http.request("POST", CHUTES_BASE_URL + "/x/Image-TEE", json={"prompt": "a farmhouse"})
    with pytest.raises(ProviderError, match="one of Chutes' TEE models"):
        DecisionsClient(CHUTES_BASE_URL, "k", client=client).decide(MODEL, "state", {"q": "?"})
    assert detect_context(CHUTES_BASE_URL, "k", MODEL, client=client) is None
    assert sent == [], "nothing of a -TEE model went in the clear"

    ordinary = EmbeddingProviderConfig(base_url=CHUTES_BASE_URL, api_key="k", model="BAAI/bge-m3")
    with pytest.raises(ProviderError):  # the recorder's empty answer isn't vectors
        OpenAICompatibleEmbeddings(ordinary, client=client).embed(["The Hidden Caves."])
    http.request("POST", CHUTES_BASE_URL + "/images/generations", json={"model": "x/Image"})
    with pytest.raises(ProviderError):
        DecisionsClient(CHUTES_BASE_URL, "k", client=client).decide(ORDINARY, "s", {"q": "?"})
    assert [r.url.path for r in sent] == [
        "/v1/embeddings",
        "/v1/images/generations",
        "/v1/decisions",
    ]
    # Tinfoil stays refused wholesale.
    with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
        MediaHttp(TINFOIL_URL, "k", timeout_seconds=5, client=client).request(
            "POST", TINFOIL_URL + "/images/generations", json={"model": "x"}
        )
    assert len(sent) == 3


def test_the_manual_says_the_three_levels_in_the_apps_own_words():
    manual = (Path(__file__).parent.parent / "docs" / "manual.md").read_text(encoding="utf-8")
    assert " ".join(PRIVACY_LEVELS.split()) in " ".join(manual.split())
