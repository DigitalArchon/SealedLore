"""nano-gpt Private Mode (providers/private_mode.py): end-to-end encrypted.

A fake enclave holds a real X25519 key and speaks EHBP, so every test here
seals and opens real ciphertext; only the attestation (Tinfoil's SDK) and the
network are stood in for."""

from __future__ import annotations

import hashlib
import json
import os
import struct

import httpx
import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCompleted,
    TextDelta,
)
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.private_catalog import MARKUP, private_entries
from sealedlore.providers.private_mode import (
    CACHE_SECRET_FIELD,
    ENCLAVE_URL_HEADER,
    MAX_AGE_SECONDS,
    Enclave,
    PrivateModeProvider,
    Verified,
    attestation_record,
    shape_body,
)

pytest.importorskip("ehbp")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from ehbp import EHBPTransport  # noqa: E402
from ehbp.derive import derive_response_keys, encrypt_chunk, frame_chunk  # noqa: E402
from ehbp.protocol import (  # noqa: E402
    ENCAPSULATED_KEY_HEADER,
    EXPORT_LABEL,
    EXPORT_LENGTH,
    HPKE_REQUEST_INFO,
    KEY_CONFIG_PROBLEM_TYPE,
    PROBLEM_JSON_MEDIA_TYPE,
    RESPONSE_NONCE_HEADER,
)
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey  # noqa: E402

BASE = "https://nano-gpt.com/api/v1"
RELAY = "https://nano-gpt.com/api/v1/private/tinfoil"
SECRET_WORDS = "the violet key under the third stair"
SCOPE = "ab" * 32

SUITE = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES256_GCM)


def sse(*texts: str, done: bool = True, finish: str | None = "stop") -> bytes:
    lines = []
    for index, text in enumerate(texts):
        chunk = {"id": "c1", "choices": [{"delta": {"content": text}}]}
        if index == len(texts) - 1 and finish:
            chunk["choices"][0]["finish_reason"] = finish
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append(
        'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\n'
    )
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


class FakeEnclave:
    """An enclave: opens what was sealed to its key, and seals its reply."""

    def __init__(self) -> None:
        self.key = X25519PrivateKey.generate()
        self.public_hex = (
            self.key.public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            .hex()
        )
        self.requests: list[dict] = []
        self.wire: list[bytes] = []
        self.headers: list[httpx.Headers] = []
        self.reply = sse("Hello", " there.")
        self.sealed_reply = True
        self.private_header = True
        self.refuse_key = 0  # how many requests to answer "key changed"

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.wire.append(body)
        self.headers.append(request.headers)
        if self.refuse_key:
            self.refuse_key -= 1
            return httpx.Response(
                422,
                headers={"content-type": PROBLEM_JSON_MEDIA_TYPE},
                json={"type": KEY_CONFIG_PROBLEM_TYPE, "title": "key changed"},
            )
        enc = bytes.fromhex(request.headers[ENCAPSULATED_KEY_HEADER])
        context = SUITE.create_recipient_context(
            enc, KEMKey.from_pyca_cryptography_key(self.key), info=HPKE_REQUEST_INFO
        )
        (length,) = struct.unpack(">I", body[:4])
        self.requests.append(json.loads(context.open(body[4 : 4 + length], b"")))
        headers = {"content-type": "text/event-stream"}
        if self.private_header:
            headers["x-nanogpt-private-mode"] = "tinfoil"
        if not self.sealed_reply:
            return httpx.Response(200, headers=headers, content=self.reply)
        nonce = os.urandom(32)
        keys = derive_response_keys(context.export(EXPORT_LABEL, EXPORT_LENGTH), enc, nonce)
        headers[RESPONSE_NONCE_HEADER] = nonce.hex()
        return httpx.Response(
            200, headers=headers, content=frame_chunk(encrypt_chunk(keys, 0, self.reply))
        )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def record(enclave: FakeEnclave) -> Verified:
    return Verified(
        hpke_public_key=enclave.public_hex,
        enclave="router-0.tinfoil.sh",
        attestation=attestation_record(
            enclave="router-0.tinfoil.sh",
            hardware="AMD SEV-SNP",
            measurement="b3be62c7",
            release_digest="ad95d02b" * 8,
            release_tag="v0.0.155",
            latest_release="",
            latest_checked=True,
            hpke_public_key=enclave.public_hex,
        ),
    )


