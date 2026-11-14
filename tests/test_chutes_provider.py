"""Chutes' end-to-end encrypted models (providers/chutes_provider.py), the
whole flow offline: a fake api.chutes.ai and llm.chutes.ai answering as
Chutes' code does, its instances keys of our own (an ML-KEM-768 key, a TLS
key bound into a quote with the registers of a published set), Intel's and
NVIDIA's checks stood in for (the real ones hold real Chutes evidence in
tests/test_chutes_evidence.py). Discovery, attestation and its cache, the
nonces, the sealed request and its reply, and every refusal."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import secrets
import struct
import uuid
from pathlib import Path

import httpx
import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.providers import chutes_e2ee, dcap
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCancelled
from sealedlore.providers.chutes_e2ee import INFO_REQUEST, INFO_RESPONSE, derive_key
from sealedlore.providers.chutes_provider import (
    CHUTES_ORIGIN,
    EVIDENCE_LIMIT,
    ChutesE2EEProvider,
    ChutesState,
    NotOffered,
)
from sealedlore.providers.tee import (
    MEASUREMENTS_LISTED,
    NRAS_URL,
    REATTEST_SECONDS,
    WORKLOAD_UNMEASURED,
    TeeRefused,
    what_was_attested,
)

mlkem = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.mlkem")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

CHUTE = "9ecb73cc-909b-5334-9cd8-74985945cff5"
MODEL = "test/Model-TEE"
MRTD = bytes(range(48))
RTMRS = tuple(bytes([i]) * 48 for i in (1, 2, 3, 4))
UP_TO_DATE = dcap.Result(status="UpToDate", platform="UpToDate", qe="UpToDate", notes=["ok"])
REPLY = {
    "id": "chatcmpl-7",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "The door opens.",
                "reasoning_content": "Hm.",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 40,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 8},
    },
}


def tdx_quote(report_data: bytes, mrtd: bytes = MRTD, rtmrs: tuple = RTMRS) -> bytes:
    """A version 4 TDX quote with its registers and report data in place and
    nothing signed (Intel's check is stood in for)."""
    header = (
        struct.pack("<HHI", 4, dcap.ECDSA_P256, dcap.TDX_TEE_TYPE)
        + bytes(4)
        + dcap.INTEL_QE_VENDOR_ID
        + bytes(20)
    )
    body = bytearray(584)
    body[136:184] = mrtd
    for offset, value in zip((328, 376, 424, 472), rtmrs, strict=True):
        body[offset : offset + 48] = value
    body[520:584] = report_data
    inner = struct.pack("<HI", dcap.PCK_CHAIN, 0)
    cert = bytes(dcap.QE_REPORT_LEN) + bytes(64) + struct.pack("<H", 0) + inner
    signature = bytes(128) + struct.pack("<HI", dcap.QE_REPORT_DATA, len(cert)) + cert
    return header + bytes(body) + struct.pack("<I", len(signature)) + signature


class FakeInstance:
    """An instance: its ML-KEM key, its TLS key and certificate, its registers."""

    tls = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def __init__(self, *, mrtd: bytes = MRTD, binds: str | None = None) -> None:
        from datetime import UTC, datetime

        from cryptography import x509

        self.id = str(uuid.uuid4())
        self.key = mlkem.MLKEM768PrivateKey.generate()
        self.pubkey = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()
        self.mrtd = mrtd
        # The key its quote binds: its own unless told to bind another's.
        self.binds = binds
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "sek8s")])
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.tls.public_key())
            .serial_number(1)
            .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
            .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
            .sign(self.tls, hashes.SHA256())
        )

    def evidence(self, nonce: str, *, signer=None) -> dict:
        spki = self.cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        bound = hashlib.sha256((nonce + (self.binds or self.pubkey)).encode()).digest()
        quote = tdx_quote(bound + hashlib.sha256(spki).digest(), self.mrtd)
        gpu = [{"arch": "HOPPER", "evidence": "e", "certificate": "c"}]
        attested = json.dumps(
            {
                "nonce": nonce,
                "evidence": {
                    "quote": base64.b64encode(quote).decode(),
                    "nvtrust_evidence": json.dumps(gpu),
                },
            }
        ).encode()
        signature = (signer or self.tls).sign(attested, padding.PKCS1v15(), hashes.SHA256())
        return {
            "tee_type": "tdx",
            "instance_id": self.id,
            "quote": base64.b64encode(quote).decode(),
            "certificate": base64.b64encode(self.cert.public_bytes(Encoding.DER)).decode(),
            "signature": base64.b64encode(signature).decode(),
            "attested_body": base64.b64encode(attested).decode(),
            "gpu_evidence": gpu,
        }

    def open(self, blob: bytes) -> dict:
        ciphertext, nonce, sealed = blob[:1088], blob[1088:1100], blob[1100:]
        key = derive_key(self.key.decapsulate(ciphertext), ciphertext, INFO_REQUEST)
        return json.loads(gzip.decompress(ChaCha20Poly1305(key).decrypt(nonce, sealed, None)))


