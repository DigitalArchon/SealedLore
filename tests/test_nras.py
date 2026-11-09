"""NVIDIA's side of a TEE/ model's attestation (providers/nras.py) when NVIDIA's
signing keys can't be had: only the signature goes unchecked. Ported from DA Vibe
Manager's and DA Assistant's tests of their copies of the module (Nov 2026). The real
capture's other checks are in test_tee.py."""

# ruff: noqa: F811 - the real capture's fixture comes from test_tee

from __future__ import annotations

import base64
import hashlib
import json
import time

import httpx
import pytest

import sealedlore.providers.tee as tee_module
from sealedlore.providers import nras
from sealedlore.providers.ethsig import address_of
from sealedlore.providers.tee import NRAS_URL, TeeClient, TeeRefused
from tests.test_tee import KEY, UP_TO_DATE, fake_quote, nvidia, real  # noqa: F401

NO_KEYS = {"no": "keys"}


def _part(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_without_nvidias_keys_all_but_its_signature_is_still_checked(real, monkeypatch):
    """NVIDIA's keys can't be fetched: its verdict counts as unchecked (a partial
    attestation), but evidence made for another nonce (replayed) or out of date is
    refused all the same; and keys fetched before, though more than an hour ago, are
    still NVIDIA's: used when no fresh ones can be had."""
    with pytest.raises(nras.KeysUnavailable):
        nvidia(real, jwks=NO_KEYS)
    with pytest.raises(nras.NrasError, match="nonce"):
        nvidia(real, jwks=NO_KEYS, nonce="00" * 32)
    with pytest.raises(nras.NrasError, match="validity window"):
        nvidia(real, jwks=NO_KEYS, now=real["now"] + 86400)
    assert nvidia(real) == 8  # fetched now: kept
    monkeypatch.setitem(nras._keys, "at", 0.0)  # long ago
    assert nvidia(real, jwks=NO_KEYS) == 8


def test_without_nvidias_keys_each_gpus_verdict_is_still_checked(real):
    """Only the overall verdict, its nonce and its time were read without NVIDIA's
    keys: a GPU whose token said its measurements failed, or a GPU token the overall
    one doesn't list, passed as "unchecked"."""
    label, overall = real["nras_answer"][0]
    tokens = dict(real["nras_answer"][1])
    gpu = sorted(tokens)[0]
    header, _claims_part, signature = tokens[gpu].split(".")
    tokens[gpu] = f"{header}.{_part({**_claims(tokens[gpu]), 'measres': 'fail'})}.{signature}"
    # Listed by the overall verdict, digest and all: only the signatures, which go
    # unchecked here, would have told it from NVIDIA's.
    verdict = _claims(overall)
    verdict["submods"][gpu][1][1] = hashlib.sha256(tokens[gpu].encode()).hexdigest()
    o_header, _o_claims, o_signature = overall.split(".")
    forged = [[label, f"{o_header}.{_part(verdict)}.{o_signature}"], tokens]
    with pytest.raises(nras.NrasError, match=f"{gpu} failing: measurements"):
        nvidia(real, forged, jwks=NO_KEYS)
    unlisted = [[label, overall], tokens]
    with pytest.raises(nras.NrasError, match="not the one NVIDIA's verdict lists"):
        nvidia(real, unlisted, jwks=NO_KEYS)


def test_without_nvidias_keys_a_verdict_that_says_no_is_refused(real):
    label, overall = real["nras_answer"][0]
    header, _claims_part, signature = overall.split(".")
    no = {**_claims(overall), "x-nvidia-overall-att-result": False}
    answer = [[label, f"{header}.{_part(no)}.{signature}"], real["nras_answer"][1]]
    with pytest.raises(nras.NrasError, match="NVIDIA rejected the GPU evidence"):
        nvidia(real, answer, jwks=NO_KEYS)


def test_keys_fetched_before_are_used_when_fresh_ones_cannot_be_had(real):
    """Older than an hour, but NVIDIA's, fetched over TLS from its own service: a
    signature checked against them beats one not checked at all."""
    assert nvidia(real) == 8  # fetched now, and kept
    nras._keys["at"] = nras.since_boot() - nras.KEYS_TTL - 1
    assert nvidia(real, jwks=NO_KEYS) == 8
    label, overall = real["nras_answer"][0]
    forged = overall[:-4] + ("AAAA" if not overall.endswith("AAAA") else "BBBB")
    with pytest.raises(nras.NrasError, match="signature does not verify"):
        nvidia(real, [[label, forged], real["nras_answer"][1]], jwks=NO_KEYS)


# --- the attestation, with NVIDIA's keys out of reach -------------------------------------


def keys_out_of_reach(real: dict, *, answer=None, asked: list[str]) -> httpx.Client:
    """nano-gpt with a quote binding our nonce (Intel's check stood in for), and
    NVIDIA's service answering with the verdict captured in tests/data, made for the
    nonce of that request, its signing keys out of reach."""
    address = address_of(KEY).lower()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        if "/tee/attestation" in url:
            nonce = request.url.params["nonce"]
            report = bytes.fromhex(address[2:]) + bytes(12) + bytes.fromhex(nonce)
            return httpx.Response(
                200,
                json={
                    "signing_address": address,
                    "intel_quote": fake_quote(report),
                    "nvidia_payload": json.dumps({"evidence_list": [{"evidence": "e"}]}),
                },
            )
        if url == NRAS_URL:
            return httpx.Response(200, json=answer or real["nras_answer"])
        if url == nras.JWKS_URL:
            asked.append(url)
            return httpx.Response(503)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def attest_with_nvidias_keys_out_of_reach(real, monkeypatch, nonce, now, answer=None):
    """The attestation, made with `nonce`, at `now`, of a TEE model whose GPU evidence
    NVIDIA answered for with the captured verdict."""
    asked: list[str] = []
    client = TeeClient(
        "https://nano-gpt.com/api/v1",
        "k",
        "TEE/x",
        client=keys_out_of_reach(real, answer=answer, asked=asked),
        intel=lambda raw, session: UP_TO_DATE,
        nvidia=lambda a, n, session: nras.verify(a, n, session, now=now),
    )
    monkeypatch.setattr(tee_module.secrets, "token_hex", lambda _n: nonce)
    try:
        return client.attest()
    finally:
        assert asked, "NVIDIA's keys were not looked for"


def test_with_nvidias_keys_out_of_reach_a_verdict_for_another_nonce_is_refused(real, monkeypatch):
    """Without its keys NVIDIA's signatures can't be checked, and only the overall
    verdict used to be read: one made for an earlier request passed as "unchecked".
    The answer comes over TLS from NVIDIA itself, so its nonce and its time are held
    to even then."""
    with pytest.raises(
        TeeRefused,
        match="GPU attestation failed: NVIDIA's verdict was not made for this request's nonce",
    ):
        attest_with_nvidias_keys_out_of_reach(real, monkeypatch, "0d" * 32, real["now"])


def test_with_nvidias_keys_out_of_reach_an_old_verdict_is_refused(real, monkeypatch):
    with pytest.raises(
        TeeRefused, match="GPU attestation failed: a token is outside its validity window"
    ):
        attest_with_nvidias_keys_out_of_reach(real, monkeypatch, real["nonce"], real["now"] + 86400)


def test_with_nvidias_keys_out_of_reach_a_verdict_that_says_no_is_refused(real, monkeypatch):
    label, overall = real["nras_answer"][0]
    header, _claims_part, signature = overall.split(".")
    no = {**_claims(overall), "x-nvidia-overall-att-result": False}
    answer = [[label, f"{header}.{_part(no)}.{signature}"], real["nras_answer"][1]]
    with pytest.raises(TeeRefused, match="GPU attestation failed: NVIDIA rejected"):
        attest_with_nvidias_keys_out_of_reach(
            real, monkeypatch, real["nonce"], real["now"], answer=answer
        )


def test_with_nvidias_keys_out_of_reach_a_current_verdict_is_partial(real, monkeypatch):
    result = attest_with_nvidias_keys_out_of_reach(real, monkeypatch, real["nonce"], real["now"])
    assert result.level == "partial" and "NVIDIA signature unchecked" in result.shortfalls
    assert "so NVIDIA's verdict is unchecked" in result.detail
    assert result.gpus == 0  # vouched for by nobody whose signature was checked


# --- a clock that counts the time asleep --------------------------------------------------


def test_nvidias_keys_are_fetched_again_after_a_sleep(monkeypatch):
    """They were aged by time.monotonic, which stops while the machine is suspended."""
    if not hasattr(time, "CLOCK_BOOTTIME"):
        pytest.skip("no CLOCK_BOOTTIME here")
    clock = {"boot": 1000.0}
    real_gettime = time.clock_gettime
    monkeypatch.setattr(
        time,
        "clock_gettime",
        lambda which: clock["boot"] if which == time.CLOCK_BOOTTIME else real_gettime(which),
    )
    monkeypatch.setattr(time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, json={"keys": [{"kid": "k1"}]})

    session = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(2):
        assert "k1" in nras._jwks(session)
    assert len(fetched) == 1
    clock["boot"] += 2 * 3600
    nras._jwks(session)
    assert len(fetched) == 2