@pytest.fixture
def fake() -> FakeEnclave:
    return FakeEnclave()


@pytest.fixture
def setup(fake):
    attested: list[str] = []
    preflights: list[dict] = []
    clock = Clock()

    def verifier(relay: str, secret: str) -> Verified:
        attested.append(relay)
        return record(fake)

    def preflight(request: httpx.Request) -> httpx.Response:
        preflights.append(json.loads(request.read()))
        return httpx.Response(
            200, json={"ok": True, "cacheScope": SCOPE, "upstreamModel": "glm-5-3"}
        )

    config = ProviderConfig(
        name="nano", base_url=BASE, api_key="sk-test", extra_body={"provider": {"order": ["x"]}}
    )
    provider = PrivateModeProvider(
        config,
        enclave=Enclave(RELAY, verifier=verifier, clock=clock),
        transport_factory=lambda key: EHBPTransport.from_public_key_hex(
            key, inner=httpx.MockTransport(fake.handler)
        ),
        preflight_client=httpx.Client(transport=httpx.MockTransport(preflight)),
    )
    return provider, attested, preflights, clock


def request(model: str = "private/glm-5-3", **params) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[
            PromptMessage(role="system", parts=(ContentPart(text="You are a storyteller."),)),
            PromptMessage(role="user", parts=(ContentPart(text=SECRET_WORDS),)),
        ],
        params=GenerationParams(**params),
        use_cache_control=True,
    )


def test_the_prompt_leaves_sealed_and_the_reply_comes_back_opened(fake, setup):
    provider, attested, preflights, _clock = setup
    events = list(provider.stream(request(temperature=0.7)))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there."
    completed = events[-1]
    assert isinstance(completed, StreamCompleted) and completed.sealed
    assert completed.finish_reason == "stop"

    # On the wire: ciphertext only.
    assert SECRET_WORDS.encode() not in fake.wire[0]
    assert b"storyteller" not in fake.wire[0]
    # What the enclave opened: the router's fields, our own cache secret.
    body = fake.requests[0]
    assert body["model"] == "glm-5-3" and body["temperature"] == 0.7
    assert body["messages"][1] == {"role": "user", "content": SECRET_WORDS}
    assert body[CACHE_SECRET_FIELD] == provider.enclave.cache_secret != SCOPE
    assert "provider" not in body, "fields the router doesn't take are dropped"
    # nano-gpt sees the proof of its own scope, the model, and nothing else.
    headers = fake.headers[0]
    assert headers["x-nanogpt-private-cache-scope"] == hashlib.sha256(SCOPE.encode()).hexdigest()
    assert headers["x-nanogpt-private-model"] == "private/glm-5-3"
    assert headers[ENCLAVE_URL_HEADER] == "https://router-0.tinfoil.sh"
    assert "x-nanogpt-private-proxy-version" not in headers
    assert preflights == [
        {"model": "private/glm-5-3", "requestBodyBytes": preflights[0]["requestBodyBytes"]}
    ]
    assert attested == [RELAY]


def test_nothing_is_sent_or_charged_before_the_enclave_is_attested(fake):
    preflights: list = []

    def verifier(relay, secret):
        raise RuntimeError("measurement mismatch")

    provider = PrivateModeProvider(
        ProviderConfig(name="nano", base_url=BASE, api_key="k"),
        enclave=Enclave(RELAY, verifier=verifier),
        transport_factory=lambda key: EHBPTransport.from_public_key_hex(
            key, inner=httpx.MockTransport(fake.handler)
        ),
        preflight_client=httpx.Client(
            transport=httpx.MockTransport(lambda r: preflights.append(r) or httpx.Response(500))
        ),
    )
    with pytest.raises(ProviderError, match="couldn't be attested.*nothing was sent"):
        list(provider.stream(request()))
    assert fake.wire == [] and preflights == []