def seal_reply(reply: dict, response_pk: str) -> bytes:
    client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
    secret, ciphertext = client.encapsulate()
    key = derive_key(secret, ciphertext, INFO_RESPONSE)
    nonce = os.urandom(12)
    return (
        ciphertext
        + nonce
        + ChaCha20Poly1305(key).encrypt(nonce, gzip.compress(json.dumps(reply).encode()), None)
    )


class FakeChutes:
    """api.chutes.ai and llm.chutes.ai as Chutes' code answers (chutes-api
    api/e2e/router.py, api/chute/router.py), every request recorded."""

    def __init__(self, count: int = 3) -> None:
        self.instances = [FakeInstance() for _ in range(count)]
        self.requests: list[httpx.Request] = []
        self.issued: dict[str, str] = {}  # nonce token -> instance id
        self.used: list[str] = []
        self.opened: list[tuple[str, dict]] = []
        self.reply: dict = REPLY
        self.evidence_status: list[int] = []  # statuses to answer before a 200
        self.invoke_status: list[int] = []
        self.invoke_body: bytes | None = None
        self.tamper = False
        self.redirect: str | None = None
        self.measured = [MRTD]
        self.nonce_life = 60

    # --- the routes

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if str(url).startswith(NRAS_URL):
            return httpx.Response(200, json=[["JWT", "a.b.c"], {}])
        if url.host == "llm.chutes.ai" and url.path == "/v1/models":
            return httpx.Response(200, json=self.models())
        if url.host != "api.chutes.ai":
            return httpx.Response(404)
        if self.redirect and url.path == self.redirect:
            return httpx.Response(307, headers={"location": "https://elsewhere.example/steal"})
        if url.path == "/servers/tdx/measurements":
            return httpx.Response(
                200,
                json=[
                    {
                        "version": "1.5.0",
                        "name": f"set {i}",
                        "mrtd": m.hex().upper(),
                        **{f"rtmr{j}": r.hex().upper() for j, r in enumerate(RTMRS)},
                    }
                    for i, m in enumerate(self.measured)
                ],
            )
        if url.path == f"/chutes/{CHUTE}/evidence":
            if self.evidence_status:
                status = self.evidence_status.pop(0)
                return httpx.Response(status, headers={"retry-after": "1"}, json={"detail": "no"})
            nonce = url.params["nonce"]
            return httpx.Response(
                200,
                json={
                    "evidence": [i.evidence(nonce) for i in self.instances],
                    "failed_instance_ids": [],
                    "excluded_instance_ids": [],
                },
            )
        if url.path.startswith("/instances/") and url.path.endswith("/evidence"):
            if request.headers.get("authorization") != "Bearer cpk_test":
                return httpx.Response(401, json={"detail": "no key"})
            instance_id = url.path.split("/")[2]
            instance = next(i for i in self.instances if i.id == instance_id)
            return httpx.Response(200, json=instance.evidence(url.params["nonce"]))
        if url.path == f"/e2e/instances/{CHUTE}":
            if request.headers.get("authorization") != "Bearer cpk_test":
                return httpx.Response(401, json={"detail": "Can't find a user with that api key"})
            listed = []
            for instance in self.instances[:5]:
                tokens = [secrets.token_urlsafe(24) for _ in range(10)]
                self.issued.update({t: instance.id for t in tokens})
                listed.append(
                    {"instance_id": instance.id, "e2e_pubkey": instance.pubkey, "nonces": tokens}
                )
            return httpx.Response(
                200, json={"instances": listed, "nonce_expires_in": self.nonce_life}
            )
        if url.path == "/e2e/invoke" and request.method == "POST":
            return self.invoke(request)
        return httpx.Response(404)

    def invoke(self, request: httpx.Request) -> httpx.Response:
        headers = request.headers
        token, instance_id = headers["x-e2e-nonce"], headers["x-instance-id"]
        if self.issued.get(token) != instance_id or token in self.used:
            return httpx.Response(403, json={"detail": "Invalid, expired, or already-used nonce"})
        self.used.append(token)
        if self.invoke_status:
            return httpx.Response(self.invoke_status.pop(0), json={"detail": "Instance‮ busy\nnow"})
        assert headers["authorization"] == "Bearer cpk_test"
        assert (headers["x-chute-id"], headers["x-e2e-stream"]) == (CHUTE, "false")
        assert headers["x-e2e-path"] == "/v1/chat/completions"
        if self.invoke_body is not None:
            return httpx.Response(200, content=self.invoke_body)
        instance = next(i for i in self.instances if i.id == instance_id)
        body = instance.open(request.content)
        self.opened.append((instance_id, body))
        blob = bytearray(seal_reply(self.reply, body["e2e_response_pk"]))
        if self.tamper:
            blob[-1] ^= 1
        return httpx.Response(
            200, content=bytes(blob), headers={"content-type": "application/octet-stream"}
        )

    @staticmethod
    def models() -> dict:
        return {
            "data": [
                {
                    "id": MODEL,
                    "chute_id": CHUTE,
                    "confidential_compute": True,
                    "pricing": {"prompt": 0.2, "completion": 1.0, "input_cache_read": 0.02},
                    "context_length": 131072,
                },
                {"id": "plain/Model", "chute_id": str(uuid.uuid4()), "confidential_compute": False},
            ]
        }

    # --- what was asked

    def paths(self, *, host: str = "api.chutes.ai") -> list[str]:
        return [r.url.path for r in self.requests if r.url.host == host]

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def provider(fake: FakeChutes, clock=None, **config) -> ChutesE2EEProvider:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    settings = {"name": "Chutes", "base_url": CHUTES_ORIGIN, "api_key": "cpk_test", **config}
    return ChutesE2EEProvider(
        ProviderConfig(**settings),
        client=client,
        listing_client=client,
        checks_client=client,
        intel=lambda raw, session: UP_TO_DATE,
        nvidia=lambda answer, nonce, session: 1,
        state=ChutesState(clock) if clock is not None else None,
    )


