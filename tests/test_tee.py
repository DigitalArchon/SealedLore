"""TEE verification and context detection, against fixed vectors and mocked servers."""

from __future__ import annotations

import base64
import json
import struct
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sealedlore.models.config import ProviderConfig
from sealedlore.providers import dcap, nras
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCompleted
from sealedlore.providers.context_probe import detect_context
from sealedlore.providers.ethsig import (
    address_of,
    keccak256,
    personal_hash,
    recover,
    recover_personal,
    sign,
)
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.tee import NRAS_URL, TeeClient, TeeRefused, is_tee

KEY = 0xB25C7DB31FEED9122727BF0939DC769A96564B2DE4C4726D035B36ECF1E5B364
ADDRESS = "0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E"


def test_keccak_and_addresses_match_known_vectors():
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )
    assert address_of(1) == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    assert address_of(KEY) == ADDRESS


def test_a_signature_recovers_its_signer_and_only_its_signer():
    digest = personal_hash(b"req:resp")
    signature = sign(digest, KEY, 987654321)
    assert recover(digest, signature) == ADDRESS
    assert recover_personal("req:resp", signature.hex()) == ADDRESS
    assert recover(personal_hash(b"tampered"), signature) != ADDRESS


def jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'ES384'})}.{part(claims)}.c2ln"


def fake_quote(report_data: bytes) -> str:
    """A version 4 TDX quote with every field in place and nothing signed: enough
    for parse_quote, and for tests that stand in for Intel's verification."""
    header = (
        struct.pack("<HHI", 4, dcap.ECDSA_P256, dcap.TDX_TEE_TYPE)
        + bytes(4)
        + dcap.INTEL_QE_VENDOR_ID
        + bytes(20)
    )
    body = bytes(520) + report_data
    inner = struct.pack("<HI", dcap.PCK_CHAIN, 0)
    cert = bytes(dcap.QE_REPORT_LEN) + bytes(64) + struct.pack("<H", 0) + inner
    signature_data = bytes(128) + struct.pack("<HI", dcap.QE_REPORT_DATA, len(cert)) + cert
    return (header + body + struct.pack("<I", len(signature_data)) + signature_data).hex()


UP_TO_DATE = dcap.Result(
    status="UpToDate", platform="UpToDate", qe="UpToDate", notes=["Intel's chain verifies"]
)


def tee_server(
    *,
    nonce_bound: bool = True,
    echo_nonce: bool = False,
    gpu: bool = True,
    signer: int = KEY,
    evidence: bool = True,
    attestation_status: int = 200,
    nras_status: int = 200,
):
    address = address_of(KEY).lower()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(NRAS_URL):
            if nras_status != 200:
                return httpx.Response(nras_status, text="NRAS says no")
            return httpx.Response(
                200, json=[["JWT", jwt({"x-nvidia-overall-att-result": gpu})], {}]
            )
        if "/tee/attestation" in url:
            if attestation_status != 200:
                return httpx.Response(attestation_status, text="no attestation")
            nonce = request.url.params["nonce"]
            report = (
                bytes.fromhex(address[2:])
                + bytes(12)
                + (bytes.fromhex(nonce) if nonce_bound else bytes(32))
            )
            return httpx.Response(
                200,
                json={
                    "signing_address": address,
                    # nano-gpt's JSON copy: never believed over the quote's bytes.
                    "attestation": {"report_data": report.hex()},
                    "nonce": nonce if echo_nonce else None,
                    "intel_quote": fake_quote(report),
                    "nvidia_payload": json.dumps(
                        {"evidence_list": [{"evidence": "e"}] if evidence else []}
                    ),
                },
            )
        if "/tee/signature/" in url:
            text = "aa:bb"
            signature = sign(personal_hash(text.encode()), signer, 12345).hex()
            return httpx.Response(
                200, json={"text": text, "signature": signature, "signing_address": address}
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def client_for(*, intel=UP_TO_DATE, nvidia=8, **server) -> TeeClient:
    """A TeeClient on the fake server, Intel's and NVIDIA's checks stood in for
    (a result to return, or an exception to raise); the real ones are checked
    against real evidence below."""

    def intel_verify(raw, session):
        if isinstance(intel, BaseException):
            raise intel
        return intel

    def nvidia_verify(answer, nonce, session):
        if isinstance(nvidia, BaseException):
            raise nvidia
        return nvidia

    return TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        "TEE/x",
        client=tee_server(**server),
        intel=intel_verify,
        nvidia=nvidia_verify,
    )