def test_an_unsealed_reply_is_refused(fake, setup):
    provider, *_ = setup
    fake.sealed_reply = False
    seen: list = []
    with pytest.raises(ProviderError, match="couldn't be opened"):
        for event in provider.stream(request()):
            seen.append(event)
    assert seen == []


def test_a_reply_nano_gpt_didnt_route_privately_is_refused(fake, setup):
    provider, *_ = setup
    fake.private_header = False
    seen: list = []
    with pytest.raises(ProviderError, match="Private Mode"):
        for event in provider.stream(request()):
            seen.append(event)
    assert seen == []


def test_a_stream_cut_short_never_passes_as_whole(fake, setup):
    provider, *_ = setup
    fake.reply = sse("Half a", done=False)
    with pytest.raises(ProviderError, match="before it was complete"):
        list(provider.stream(request()))


def test_a_changed_key_is_attested_again_once(fake, setup):
    provider, attested, _pre, _clock = setup
    fake.refuse_key = 1
    events = list(provider.stream(request()))
    assert isinstance(events[-1], StreamCompleted)
    assert len(attested) == 2

    fake.refuse_key = 2
    with pytest.raises(ProviderError, match="twice"):
        list(provider.stream(request()))


def test_an_attestation_is_redone_once_it_has_aged(fake, setup):
    provider, attested, _pre, clock = setup
    list(provider.stream(request()))
    clock.now += MAX_AGE_SECONDS - 1
    list(provider.stream(request()))
    assert len(attested) == 1
    clock.now += 2
    list(provider.stream(request()))
    assert len(attested) == 2


def test_a_background_client_seals_to_the_same_enclave_with_the_same_secret(fake, setup):
    provider, attested, *_ = setup
    detached = provider.detached()
    list(provider.stream(request()))
    list(detached.stream(request()))
    assert detached.enclave is provider.enclave and len(attested) == 1
    assert fake.requests[0][CACHE_SECRET_FIELD] == fake.requests[1][CACHE_SECRET_FIELD]
    assert provider.sibling().enclave is provider.enclave


def test_the_plain_client_never_sends_a_private_model():
    sent: list = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    provider = OpenAICompatibleProvider(ProviderConfig(name="n", base_url=BASE), client=client)
    with pytest.raises(ProviderError, match="end-to-end encrypted"):
        list(provider.stream(request()))
    assert sent == []


def test_the_private_client_refuses_any_other_model(fake, setup):
    provider, *_ = setup
    with pytest.raises(ProviderError, match="isn't a Private Mode model"):
        list(provider.stream(request("TEE/glm-5.3")))
    assert fake.wire == []