def ask(model: str = f"Chutes::{MODEL}") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[
            PromptMessage(role="system", parts=(ContentPart(text="You are the storyteller."),)),
            PromptMessage(role="user", parts=(ContentPart(text="Open the door."),)),
        ],
        params=GenerationParams(max_tokens=300, temperature=0.7),
    )


@pytest.fixture
def fake() -> FakeChutes:
    return FakeChutes()


# --- the whole flow -------------------------------------------------------------------------


def test_a_request_is_sealed_to_an_attested_instance_and_its_reply_opened(fake):
    chutes = provider(fake)
    text, completed = chutes.complete(ask())
    assert text == "The door opens."
    assert completed.sealed and completed.finish_reason == "stop" and not completed.unterminated
    assert (completed.usage.prompt_tokens, completed.usage.completion_tokens) == (40, 5)
    assert completed.usage.cache_read_tokens == 8 and not completed.usage.cost_reported
    assert completed.response_id == "chatcmpl-7" and completed.sent_at is not None
    # What the instance opened: the body the inspector shows, our response key beside it.
    instance_id, body = fake.opened[0]
    assert instance_id == chutes.last_instance
    response_pk = body.pop("e2e_response_pk")
    assert len(base64.b64decode(response_pk)) == 1184
    assert body == chutes.build_payload(ask())
    assert body["model"] == MODEL and body["stream"] is False and "stream_options" not in body
    assert body["messages"][1] == {"role": "user", "content": "Open the door."}
    # Discovered once, the whole chute's evidence once, then sealed.
    assert fake.paths() == [
        f"/e2e/instances/{CHUTE}",
        "/servers/tdx/measurements",
        f"/chutes/{CHUTE}/evidence",
        "/e2e/invoke",
    ]


