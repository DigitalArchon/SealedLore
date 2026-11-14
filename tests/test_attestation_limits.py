"""Intel's and NVIDIA's checks (providers/dcap.py, providers/nras.py, shared by every
DA app) read their JSON to MAX_NESTING (200) levels at most, a deeper answer refused as one
that can't be read, and their own fetches wait no longer than the attestation's time
leaves (its deadline, on time.monotonic: TeeClient's ATTEMPT_SECONDS), refused in words
once none is left (the DA apps' security standard, its final pass, Nov 14 2026). Nothing
reaches the network. As DA Toolkit's tests/test_attestation_limits.py."""

from __future__ import annotations

import base64
import time

import httpx
import pytest

from sealedlore.providers import dcap, nras
from sealedlore.providers.tee import TeeClient

DEEP = "[" * 300 + "]" * 300


class Recording:
    """A session that answers every GET with `body` and keeps the timeout each was given."""

    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self.body, self.status, self.timeouts = body, status, []

    def get(self, url, params=None, timeout=None, **_kw):
        self.timeouts.append(timeout)
        return httpx.Response(self.status, content=self.body, request=httpx.Request("GET", url))


def test_intels_json_is_read_to_200_levels_and_a_deeper_answer_is_refused():
    assert dcap.MAX_NESTING == 200
    assert dcap._nesting('{"a": ' + "[" * 199 + "]" * 199 + "}") == 200
    assert isinstance(dcap._loads('{"a": ' + "[" * 199 + "]" * 199 + "}"), dict)
    with pytest.raises(dcap.DcapError, match="nested deeper than 200 levels"):
        dcap._loads('{"a": ' + DEEP + "}")
    assert dcap._nesting('{"text": "[[[[[[ in a string ]"}') == 1
    content = ('{"tcbInfo": {"id": "TDX", "deep": ' + DEEP + '}, "signature": "00"}').encode()
    with pytest.raises(dcap.DcapError, match="nested deeper"):
        dcap._signed_json(content, "tcbInfo", "", dcap.intel_root(), dcap.datetime.now(dcap.UTC))


def test_nvidias_json_is_read_to_200_levels_and_a_deeper_token_or_key_set_is_refused(monkeypatch):
    assert nras.MAX_NESTING == 200
    deep = (
        base64.urlsafe_b64encode(('{"alg": "ES384", "x": ' + DEEP + "}").encode())
        .decode()
        .rstrip("=")
    )
    claims = base64.urlsafe_b64encode(b'{"iss": "x"}').decode().rstrip("=")
    signature = base64.urlsafe_b64encode(bytes(96)).decode()
    with pytest.raises(nras.NrasError, match="nested deeper than 200 levels"):
        nras.verify_token(f"{deep}.{claims}.{signature}", Recording(), "nonce", time.time())
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    with pytest.raises(nras.NrasError, match="nested deeper"):
        nras._jwks(Recording(('{"keys": ' + DEEP + "}").encode()))


def test_intels_fetches_wait_only_for_what_is_left_of_the_attestations_time(monkeypatch):
    monkeypatch.setattr(dcap, "_cache", {})
    session = Recording(b"collateral")
    dcap._fetch(session, "https://intel.example/a")
    dcap._fetch(session, "https://intel.example/b", deadline=time.monotonic() + 5)
    assert session.timeouts[0] == dcap.TIMEOUT_SECONDS and 0 < session.timeouts[1] <= 5
    with pytest.raises(
        dcap.DcapError, match="time ran out before Intel's collateral could be fetched"
    ):
        dcap._fetch(session, "https://intel.example/c", deadline=time.monotonic() - 1)
    assert len(session.timeouts) == 2, "nothing fetched once the time is up"
    # cached, it needs no time
    assert (
        dcap._fetch(session, "https://intel.example/a", deadline=time.monotonic() - 1)[0]
        == b"collateral"
    )


def test_nvidias_key_fetch_waits_only_for_what_is_left_and_is_refused_once_none_is(monkeypatch):
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    session = Recording(b'{"keys": [{"kid": "k1"}]}')
    assert set(nras._jwks(session, deadline=time.monotonic() + 5)) == {"k1"}
    assert 0 < session.timeouts[0] <= 5
    monkeypatch.setattr(nras, "_keys", {"at": 0.0, "keys": {}})
    with pytest.raises(
        nras.NrasError, match="time ran out before NVIDIA's signing keys could be fetched"
    ):
        nras._jwks(session, deadline=time.monotonic() - 1)
    assert len(session.timeouts) == 1


def test_the_tee_client_gives_intels_and_nvidias_checks_its_deadline(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        dcap, "verify", lambda raw, session, **kw: seen.update(intel=kw.get("deadline"))
    )
    monkeypatch.setattr(
        nras, "verify", lambda answer, nonce, session, **kw: seen.update(nvidia=kw.get("deadline"))
    )
    client = TeeClient("https://nano-gpt.com/api/v1", "k", "TEE/x", client=httpx.Client())
    client._deadline = 1234.5
    client._intel(b"quote", client._client)
    client._nvidia([], "nonce", client._client)
    assert seen == {"intel": 1234.5, "nvidia": 1234.5}
    client._client.close()