def test_a_bound_quote_intel_and_nvidia_vouch_for_is_fully_attested():
    client = client_for()
    result = client.attest()
    assert result.level == "full" and result.verified and not result.shortfalls
    assert (result.gpus, result.tcb_status, result.gpu_ok) == (8, "UpToDate", True)
    assert result.signing_address == address_of(KEY).lower()
    assert "binds the signing key to our nonce" in result.detail
    assert "vouches for 8 GPUs" in result.detail
    assert client.verify_reply("req_1") is True


def test_a_nonce_merely_echoed_back_is_refused():
    """The old fallback: a nonce in nano-gpt's JSON proves nothing."""
    with pytest.raises(TeeRefused, match="didn't bind"):
        client_for(nonce_bound=False, echo_nonce=True).attest()


def test_a_quote_intel_does_not_vouch_for_is_refused():
    with pytest.raises(TeeRefused, match="failed verification: .*revoked"):
        client_for(intel=dcap.DcapError("the PCK certificate has been revoked")).attest()
    with pytest.raises(TeeRefused, match="debug TD"):
        client_for(intel=dcap.DcapError("it is a debug TD")).attest()


def test_what_couldnt_be_checked_or_isnt_current_is_partial_and_named():
    stale = dcap.Result(status="OutOfDate", platform="OutOfDate", qe="UpToDate")
    assert client_for(intel=stale).attest().shortfalls == ["Intel TCB OutOfDate"]
    unchecked = client_for(intel=dcap.Result(collateral=False)).attest()
    assert unchecked.level == "partial" and unchecked.shortfalls == ["Intel TCB unchecked"]
    no_gpu = client_for(evidence=False).attest()
    assert no_gpu.level == "partial" and no_gpu.gpu_ok is None
    assert no_gpu.shortfalls == ["no GPU evidence"]


def test_nvidia_unreachable_is_partial_and_nvidia_rejecting_is_refused():
    down = client_for(nras_status=503).attest()
    assert down.level == "partial" and down.shortfalls == ["GPUs unverified"]
    assert "couldn't be reached" in down.detail
    keys = client_for(nvidia=nras.KeysUnavailable("keys could not be fetched")).attest()
    assert keys.shortfalls == ["NVIDIA signature unchecked"]
    with pytest.raises(TeeRefused, match="NVIDIA rejected"):
        client_for(nras_status=400).attest()
    with pytest.raises(TeeRefused, match="NVIDIA rejected"):
        client_for(gpu=False, nvidia=nras.KeysUnavailable("no keys")).attest()
    with pytest.raises(TeeRefused, match="GPU attestation failed: .*nonce"):
        client_for(nvidia=nras.NrasError("not made for this request's nonce")).attest()


def test_no_attestation_is_refused():
    with pytest.raises(TeeRefused, match="offers no attestation"):
        client_for(attestation_status=400).attest()


def test_a_reply_signed_by_another_key_fails():
    client = client_for(signer=1)
    client.attest()
    assert client.verify_reply("req_1") is False


# --- real evidence: Intel's and NVIDIA's own signatures ---------------------------------

FIXTURE = Path(__file__).parent / "data" / "tee_attestation.json"


