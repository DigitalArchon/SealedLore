"""Real Chutes evidence, verified offline at the moment it was captured, as
tests/data/tee_attestation.json is (tests/test_tee.py): three instances of
Qwen/Qwen3.8-27B-TEE for our nonce, keyless, with Intel's collateral and
NVIDIA's answers and keys as SealedLore's own checks fetched them then
(tests/data/chutes_evidence.json says how). tee.py's per-instance check
(`TeeClient.check_instance`) hands back each quote's report data and
registers, which the Chutes provider binds to its key and holds to Chutes'
published measurements."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sealedlore.providers import dcap, nras
from sealedlore.providers.chutes_e2ee import key_bound, measured, published_measurements
from sealedlore.providers.tee import NRAS_URL, QuoteFacts, TeeClient, TeeRefused

FIXTURE = Path(__file__).parent / "data" / "chutes_evidence.json"


@pytest.fixture
def real():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})
    yield fixture
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})


def checks_server(fixture: dict, *, intel_status: int = 200) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(checks_handler(fixture, intel_status)))


def checks_handler(fixture: dict, intel_status: int = 200):
    """Intel's collateral, NVIDIA's answers (by the nonce each was asked for)
    and NVIDIA's keys, exactly as they came at the capture."""
    answers = {item["nonce"]: item["answer"] for item in fixture["nras"]}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        params = dict(request.url.params)
        if url == NRAS_URL:
            nonce = json.loads(request.content)["nonce"]
            return (
                httpx.Response(200, json=answers[nonce])
                if nonce in answers
                else (httpx.Response(400, json={"error": "not this nonce"}))
            )
        if url == nras.JWKS_URL:
            return httpx.Response(200, json=fixture["jwks"])
        for item in fixture["collateral"]:
            if item["url"] == url and (item["params"] or {}) == params:
                if intel_status != 200:
                    return httpx.Response(intel_status)
                return httpx.Response(
                    item["status"], content=base64.b64decode(item["body"]), headers=item["headers"]
                )
        return httpx.Response(404)

    return handler


def checker(fixture: dict, **server) -> TeeClient:
    """tee.py's checks on the capture, Intel's and NVIDIA's at its moment."""
    moment = datetime.fromtimestamp(fixture["now"], UTC)
    return TeeClient(
        "https://api.chutes.ai",
        "",
        fixture["model"],
        client=checks_server(fixture, **server),
        intel=lambda raw, session: dcap.verify(raw, session, now=moment),
        nvidia=lambda answer, nonce, session: nras.verify(
            answer, nonce, session, now=fixture["now"]
        ),
    )


def test_each_real_instance_holds_up_to_intel_and_nvidia_and_hands_back_its_quote(real):
    client = checker(real)
    sets = published_measurements(real["measurements"])
    for item in real["evidence"]["evidence"]:
        check = client.check_instance(item, real["nonce"])
        assert (check.status, check.gpus, check.shortfalls) == ("UpToDate", 1, [])
        assert "NVIDIA's signed verdict vouches for 1 GPU" in "; ".join(check.notes)
        facts = check.quote
        assert isinstance(facts, QuoteFacts) and len(facts.report_data) == 64
        # The second half is the certificate key's hash (checked); the first is
        # no nonce of ours but what Chutes derives from it and the instance's key.
        assert facts.report_data[:32] != bytes.fromhex(real["nonce"])
        assert measured(facts.mrtd, facts.rtmrs, sets) is not None
        # The key isn't in the evidence (only discovery, with an account, lists
        # it): no key of ours binds.
        assert not key_bound(facts.report_data, real["nonce"], "A" * 1580 + "==")


def test_the_registers_are_read_where_intel_puts_them(real):
    item = real["evidence"]["evidence"][0]
    raw = base64.b64decode(item["quote"])
    facts = QuoteFacts.of(dcap.parse_quote(raw))
    # As Chutes reads them (chutes-api api/server/quote.py): the TD report from byte 48.
    report = raw[48:632]
    assert facts.mrtd == report[136:184] and facts.report_data == report[520:584]
    assert facts.rtmrs == tuple(report[o : o + 48] for o in (328, 376, 424, 472))


def test_the_real_evidence_for_another_nonce_is_refused(real):
    item = real["evidence"]["evidence"][0]
    with pytest.raises(TeeRefused, match="didn't sign our nonce"):
        checker(real).check_instance(item, hashlib.sha256(b"another").hexdigest())