def test_each_model_family_is_shaped_as_the_router_takes_it():
    def shaped(model, upstream, **params):
        req = request(model, **params)
        payload = OpenAICompatibleProvider(ProviderConfig(name="n")).build_payload(req)
        return shape_body(payload, req, upstream)

    glm = shaped("private/glm-5-3", "glm-5-3", reasoning={"enabled": True, "effort": "medium"})
    assert glm["chat_template_kwargs"] == {"thinking": True}
    assert glm["reasoning_effort"] == "high" and "reasoning" not in glm
    gemma = shaped("private/gemma4-31b:thinking", "gemma4-31b")
    assert gemma["chat_template_kwargs"] == {"enable_thinking": True}
    assert shaped("private/gemma4-31b", "gemma4-31b")["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    light = {"enabled": True, "effort": "low"}  # engine/archival.LIGHT_REASONING
    assert shaped("private/gemma4-31b", "gemma4-31b", reasoning=light)["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert shaped("private/gemma4-31b:thinking", "gemma4-31b", reasoning=light)[
        "chat_template_kwargs"
    ] == {"enable_thinking": True}
    assert shaped("private/glm-5-3", "glm-5-3", reasoning=light)["reasoning_effort"] == "low"
    kimi = shaped("private/kimi-k3", "kimi-k3", temperature=0.9, top_p=0.5)
    assert "temperature" not in kimi and "top_p" not in kimi
    deepseek = shaped("private/deepseek-v4-1-flash", "deepseek-v4-1-flash")
    assert deepseek["chat_template_kwargs"] == {"thinking": False}
    # No cache_control blocks: plain text.
    assert all(isinstance(m["content"], str) for m in glm["messages"])


def test_private_models_are_listed_and_priced_from_their_tee_counterparts():
    listing = {
        "TEE/glm-5.3": {
            "id": "TEE/glm-5.3",
            "name": "GLM 5.3 TEE",
            "context_length": 1048576,
            "pricing": {"prompt": 1.0, "completion": 4.0, "unit": "per_million_tokens"},
        }
    }
    entries = private_entries(["private/glm-5-3", "private/new-model"], listing)
    glm = entries["private/glm-5-3"]
    assert glm["name"] == "GLM 5.3 TEE (end-to-end encrypted)"
    assert glm["pricing"]["prompt"] == pytest.approx(MARKUP)
    assert glm["context_length"] == 1048576
    assert listing["TEE/glm-5.3"]["pricing"]["prompt"] == 1.0, "the source is not changed"
    assert "pricing" not in entries["private/new-model"]


def test_the_sdk_check_refuses_incomplete_evidence_and_reports_an_old_release(monkeypatch):
    import tinfoil
    import tinfoil.github

    from sealedlore.providers import private_mode

    class Doc:
        def __init__(self, steps, verified=True):
            self.data = {
                "securityVerified": verified,
                "releaseDigest": "d1",
                "enclaveFingerprint": "m1",
                "enclaveMeasurement": {"measurement": {"type": "x/sev-snp-guest/v2"}},
                "steps": steps,
            }

        def to_dict(self):
            return self.data

    made: list[dict] = []
    ok_steps = {s: {"status": "success"} for s in private_mode.REQUIRED_STEPS}

    class FakeClient:
        doc = Doc(ok_steps)

        def __init__(self, **kwargs):
            made.append(kwargs)
            self.enclave = "router-0.tinfoil.sh"

        def verify(self):
            class Truth:
                hpke_public_key = "aa" * 32
                digest = "d1"

            return Truth()

        def get_verification_document(self):
            return FakeClient.doc

    class Release:
        def __init__(self, tag, digest):
            self.tag, self.digest = tag, digest

    monkeypatch.setattr(tinfoil, "SecureClient", FakeClient)
    monkeypatch.setattr(tinfoil.github, "fetch_latest_release", lambda repo: Release("v9", "d2"))
    verified = private_mode.tinfoil_verify(RELAY, "s" * 64)
    assert made[0]["user_cache_secret"] == "s" * 64, "never the SDK's file in ~/.tinfoil"
    assert made[0]["attestation_bundle_url"] == RELAY
    assert verified.attestation.latest_release == "v9"
    assert "older release" in verified.attestation.detail
    assert verified.attestation.hardware == "AMD SEV-SNP"

    monkeypatch.setattr(tinfoil.github, "fetch_latest_release", lambda repo: Release("v8", "d1"))
    current = private_mode.tinfoil_verify(RELAY, "s" * 64).attestation
    assert current.latest_release == "" and current.release_tag == "v8"

    FakeClient.doc = Doc({**ok_steps, "compareMeasurements": {"status": "failed"}})
    with pytest.raises(ProviderError, match="compareMeasurements"):
        private_mode.tinfoil_verify(RELAY, "s" * 64)


# --- in a story and a chat ------------------------------------------------------------


def sealed_provider(fake: FakeEnclave) -> PrivateModeProvider:
    def preflight(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "cacheScope": SCOPE, "upstreamModel": "glm-5-3"}
        )

    return PrivateModeProvider(
        ProviderConfig(name="nano", base_url=BASE, api_key="sk-test"),
        enclave=Enclave(RELAY, verifier=lambda relay, secret: record(fake)),
        transport_factory=lambda key: EHBPTransport.from_public_key_hex(
            key, inner=httpx.MockTransport(fake.handler)
        ),
        preflight_client=httpx.Client(transport=httpx.MockTransport(preflight)),
    )