def fixture_server(fixture: dict, *, intel_status: int = 200, jwks: dict | None = None):
    """One real attestation, captured live from TEE/glm-5.3-flash with Intel's
    collateral and NVIDIA's answer and keys exactly as they came (in the author's
    AI-Assistant app, Sept 26 2026; the same nano-gpt endpoint)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        params = dict(request.url.params)
        if "/tee/attestation" in url:
            return httpx.Response(
                200,
                json={
                    "signing_address": fixture["signing_address"],
                    "intel_quote": fixture["intel_quote"],
                    "nvidia_payload": json.dumps(
                        {"nonce": fixture["nonce"], "evidence_list": [{"evidence": "e"}]}
                    ),
                },
            )
        if url == NRAS_URL:
            return httpx.Response(200, json=fixture["nras_answer"])
        if url == nras.JWKS_URL:
            return httpx.Response(200, json=fixture["jwks"] if jwks is None else jwks)
        for item in fixture["collateral"]:
            if item["url"] == url and (item["params"] or {}) == params:
                if intel_status != 200:
                    return httpx.Response(intel_status)
                return httpx.Response(
                    item["status"],
                    content=base64.b64decode(item["body"]),
                    headers=item["headers"],
                )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def real():
    fixture = json.loads(FIXTURE.read_text())
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})
    yield fixture
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})


def at(fixture: dict) -> datetime:
    return datetime.fromtimestamp(fixture["now"], UTC)


def intel(fixture: dict, raw: bytes | None = None, **server) -> dcap.Result:
    quote = raw or bytes.fromhex(fixture["intel_quote"])
    return dcap.verify(quote, fixture_server(fixture, **server), now=at(fixture))


def nvidia(fixture: dict, answer=None, nonce=None, now=None, **server) -> int:
    return nras.verify(
        answer or fixture["nras_answer"],
        nonce or fixture["nonce"],
        fixture_server(fixture, **server),
        now=now or fixture["now"],
    )


def test_the_quote_verifies_up_to_intels_root_and_is_current(real):
    result = intel(real)
    assert result.collateral and result.status == "UpToDate"
    assert (result.platform, result.module, result.qe) == ("UpToDate",) * 3
    assert result.fmspc == "20a06f000000"


def test_a_changed_report_breaks_the_quotes_signature(real):
    tampered = bytearray(bytes.fromhex(real["intel_quote"]))
    tampered[dcap.HEADER_LEN + 520] ^= 1  # the report data: the signing key
    with pytest.raises(dcap.DcapError, match="quote's signature"):
        intel(real, bytes(tampered))


def test_a_debug_td_is_refused(real):
    debug = bytearray(bytes.fromhex(real["intel_quote"]))
    debug[dcap.HEADER_LEN + 120] |= 0x01  # TDATTRIBUTES.DEBUG
    with pytest.raises(dcap.DcapError, match="debug TD"):
        intel(real, bytes(debug))


def test_a_chain_to_any_other_root_is_refused(real, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "Intel SGX Root CA"),
            x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, "Intel Corporation"),
            x509.NameAttribute(x509.NameOID.LOCALITY_NAME, "Santa Clara"),
            x509.NameAttribute(x509.NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(x509.NameOID.COUNTRY_NAME, "US"),
        ]
    )
    impostor = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2018, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2049, 1, 1, tzinfo=UTC))
        .sign(key, hashes.SHA256())
    )
    monkeypatch.setattr(dcap, "intel_root", lambda: impostor)
    with pytest.raises(dcap.DcapError, match="signature does not verify"):
        intel(real)


def test_a_revocation_list_intel_did_not_sign_is_refused(real):
    forged = json.loads(json.dumps(real))
    crl = bytearray(base64.b64decode(forged["collateral"][0]["body"]))
    crl[-5] ^= 1  # inside the CRL's signature
    forged["collateral"][0]["body"] = base64.b64encode(bytes(crl)).decode()
    with pytest.raises(dcap.DcapError, match="CRL is not signed"):
        intel(forged)


def test_tcb_information_intel_did_not_sign_is_refused(real):
    forged = json.loads(json.dumps(real))
    body = base64.b64decode(forged["collateral"][2]["body"]).decode()
    body = body.replace('"tcbEvaluationDataNumber":', '"tcbEvaluationDataNumber":1', 1)
    forged["collateral"][2]["body"] = base64.b64encode(body.encode()).decode()
    with pytest.raises(dcap.DcapError, match="signature on its tcbInfo"):
        intel(forged)


def test_intels_collateral_out_of_reach_is_reported_not_passed(real):
    result = intel(real, intel_status=503)
    assert not result.collateral and result.status == ""
    assert "could not be fetched" in result.notes[-1]


def test_nvidias_verdict_verifies_for_every_gpu(real):
    assert nvidia(real) == 8


def test_a_verdict_for_another_nonce_is_refused(real):
    with pytest.raises(nras.NrasError, match="nonce"):
        nvidia(real, nonce="00" * 32)


def test_an_old_verdict_is_refused(real):
    with pytest.raises(nras.NrasError, match="validity window"):
        nvidia(real, now=real["now"] + 86400)


def test_edited_claims_break_nvidias_signature(real):
    answer = json.loads(json.dumps(real["nras_answer"]))
    header, claims, signature = answer[0][1].split(".")
    decoded = json.loads(base64.urlsafe_b64decode(claims + "=="))
    decoded["jti"] = "replayed"
    claims = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    answer[0][1] = f"{header}.{claims}.{signature}"
    with pytest.raises(nras.NrasError, match="signature does not verify"):
        nvidia(real, answer)


def test_a_gpu_token_swapped_for_another_is_refused(real):
    answer = json.loads(json.dumps(real["nras_answer"]))
    gpus = sorted(answer[1])
    answer[1][gpus[0]] = answer[1][gpus[1]]
    with pytest.raises(nras.NrasError, match="not the one"):
        nvidia(real, answer)


def test_a_key_nvidia_does_not_publish_is_refused(real):
    with pytest.raises(nras.NrasError, match="does not publish"):
        nvidia(real, jwks={"keys": []})


def test_the_whole_real_attestation_is_full(real, monkeypatch):
    import sealedlore.providers.tee as tee_module

    client = TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        real["model"],
        client=fixture_server(real),
        intel=lambda raw, s: dcap.verify(raw, s, now=at(real)),
        nvidia=lambda a, n, s: nras.verify(a, n, s, now=real["now"]),
    )
    monkeypatch.setattr(tee_module.secrets, "token_hex", lambda _n: real["nonce"])
    result = client.attest()
    assert result.level == "full" and not result.shortfalls
    assert (result.gpus, result.tcb_status) == (8, "UpToDate")
    assert result.signing_address == real["signing_address"]


def test_the_real_quote_for_another_nonce_is_refused(real):
    """The same genuine quote, replayed against a fresh request."""
    client = TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        real["model"],
        client=fixture_server(real),
        intel=lambda raw, s: dcap.verify(raw, s, now=at(real)),
    )
    with pytest.raises(TeeRefused, match="didn't bind"):
        client.attest()  # our nonce is random: the captured quote holds another


def test_tee_models_are_named_so():
    assert is_tee("TEE/glm-5.3-flash")
    assert not is_tee("Steelskull/teething")


def test_the_stream_keeps_the_completion_id():
    body = (
        'data: {"id": "req_42", "choices": [{"delta": {"content": "Hi"}}]}\n\n'
        'data: {"id": "req_42", "choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=body))
    provider = OpenAICompatibleProvider(
        ProviderConfig(name="p", base_url="https://x.test/v1", model="m"),
        client=httpx.Client(transport=transport),
    )
    events = list(provider.stream(ChatRequest(model="m", messages=[])))
    done = next(e for e in events if isinstance(e, StreamCompleted))
    assert done.response_id == "req_42"


# --- how much context a private model has --------------------------------------------


def probe(routes: dict[str, dict]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for path, body in routes.items():
            if request.url.path == path:
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_context_from_each_kind_of_server():
    nano = probe({"/api/v1/models": {"data": [{"id": "TEE/x", "context_length": 131072}]}})
    assert detect_context("https://nano-gpt.com/api/v1", "", "TEE/x", client=nano).tokens == 131072

    ollama = probe({"/api/ps": {"models": [{"name": "llama3:8b", "context_length": 8192}]}})
    found = detect_context("http://localhost:11434/v1", "", "llama3:8b", client=ollama)
    assert found.tokens == 8192 and found.loaded

    lm = probe(
        {
            "/api/v1/models": {
                "models": [
                    {
                        "key": "qwen",
                        "max_context_length": 32768,
                        "loaded_instances": [{"config": {"context_length": 16384}}],
                    }
                ]
            }
        }
    )
    assert detect_context("http://localhost:1234/v1", "", "qwen", client=lm).tokens == 16384

    llama = probe({"/props": {"default_generation_settings": {"n_ctx": 4096}}})
    assert detect_context("http://localhost:8080/v1", "", "any", client=llama).tokens == 4096
    assert detect_context("http://localhost:9/v1", "", "x", client=probe({})) is None


# --- per-instance providers (Chutes), relays and outages ---------------------------------


class InstanceEvidence:
    """Chutes' per-instance attestation, built with real keys: each instance's
    certificate key is bound into its quote's report data, and signs a body
    carrying the nonce, the quote and the GPU evidence (TEE/kimi-k3's shape)."""

    def __init__(self, count: int = 2) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.keys, self.certs = [], []
        for i in range(count):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "attestation")])
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(i + 1)
                .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
                .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
                .sign(key, hashes.SHA256())
            )
            self.keys.append(key)
            self.certs.append(cert)
        self.gpu_nonce = "ab" * 32

    def body(
        self,
        nonce: str,
        *,
        sign_with=None,
        bind: bool = True,
        signed_nonce: str | None = None,
        other_quote: bool = False,
        failed: tuple = (),
    ) -> dict:
        import hashlib

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        evidence = []
        for i, (key, cert) in enumerate(zip(self.keys, self.certs, strict=True)):
            spki = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
            bound = hashlib.sha256(spki).digest() if bind else bytes(32)
            quote = bytes.fromhex(fake_quote(bytes.fromhex(self.gpu_nonce) + bound))
            signed_quote = bytes.fromhex(fake_quote(bytes(64))) if other_quote else quote
            gpu = [{"arch": "HOPPER", "evidence": "e", "certificate": "c"}]
            attested = json.dumps(
                {
                    "nonce": signed_nonce or nonce,
                    "evidence": {
                        "tdx_quote": base64.b64encode(signed_quote).decode(),
                        "nvtrust_evidence": json.dumps(gpu),
                    },
                }
            ).encode()
            signature = (sign_with or key).sign(attested, padding.PKCS1v15(), hashes.SHA256())
            evidence.append(
                {
                    "instance_id": f"inst-{i}",
                    "quote": base64.b64encode(quote).decode(),
                    "certificate": base64.b64encode(cert.public_bytes(Encoding.DER)).decode(),
                    "signature": base64.b64encode(signature).decode(),
                    "attested_body": base64.b64encode(attested).decode(),
                }
            )
        return {"evidence": evidence, "failed_instance_ids": list(failed)}