def test_the_record_is_partial_always_and_says_what_was_attested(fake):
    chutes = provider(fake)
    attestation = chutes.attest(MODEL)
    assert attestation.verified and attestation.encrypted and attestation.level == "partial"
    assert attestation.attested == "instance" and attestation.instances == 3
    assert attestation.shortfalls == [WORKLOAD_UNMEASURED, MEASUREMENTS_LISTED]
    assert attestation.gpus == 3 and attestation.tcb_status == "UpToDate"
    assert attestation.hardware == "Intel TDX" and attestation.measurement == "set 0 (1.5.0)"
    assert "3 of 3 instances" in attestation.detail
    words = what_was_attested(attestation)
    assert words.startswith("Sealed on this computer to the key of one Chutes instance")
    assert "(one of the 3 attested)" in words and "closed binary" in words
    assert "prompt cache is shared" in words
    assert fake.count("/e2e/invoke") == 0 and not fake.used, "attesting takes no nonce"


def test_every_shortfall_chutes_names_says_what_it_costs():
    """As test_attestation_check holds tee.py's: the model button's tooltip
    explains each (window_private.SHORTFALL_TIPS)."""
    import ast

    import sealedlore.providers.chutes_provider as module
    from sealedlore.gui.window_private import SHORTFALL_TIPS

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    named = {
        getattr(module, node.args[0].id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fall_short"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    # The keyless check (Test attestation without a key) names a third.
    assert named == {WORKLOAD_UNMEASURED, MEASUREMENTS_LISTED, module.KEY_BINDING_UNCHECKED}
    assert all(SHORTFALL_TIPS.get(what) for what in named)


def test_a_nonce_is_used_once_and_never_again(fake):
    fake.instances = fake.instances[:1]  # ten nonces in all, per discovery
    chutes = provider(fake)
    for _ in range(12):
        chutes.complete(ask())
    assert len(fake.used) == len(set(fake.used)) == 12
    assert fake.count(f"/e2e/instances/{CHUTE}") == 2, "discovered again when they ran out"
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 1, "the instance attested once"


def test_a_nonce_is_used_only_while_it_has_time_left(fake):
    now = [1000.0]
    chutes = provider(fake, clock=lambda: now[0])
    chutes.complete(ask())
    now[0] += 51  # 60 seconds' life, less the margin
    chutes.complete(ask())
    assert fake.count(f"/e2e/instances/{CHUTE}") == 2
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 1, "still attested"


def test_an_instance_attested_fifteen_minutes_ago_is_attested_again(fake):
    now = [1000.0]
    chutes = provider(fake, clock=lambda: now[0])
    chutes.complete(ask())
    now[0] += REATTEST_SECONDS - 1
    chutes.complete(ask())
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 1
    now[0] += 1
    chutes.complete(ask())
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 2
    nonces = [r.url.params["nonce"] for r in fake.requests if r.url.path.endswith("/evidence")]
    assert len(set(nonces)) == 2 and all(len(n) == 64 for n in nonces)


def test_an_instance_first_seen_since_the_chutes_evidence_is_attested_alone(fake):
    now = [1000.0]
    chutes = provider(fake, clock=lambda: now[0])
    chutes.complete(ask())
    newcomer = FakeInstance()
    fake.instances.append(newcomer)
    now[0] += 120  # the nonces gone: discovered again, the newcomer with them
    chutes.complete(ask())
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 1
    own = [r for r in fake.requests if r.url.path == f"/instances/{newcomer.id}/evidence"]
    assert len(own) == 1 and own[0].headers["authorization"] == "Bearer cpk_test"
    assert chutes.state.attestations[CHUTE].instances == 4


@pytest.mark.parametrize(
    "spoil, why",
    [
        ({"binds": "another key"}, "doesn't bind the key Chutes lists"),
        ({"mrtd": bytes(48)}, "aren't among the sets Chutes publishes"),
    ],
    ids=["unbound key", "unlisted image"],
)
def test_an_instance_that_fails_is_left_out_said_and_never_sealed_to(fake, spoil, why):
    bad = FakeInstance(**spoil)
    fake.instances[1] = bad
    chutes = provider(fake)
    for _ in range(10):
        chutes.complete(ask())
    assert bad.id not in {h.headers.get("x-instance-id") for h in fake.requests}
    attestation = chutes.state.attestations[CHUTE]
    assert attestation.instances == 2 and "2 of 3 instances" in attestation.detail
    assert f"left out, never sealed to: {bad.id[:8]}" in attestation.detail
    assert why in attestation.detail


def test_an_instance_signed_by_another_key_is_left_out(fake):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = fake.instances[0]
    original = bad.evidence
    bad.evidence = lambda nonce: original(nonce, signer=other)
    attestation = provider(fake).attest(MODEL)
    assert attestation.instances == 2 and "didn't sign its evidence" in attestation.detail


def test_with_no_instance_left_the_model_is_refused_and_nothing_sent(fake):
    fake.measured = [bytes(48)]
    chutes = provider(fake)
    with pytest.raises(
        TeeRefused, match="none of the instances Chutes listed .* could be attested"
    ):
        chutes.complete(ask())
    assert fake.count("/e2e/invoke") == 0 and not fake.used


def test_chutes_evidence_limit_is_waited_out_within_the_attempt(fake, monkeypatch):
    chutes = provider(fake)
    waits: list[float] = []
    monkeypatch.setattr(chutes._wake, "wait", lambda seconds: waits.append(seconds) or False)
    fake.evidence_status = [429, 429]
    text, _ = chutes.complete(ask())
    assert text == "The door opens." and waits == [1.0, 1.0]
    nonces = [r.url.params["nonce"] for r in fake.requests if r.url.path.endswith("/evidence")]
    assert len(nonces) == 3 == len(set(nonces)), "a fresh nonce each time"


def test_chutes_evidence_limit_past_the_attempt_refuses_in_words(fake, monkeypatch):
    import sealedlore.providers.chutes_provider as module

    monkeypatch.setattr(module, "ATTEMPT_SECONDS", 1.5)
    chutes = provider(fake)
    fake.evidence_status = [429] * 5
    with pytest.raises(TeeRefused) as caught:
        chutes.complete(ask())
    assert EVIDENCE_LIMIT in str(caught.value) and "nothing was sent" in str(caught.value)
    assert fake.count("/e2e/invoke") == 0


def test_a_stop_while_waiting_on_the_limit_stops(fake, monkeypatch):
    chutes = provider(fake)
    fake.evidence_status = [429]
    monkeypatch.setattr(chutes._wake, "wait", lambda seconds: True)
    with pytest.raises(StreamCancelled):
        chutes.complete(ask())


def test_a_reply_whose_seal_doesnt_hold_is_refused_and_doubts_the_attestation(fake):
    chutes = provider(fake)
    chutes.complete(ask())
    fake.tamper = True
    with pytest.raises(ProviderError, match="couldn't be opened .* attested again"):
        chutes.complete(ask())
    fake.tamper = False
    chutes.complete(ask())
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 2, "attested again at once"
    assert fake.count(f"/e2e/instances/{CHUTE}") == 2


@pytest.mark.parametrize(
    "body",
    [b'{"choices": [{"message": {"content": "relay text"}}]}', b"x" * 4000],
    ids=["json", "long"],
)
def test_an_unsealed_reply_is_never_taken_for_the_models(fake, body):
    fake.invoke_body = body
    with pytest.raises(ProviderError, match="couldn't be opened"):
        provider(fake).complete(ask())


def test_a_refusal_is_said_as_chutes_unauthenticated_words_and_a_500_never_resent(fake):
    fake.invoke_status = [500]
    with pytest.raises(ProviderError) as caught:
        provider(fake).complete(ask())
    assert str(caught.value) == (
        "Chutes refused the sealed request (HTTP 500); "
        "Chutes said (unauthenticated): Instance busy now"
    )
    assert caught.value.status_code == 500 and fake.count("/e2e/invoke") == 1


def test_a_busy_instance_is_asked_again_with_a_fresh_nonce(fake, monkeypatch):
    chutes = provider(fake)
    monkeypatch.setattr(chutes._wake, "wait", lambda seconds: False)
    fake.invoke_status = [429]
    text, _ = chutes.complete(ask())
    assert text == "The door opens." and len(fake.used) == 2 == len(set(fake.used))


def test_a_nonce_chutes_refuses_sends_the_next_request_to_discovery(fake):
    chutes = provider(fake)
    chutes.complete(ask())
    fake.issued.clear()  # every nonce of that discovery now refused
    with pytest.raises(ProviderError, match="HTTP 403"):
        chutes.complete(ask())
    chutes.complete(ask())
    assert fake.count(f"/e2e/instances/{CHUTE}") == 2


def test_the_key_goes_to_api_chutes_ai_alone(fake):
    chutes = provider(fake)
    chutes.complete(ask())
    chutes.fetch_model_prices()
    keyed = {(r.url.scheme, r.url.host) for r in fake.requests if "authorization" in r.headers}
    assert keyed == {("https", "api.chutes.ai")}
    unkeyed = {r.url.path for r in fake.requests if "authorization" not in r.headers}
    assert {"/v1/models", f"/chutes/{CHUTE}/evidence", "/servers/tdx/measurements"} <= unkeyed
    assert all(
        r.url.host != "llm.chutes.ai" or "authorization" not in r.headers for r in fake.requests
    )


@pytest.mark.parametrize("where", ["/e2e/invoke", f"/e2e/instances/{CHUTE}"])
def test_no_redirect_is_followed(fake, where):
    fake.redirect = where
    with pytest.raises(ProviderError, match="redirected"):
        provider(fake).complete(ask())
    assert all(r.url.host != "elsewhere.example" for r in fake.requests)


@pytest.mark.parametrize(
    "config, why",
    [
        ({"base_url": "https://llm.chutes.ai/v1"}, "isn't Chutes' API"),
        ({"base_url": "https://chutes.example.com"}, "isn't Chutes' API"),
        ({"api_key": ""}, "need a Chutes API key"),
        ({"key_saved_for": "https://evil.example"}, "was saved for https://evil.example"),
    ],
    ids=["llm host", "elsewhere", "no key", "key for elsewhere"],
)
def test_nothing_is_fetched_or_sent_without_a_key_that_may_go_to_chutes(fake, config, why):
    chutes = provider(fake, **config)
    chutes.config = chutes.config.sendable()
    with pytest.raises(ProviderError, match=why):
        chutes.complete(ask())
    with pytest.raises(ProviderError, match=why):
        chutes.attest(MODEL)
    assert fake.requests == []


def test_a_model_chutes_doesnt_list_as_confidential_is_never_sent(fake):
    chutes = provider(fake)
    with pytest.raises(
        NotOffered, match="isn't one of Chutes' end-to-end encrypted models"
    ) as caught:
        chutes.complete(ask("Chutes::plain/Model"))
    assert caught.value.offered == [MODEL]
    assert fake.paths() == [], "not even discovered"
    assert chutes.still_offered(MODEL) is True and chutes.still_offered("plain/Model") is False


def test_without_ml_kem_nothing_is_sent(fake, monkeypatch):
    monkeypatch.setattr(chutes_e2ee, "_available", False)
    with pytest.raises(ProviderError, match="OpenSSL 3.5"):
        provider(fake).complete(ask())
    assert fake.requests == []


def test_the_models_own_error_inside_the_seal_is_said(fake):
    fake.reply = {"error": {"message": "context too long"}}
    with pytest.raises(ProviderError, match="model's server reported an error: context too long"):
        provider(fake).complete(ask())


def test_a_reply_with_no_finish_reason_is_marked_unfinished(fake):
    fake.reply = {"choices": [{"message": {"content": "Half"}}]}
    text, completed = provider(fake).complete(ask())
    assert text == "Half" and completed.unterminated


def test_a_discovered_key_that_isnt_one_is_never_sealed_to(fake):
    raw = base64.b64decode(fake.instances[0].pubkey)
    fake.instances[0].pubkey = base64.b64encode(raw[:-1]).decode()
    chutes = provider(fake)
    for _ in range(5):
        chutes.complete(ask())
    assert fake.instances[0].id not in {r.headers.get("x-instance-id") for r in fake.requests}
    assert "1183 bytes" in chutes.state.attestations[CHUTE].detail


def test_siblings_and_detached_copies_share_one_attestation_and_one_pool(fake):
    chutes = provider(fake)
    chutes.complete(ask())
    chutes.sibling().complete(ask())
    background = chutes.detached()
    background.complete(ask())
    assert background.state is chutes.state is chutes.sibling().state
    assert fake.count(f"/chutes/{CHUTE}/evidence") == 1
    assert fake.count(f"/e2e/instances/{CHUTE}") == 1
    assert len(set(fake.used)) == 3


def test_the_model_list_is_chutes_confidential_models_priced(fake):
    listing = provider(fake).fetch_model_prices()
    assert list(listing) == [MODEL]
    assert listing[MODEL]["pricing"] == {
        "prompt": 0.2,
        "completion": 1.0,
        "unit": "per_million_tokens",
        "input_cache_read": 0.02,
    }


# --- GPUs NVIDIA didn't vouch for -----------------------------------------------------------


class NoGpuEvidence(FakeInstance):
    """An instance whose evidence offers nothing for NVIDIA to check."""

    def evidence(self, nonce: str, *, signer=None) -> dict:
        found = super().evidence(nonce, signer=signer)
        attested = json.loads(base64.b64decode(found["attested_body"]))
        attested["evidence"]["nvtrust_evidence"] = "[]"
        body = json.dumps(attested).encode()
        signature = (signer or self.tls).sign(body, padding.PKCS1v15(), hashes.SHA256())
        found.update(
            attested_body=base64.b64encode(body).decode(),
            signature=base64.b64encode(signature).decode(),
            gpu_evidence=[],
        )
        return found


def test_an_instance_with_no_gpu_evidence_is_left_out_and_never_sealed_to(fake):
    bad = NoGpuEvidence()
    fake.instances[1] = bad
    chutes = provider(fake)
    for _ in range(10):
        chutes.complete(ask())
    assert bad.id not in {i for i, _ in fake.opened}, "never sealed to"
    attestation = chutes.state.attestations[CHUTE]
    assert attestation.instances == 2
    assert f"instance {bad.id[:8]} offered no GPU evidence for NVIDIA to check" in (
        attestation.detail
    )
    assert "no GPU evidence" not in attestation.shortfalls, "not a partial of the rest"
    # Without a key too: the keyless check leaves it out.
    public = provider(fake).attest_public(MODEL)
    assert public.instances == 2


def test_with_nvidia_out_of_reach_every_instance_is_left_out_and_chutes_refused(fake):
    class NvidiaDown(FakeChutes):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(NRAS_URL):
                self.requests.append(request)
                return httpx.Response(503)
            return super().handler(request)

    down = NvidiaDown()
    chutes = provider(down)
    with pytest.raises(TeeRefused, match="NVIDIA's attestation service couldn't be reached"):
        chutes.complete(ask())
    assert down.opened == [] and down.count("/e2e/invoke") == 0, "nothing sealed or sent"


def test_with_nvidias_keys_out_of_reach_every_instance_is_left_out(fake):
    from sealedlore.providers import nras

    def keys_gone(answer, nonce, session):
        raise nras.KeysUnavailable("NVIDIA's signing keys couldn't be fetched")

    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    chutes = ChutesE2EEProvider(
        ProviderConfig(name="Chutes", base_url=CHUTES_ORIGIN, api_key="cpk_test"),
        client=client,
        listing_client=client,
        checks_client=client,
        intel=lambda raw, session: UP_TO_DATE,
        nvidia=keys_gone,
    )
    with pytest.raises(TeeRefused, match="NVIDIA's signing keys couldn't be fetched"):
        chutes.complete(ask())
    assert fake.opened == []