def test_a_private_scene_on_an_encrypted_model_is_sealed_end_to_end(fake, tmp_path):
    from tests.test_private import MARKER, build, play, turn

    session, main, held = build(tmp_path)
    play(session, turn(held, "Before the scene."))
    session.enter_private(keep="memory", provider=sealed_provider(fake), model="private/glm-5-3")
    fake.reply = sse("A quiet reply.")
    play(session, turn(held, f"{MARKER} whispered"))
    reply = session.full_path()[-1]
    assert reply.content == "A quiet reply." and reply.meta.tee == "encrypted"
    assert session.full_path()[-2].meta.tee == "encrypted", "the author's message too"

    fake.reply = sse("They spoke quietly.")
    summary = session.summarise_private()
    assert summary == "They spoke quietly."
    assert session.open_span.summary_tee == "encrypted"
    # The turn reasons as the model likes; the summary, lightly (live: 107s → 9s).
    assert "reasoning_effort" not in fake.requests[0]
    assert fake.requests[-1]["reasoning_effort"] == "low"

    # The enclave read the scene; nothing on the wire, and no other model, did.
    assert any(MARKER in json.dumps(body) for body in fake.requests)
    assert not any(MARKER.encode() in body for body in fake.wire)
    assert MARKER not in json.dumps(main.payloads)


def test_a_memory_only_encrypted_chat_leaves_no_trace_and_marks_each_reply(fake, tmp_path):
    from sealedlore.engine.prompt import TurnRequest
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.models.node import CHAT_USER_ID
    from sealedlore.models.story import Story
    from sealedlore.storage.repository import StoryBundle
    from tests.test_chat import listing, make_config

    story = Story(title="Sealed", mode="chat", chat_prompt="Be brief.", chat_keep="memory")
    story.defaults.main_model = "private/glm-5-3"
    before = listing(tmp_path)
    session = StorySession(
        StoryBundle(story=story),
        make_config(),
        sealed_provider(fake),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    for text in ("A secret: the violet key.", "And another."):
        list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text=text)))
    assert [node.meta.tee for node in session.path()] == ["encrypted"] * 4
    assert listing(tmp_path) == before
    assert not any(b"violet" in body for body in fake.wire)


def test_an_encrypted_chat_never_moves_to_a_model_that_isnt():
    from sealedlore.providers.tee import move_refusal

    assert move_refusal("private/glm-5-3", "private/kimi-k3") is None
    assert "end-to-end encrypted" in move_refusal("private/glm-5-3", "TEE/glm-5.3")
    assert "end-to-end encrypted" in move_refusal("private/glm-5-3", "anthropic/claude")
    assert move_refusal("TEE/glm-5.3", "private/glm-5-3") is None, "a TEE chat may move up"
    assert "TEE" in move_refusal("TEE/glm-5.3", "anthropic/claude")
    assert move_refusal("anthropic/claude", "private/glm-5-3") is None


def test_only_a_tee_models_summaries_ask_for_light_reasoning():
    from sealedlore.engine.archival import LIGHT_REASONING, summary_params

    for model in ("TEE/glm-5.3-flash", "private/glm-5-3", "TEE/gemma-4-31b-it"):
        assert summary_params(model).reasoning == LIGHT_REASONING
    for model in ("deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-4.6", "local/model"):
        assert not summary_params(model).reasoning.enabled
    assert summary_params("TEE/x", max_tokens=4000).max_tokens == 4000
    # Never shared: a change to one request's params can't leak into the next.
    first = summary_params("TEE/x")
    first.reasoning.effort = "high"
    assert summary_params("TEE/x").reasoning.effort == "low"