def shaped(respond, *, model: str = "TEE/kimi-k3", seen: dict | None = None) -> TeeClient:
    """A TeeClient whose /tee/attestation answers `respond(nonce) -> (status, body)`."""
    seen = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(NRAS_URL):
            seen.setdefault("nras_nonces", []).append(json.loads(request.content)["nonce"])
            return httpx.Response(200, json=[["JWT", jwt({"x-nvidia-overall-att-result": True})]])
        if "/tee/attestation" in url:
            status, body = respond(request.url.params["nonce"])
            return httpx.Response(status, json=body)
        if "/tee/signature/" in url:
            return httpx.Response(400, json={"error": "TEE signature is not available"})
        return httpx.Response(404)

    def nvidia_verify(answer, nonce, session):
        seen.setdefault("verified_nonces", []).append(nonce)
        return 8

    return TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        model,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        intel=lambda raw, session: UP_TO_DATE,
        nvidia=nvidia_verify,
    )


@pytest.fixture(scope="module")
def instances() -> InstanceEvidence:
    return InstanceEvidence()


def test_every_instance_bound_and_signed_is_fully_attested(instances):
    seen: dict = {}
    client = shaped(lambda nonce: (200, instances.body(nonce)), seen=seen)
    result = client.attest()
    assert result.level == "full" and result.instances == 2 and result.gpus == 16
    assert "signs no replies" in result.detail
    # NVIDIA's verdict is sought for the nonce each quote binds, as measured live.
    assert seen["nras_nonces"] == seen["verified_nonces"] == [instances.gpu_nonce] * 2
    with pytest.raises(ProviderError, match="signs no replies"):
        client.verify_reply("chatcmpl-1")