def test_real_evidence_changed_is_refused(real):
    item = dict(real["evidence"]["evidence"][1])
    quote = bytearray(base64.b64decode(item["quote"]))
    quote[48 + 520] ^= 1  # the report data's first half
    item["quote"] = base64.b64encode(bytes(quote)).decode()
    with pytest.raises(TeeRefused, match="signed a different quote"):
        checker(real).check_instance(item, real["nonce"])
    other = dict(real["evidence"]["evidence"][1])
    other["signature"] = real["evidence"]["evidence"][0]["signature"]
    with pytest.raises(TeeRefused, match="didn't sign its evidence"):
        checker(real).check_instance(other, real["nonce"])


def test_evidence_in_no_shape_is_refused_by_what_it_raised(real):
    """Not an AttributeError out of the check, as `attest` refuses it."""
    with pytest.raises(TeeRefused, match=r"isn't in the shape it should be \(AttributeError\)"):
        checker(real).check_instance(["not", "an instance"], real["nonce"])
    item = dict(real["evidence"]["evidence"][0], quote=["a", "list"])
    with pytest.raises(TeeRefused, match="sent unreadable evidence"):
        checker(real).check_instance(item, real["nonce"])


def test_an_attempts_time_holds_the_checks_it_wraps(real):
    import time

    client = checker(real)
    with client.attempt(time.monotonic() - 1):
        with pytest.raises(TeeRefused, match="wasn't done within 3 minutes"):
            client.check_instance(real["evidence"]["evidence"][0], real["nonce"])
    assert client._deadline is None, "and none after it"


def test_the_capture_names_what_it_is_and_holds_no_key(real):
    text = FIXTURE.read_text(encoding="utf-8").lower()
    assert "keyless" in real["source"] and "no key and no prompt" in real["source"].lower()
    assert "authorization" not in text and "bearer" not in text and "cpk_" not in text


def test_the_provider_attests_real_instances_up_to_the_key_it_cant_know(real, monkeypatch):
    """The real capture through ChutesE2EEProvider's attestation (Intel's and
    NVIDIA's real checks at its moment, Chutes' real lists): every check holds
    but the key binding, which no key of ours can pass (only discovery, with an
    account, lists the instance's key); with it stood in for, the three are
    attested from their real evidence, each one's published set named."""
    import secrets as secrets_module

    from cryptography.hazmat.primitives.asymmetric import mlkem

    import sealedlore.providers.chutes_provider as module
    from sealedlore.models.config import ProviderConfig

    moment = datetime.fromtimestamp(real["now"], UTC)
    checks = checks_handler(real)
    items = real["evidence"]["evidence"]

    def chutes(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "llm.chutes.ai":
            listed = {
                "id": real["model"],
                "chute_id": real["chute_id"],
                "confidential_compute": True,
            }
            return httpx.Response(200, json={"data": [listed]})
        if url.path == f"/e2e/instances/{real['chute_id']}":
            made_up = [
                {
                    "instance_id": item["instance_id"],
                    "e2e_pubkey": base64.b64encode(
                        mlkem.MLKEM768PrivateKey.generate().public_key().public_bytes_raw()
                    ).decode(),
                    "nonces": [secrets_module.token_urlsafe(24) for _ in range(10)],
                }
                for item in items
            ]
            return httpx.Response(200, json={"instances": made_up, "nonce_expires_in": 60})
        if url.path == f"/chutes/{real['chute_id']}/evidence":
            return httpx.Response(200, json=real["evidence"])
        if url.path == "/servers/tdx/measurements":
            return httpx.Response(200, json=real["measurements"])
        return checks(request)

    def provider() -> module.ChutesE2EEProvider:
        client = httpx.Client(transport=httpx.MockTransport(chutes))
        return module.ChutesE2EEProvider(
            ProviderConfig(name="Chutes", base_url="https://api.chutes.ai", api_key="cpk_x"),
            client=client,
            listing_client=client,
            checks_client=client,
            intel=lambda raw, session: dcap.verify(raw, session, now=moment),
            nvidia=lambda answer, nonce, session: nras.verify(
                answer, nonce, session, now=real["now"]
            ),
        )

    monkeypatch.setattr(module.secrets, "token_hex", lambda _n: real["nonce"])
    with pytest.raises(TeeRefused, match="doesn't bind the key Chutes lists for it to our nonce"):
        provider().attest(real["model"])
    monkeypatch.setattr(module, "key_bound", lambda *_args: True)
    attestation = provider().attest(real["model"])
    assert attestation.instances == 3 and attestation.level == "partial"
    assert (attestation.tcb_status, attestation.gpus) == ("UpToDate", 3)
    assert "8xpro_6000" in attestation.measurement and "(1.5.0)" in attestation.measurement