@pytest.mark.parametrize(
    "shape, refusal",
    [
        ({"signed_nonce": "00" * 32}, "didn't sign our nonce"),
        ({"bind": False}, "quote doesn't bind"),
        ({"other_quote": True}, "signed a different quote"),
        ({"failed": ("inst-9",)}, "1 of 3 .* couldn't be attested"),
    ],
)
def test_an_instance_that_doesnt_hold_refuses_them_all(instances, shape, refusal):
    with pytest.raises(TeeRefused, match=refusal):
        shaped(lambda nonce: (200, instances.body(nonce, **shape))).attest()


def test_an_instance_signed_by_another_key_is_refused(instances):
    other = InstanceEvidence(1).keys[0]
    with pytest.raises(TeeRefused, match="didn't sign its evidence"):
        shaped(lambda nonce: (200, instances.body(nonce, sign_with=other))).attest()


def test_an_attestation_relayed_for_the_relays_own_nonce_is_refused():
    body = {
        "attestation_type": "chutes",
        "nonce": "f9" * 32,
        "all_attestations": [{"intel_quote": "", "gpu_evidence": []}],
    }
    with pytest.raises(TeeRefused, match=r"relayed by its provider \(chutes\).*nonce the relay"):
        shaped(lambda nonce: (200, body), model="TEE/glm-5.1").attest()


def test_nano_gpts_reason_is_given_and_tinfoil_models_point_to_their_twin():
    reason = "Fresh nonce-bound attestation is not available for Tinfoil via this endpoint."
    with pytest.raises(TeeRefused) as caught:
        shaped(lambda nonce: (400, {"error": reason}), model="TEE/gemma4-31b").attest()
    assert reason in str(caught.value) and "private/ version" in str(caught.value)


def test_a_provider_outage_says_so():
    with pytest.raises(TeeRefused, match="Unable to reach RedPill.*provider's side.*again"):
        shaped(
            lambda nonce: (502, {"error": "Unable to reach RedPill attestation service"})
        ).attest()


def test_an_unknown_shape_names_what_it_held():
    with pytest.raises(TeeRefused, match="doesn't recognise .*holds novel"):
        shaped(lambda nonce: (200, {"novel": 1})).attest()


def test_intels_collateral_that_arrived_but_cant_be_read_refuses(real):
    """An outage is "partial"; an answer that doesn't parse is not an outage."""
    from sealedlore.providers import dcap

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not a CRL</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(dcap.DcapError, match="could not be read"):
        dcap.verify(bytes.fromhex(real["intel_quote"]), client, now=at(real))
