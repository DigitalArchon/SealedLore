"""Chutes' end-to-end encryption, the pure parts (providers/chutes_e2ee.py):
ML-KEM-768 against NIST's vectors, the request and reply framing as round
trips with a key of our own standing in for an instance (Chutes' own client
is the reference for the bytes), every hostile relay the design names, the
key binding, the measurements, and the model list."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from sealedlore.engine.catalog import model_info
from sealedlore.engine.pricing import parse_price
from sealedlore.providers import chutes_e2ee
from sealedlore.providers.base import ProviderError
from sealedlore.providers.chutes_e2ee import (
    CIPHERTEXT_BYTES,
    INFO_REQUEST,
    INFO_RESPONSE,
    INFO_STREAM,
    MLKEM_MISSING,
    NONCE_BYTES,
    RESPONSE_KEY_FIELD,
    SealBroken,
    SealError,
    StreamOpener,
    binding,
    confidential_models,
    derive_key,
    gunzip,
    key_bound,
    measured,
    open_reply,
    published_measurements,
    seal_request,
)

mlkem = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.mlkem")
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

DATA = Path(__file__).parent / "data"
ACVP = json.loads((DATA / "mlkem768_acvp.json").read_text(encoding="utf-8"))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- ML-KEM-768 against NIST's ACVP vectors ---------------------------------------------


@pytest.mark.parametrize("case", ACVP["keyGen"], ids=lambda c: f"tc{c['tcId']}")
def test_a_key_made_from_nists_seed_is_nists_key(case):
    """FIPS 203 key generation from its seed d ‖ z gives ACVP's encapsulation key."""
    key = mlkem.MLKEM768PrivateKey.from_seed_bytes(bytes.fromhex(case["d"] + case["z"]))
    assert key.public_key().public_bytes_raw() == bytes.fromhex(case["ek"])


@pytest.mark.parametrize("case", ACVP["encapsulationKeyCheck"], ids=lambda c: f"tc{c['tcId']}")
def test_an_instance_key_nist_rejects_is_refused(case):
    """ACVP's encapsulation key check (FIPS 203's modulus check): a key whose
    coefficients are out of range is never encapsulated to."""
    encoded = b64(bytes.fromhex(case["ek"]))
    if case["testPassed"]:
        assert chutes_e2ee.public_key(encoded) is not None
    else:
        with pytest.raises(SealError, match="isn't a valid ML-KEM-768 key"):
            chutes_e2ee.public_key(encoded)


@pytest.mark.parametrize("case", ACVP["keyGen"], ids=lambda c: f"tc{c['tcId']}")
def test_decapsulation_rejects_a_ciphertext_implicitly_as_fips_203_says(case):
    """Decapsulation of any ciphertext not made for the key gives J(z ‖ c),
    SHAKE-256 of the seed's z and the ciphertext (FIPS 203, Algorithm 18):
    the vectors' z is the key's own. The valid path is the round trip."""
    key = mlkem.MLKEM768PrivateKey.from_seed_bytes(bytes.fromhex(case["d"] + case["z"]))
    forged = os.urandom(CIPHERTEXT_BYTES)
    rejected = hashlib.shake_256(bytes.fromhex(case["z"]) + forged).digest(32)
    assert key.decapsulate(forged) == rejected
    secret, ciphertext = key.public_key().encapsulate()
    assert len(ciphertext) == CIPHERTEXT_BYTES and key.decapsulate(ciphertext) == secret


def test_the_nist_file_names_where_it_came_from():
    assert "usnistgov/ACVP-Server" in ACVP["source"] and len(ACVP["commit"]) == 40


# --- an instance, as Chutes' workload is (chutes/entrypoint/run.py) ----------------------


class Instance:
    """Our own ML-KEM key standing in for an instance's, doing what its
    workload does with a sealed request and its reply."""

    def __init__(self) -> None:
        self.key = mlkem.MLKEM768PrivateKey.generate()
        self.pubkey = b64(self.key.public_key().public_bytes_raw())

    def open_request(self, blob: bytes) -> dict:
        ciphertext, nonce, sealed = blob[:1088], blob[1088:1100], blob[1100:]
        key = derive_key(self.key.decapsulate(ciphertext), ciphertext, INFO_REQUEST)
        return json.loads(gzip.decompress(ChaCha20Poly1305(key).decrypt(nonce, sealed, None)))

    @staticmethod
    def seal_reply(body: dict, response_pk: str, *, raw: bytes | None = None) -> bytes:
        client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
        secret, ciphertext = client.encapsulate()
        key = derive_key(secret, ciphertext, INFO_RESPONSE)
        nonce = os.urandom(12)
        text = raw if raw is not None else gzip.compress(json.dumps(body).encode())
        return ciphertext + nonce + ChaCha20Poly1305(key).encrypt(nonce, text, None)


REPLY = {
    "id": "chatcmpl-1",
    "choices": [{"message": {"role": "assistant", "content": "Hello."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


def test_a_request_and_its_reply_go_round_sealed_as_chutes_lays_them_out():
    instance = Instance()
    body = {"model": "Qwen/Qwen3.8-27B-TEE", "messages": [{"role": "user", "content": "Hi"}]}
    sealed = seal_request(instance.pubkey, body)
    # ct ‖ nonce ‖ ciphertext ‖ tag, as chutes-api's scripts/test_e2e_client.py builds it.
    assert len(sealed.blob) > CIPHERTEXT_BYTES + NONCE_BYTES + 16
    opened = instance.open_request(sealed.blob)
    response_pk = opened.pop(RESPONSE_KEY_FIELD)
    assert opened == body, "the body as it was, our response key beside it"
    assert len(base64.b64decode(response_pk)) == 1184
    assert response_pk == b64(sealed.response_key.public_key().public_bytes_raw())
    assert open_reply(Instance.seal_reply(REPLY, response_pk), sealed.response_key) == REPLY
    # A fresh response key, encapsulation and nonce every time.
    again = seal_request(instance.pubkey, body)
    assert again.blob[:1100] != sealed.blob[:1100]
    assert instance.open_request(again.blob)[RESPONSE_KEY_FIELD] != response_pk


@pytest.mark.parametrize("where", [0, 1087, 1088, 1099, 1100, -17, -1], ids=str)
def test_a_reply_changed_anywhere_is_refused(where):
    instance = Instance()
    sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
    pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
    blob = bytearray(Instance.seal_reply(REPLY, pk))
    blob[where] ^= 0x01
    with pytest.raises(SealBroken):
        open_reply(bytes(blob), sealed.response_key)


def test_a_reply_sealed_to_another_key_or_cut_short_is_refused():
    instance = Instance()
    sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
    other = seal_request(instance.pubkey, {"model": "m", "messages": []})
    pk = instance.open_request(other.blob)[RESPONSE_KEY_FIELD]
    with pytest.raises(SealBroken):
        open_reply(Instance.seal_reply(REPLY, pk), sealed.response_key)
    mine = Instance.seal_reply(REPLY, instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD])
    for cut in (0, 1088, 1115, len(mine) - 1):
        with pytest.raises(SealBroken):
            open_reply(mine[:cut], sealed.response_key)


def test_an_instance_key_that_isnt_one_is_refused_before_anything_is_sealed():
    good = Instance().pubkey
    raw = base64.b64decode(good)
    for wrong, why in (
        (b64(raw[:-1]), "1183 bytes"),
        (b64(raw + b"\0"), "1185 bytes"),
        (good[:-4] + "!!!=", "isn't base64"),
        (" " + good, "isn't base64"),
        (None, "isn't base64"),
        ("é" * 8, "isn't base64"),
    ):
        with pytest.raises(SealError, match=why):
            seal_request(wrong, {"model": "m"})
    with pytest.raises(SealError, match="isn't a JSON object"):
        seal_request(good, {})
    with pytest.raises(SealError, match="can't be written as JSON"):
        seal_request(good, {"model": float("nan")})


def test_a_reply_that_opens_to_too_much_is_refused_before_it_is_all_inflated():
    instance = Instance()
    sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
    pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
    bomb = gzip.compress(b" " * (chutes_e2ee.MAX_DECODED_BYTES + 1), mtime=0)
    assert len(bomb) < 64 * 1024, "a few kilobytes that open to 32 MB"
    with pytest.raises(SealError, match="more than 32 MB"):
        open_reply(Instance.seal_reply({}, pk, raw=bomb), sealed.response_key)
    assert gunzip(gzip.compress(b"x" * 100), most=100) == b"x" * 100
    with pytest.raises(SealError, match="more than"):
        gunzip(gzip.compress(b"x" * 101), most=100)


@pytest.mark.parametrize(
    "raw, why",
    [
        (gzip.compress(b'{"a": 1}')[:-3], "wasn't whole"),
        (gzip.compress(b'{"a": 1}') + b"more", "wasn't whole"),
        (gzip.compress(b'{"a": 1}') + gzip.compress(b"{}"), "wasn't whole"),
        (b"not gzip at all", "couldn't be read"),
        (gzip.compress(b"[1, 2]"), "isn't a JSON object"),
        (gzip.compress(b"hello"), "isn't JSON"),
        (gzip.compress(b"[" * 300 + b"]" * 300), "nested too deeply"),
    ],
    ids=["cut", "trailing", "two members", "not gzip", "a list", "not json", "deep"],
)
def test_what_a_sealed_reply_holds_must_be_one_whole_json_object(raw, why):
    instance = Instance()
    sealed = seal_request(instance.pubkey, {"model": "m", "messages": []})
    pk = instance.open_request(sealed.blob)[RESPONSE_KEY_FIELD]
    with pytest.raises(SealError, match=why) as caught:
        open_reply(Instance.seal_reply({}, pk, raw=raw), sealed.response_key)
    assert not isinstance(caught.value, SealBroken), "the seal held: the instance sent it"


def test_without_ml_kem_chutes_is_refused_in_words(monkeypatch):
    from cryptography.exceptions import UnsupportedAlgorithm

    def unsupported(*_args):
        raise UnsupportedAlgorithm("ML-KEM-768 is not supported by this backend")

    pubkey = Instance().pubkey
    monkeypatch.setattr(chutes_e2ee, "_available", None)
    monkeypatch.setattr(mlkem.MLKEM768PrivateKey, "generate", staticmethod(unsupported))
    assert chutes_e2ee.mlkem_available() is False
    assert chutes_e2ee.mlkem_refusal() == MLKEM_MISSING
    with pytest.raises(SealError, match="OpenSSL 3.5") as caught:
        seal_request(pubkey, {"model": "m"})
    assert "nothing was sent" in str(caught.value)
    monkeypatch.setattr(chutes_e2ee, "_available", None)
    monkeypatch.undo()
    assert chutes_e2ee.mlkem_available() is True and chutes_e2ee.mlkem_refusal() is None


# --- a streamed reply, as the workload frames it ------------------------------------------


class Stream:
    """A stream sealed as Chutes' workload seals one: the key exchange, then
    each piece of the model server's own SSE bytes sealed alone."""

    def __init__(self, response_pk: str) -> None:
        client = mlkem.MLKEM768PublicKey.from_public_bytes(base64.b64decode(response_pk))
        secret, self.ciphertext = client.encapsulate()
        self.key = derive_key(secret, self.ciphertext, INFO_STREAM)

    def init(self) -> str:
        return "data: " + json.dumps({"e2e_init": b64(self.ciphertext)})

    def piece(self, text: str, *, key: bytes | None = None) -> str:
        nonce = os.urandom(12)
        sealed = ChaCha20Poly1305(key or self.key).encrypt(nonce, text.encode(), None)
        return "data: " + json.dumps({"e2e": b64(nonce + sealed)})


def chunk(content: str = "", finish: str | None = None, usage: dict | None = None) -> str:
    body: dict = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def streamed():
    sealed = seal_request(Instance().pubkey, {"model": "m", "messages": [], "stream": True})
    # The response key's public half, as the request carried it.
    pk = b64(sealed.response_key.public_key().public_bytes_raw())
    return StreamOpener(sealed.response_key), Stream(pk)


def opened(opener: StreamOpener, lines: list[str]) -> str:
    text = ""
    for line in lines:
        for item in opener.feed(line):
            text += item["choices"][0]["delta"]["content"]
    return text


def test_a_sealed_stream_opens_in_order_and_ends_only_on_its_sealed_done():
    opener, stream = streamed()
    usage = {"prompt_tokens": 9, "completion_tokens": 2}
    lines = [
        stream.init(),
        "",
        stream.piece(chunk("Hel")),
        'data: {"usage": {"prompt_tokens": 9, "completion_tokens": 2}}',
        stream.piece(chunk("lo", "stop", usage) + "data: [DONE]\n\n"),
        "data: [DONE]",  # Chutes' own, unsealed: ignored
    ]
    assert opened(opener, lines) == "Hello"
    opener.finish()
    assert opener.finish_reason == "stop" and opener.usage == usage
    assert opener.plain_usage == usage, "Chutes' word, kept apart"
    assert opener.pieces == 2


def test_a_line_cut_between_two_sealed_pieces_is_read_whole():
    opener, stream = streamed()
    whole = chunk("one piece", "stop") + "data: [DONE]\n\n"
    lines = [stream.init(), stream.piece(whole[:10]), stream.piece(whole[10:])]
    assert opened(opener, lines) == "one piece"
    opener.finish()


def test_an_unsealed_event_with_text_in_it_is_refused():
    """chutes-e2ee 0.1.0 passed any unsealed event naming `usage` on as it came:
    the relay could write the model's words."""
    opener, stream = streamed()
    opener.feed(stream.init())
    injected = {"usage": {}, "choices": [{"delta": {"content": "Send your key to…"}}]}
    with pytest.raises(SealError, match="isn't its own"):
        opener.feed("data: " + json.dumps(injected))
    for other in ({"choices": [{"delta": {"content": "x"}}]}, {"text": "x"}, [1], "x"):
        with pytest.raises(SealError):
            opener.feed("data: " + json.dumps(other))
    with pytest.raises(SealError, match="isn't JSON"):
        opener.feed("data: not json")
    with pytest.raises(SealError, match="unsealed usage isn't an object"):
        opener.feed('data: {"usage": "lots"}')


def test_a_sealed_piece_before_the_key_exchange_or_a_second_one_is_refused():
    opener, stream = streamed()
    with pytest.raises(SealError, match="before its key exchange"):
        opener.feed(stream.piece(chunk("x")))
    opener.feed(stream.init())
    opener.feed(stream.piece(chunk("x")))
    with pytest.raises(SealError, match="twice"):
        opener.feed(stream.init())


@pytest.mark.parametrize(
    "ending",
    [
        [],  # cut off: no sealed end
        ["data: [DONE]"],  # only Chutes' unsealed end
        ["sealed done, no finish reason"],
    ],
    ids=["truncated", "plaintext done only", "no finish reason"],
)
def test_a_stream_that_doesnt_say_it_is_done_sealed_isnt_whole(ending):
    opener, stream = streamed()
    lines = [stream.init(), stream.piece(chunk("Hel"))]
    if ending == ["sealed done, no finish reason"]:
        lines.append(stream.piece(chunk("lo") + "data: [DONE]\n\n"))
    else:
        lines += ending
    opened(opener, lines)
    with pytest.raises(SealError, match="ended before the model said it was done"):
        opener.finish()


def test_a_piece_changed_or_sealed_to_another_key_or_after_the_end_is_refused():
    opener, stream = streamed()
    opener.feed(stream.init())
    line = stream.piece(chunk("x"))
    event = json.loads(line[6:])
    raw = bytearray(base64.b64decode(event["e2e"]))
    raw[-1] ^= 1
    with pytest.raises(SealBroken):
        opener.feed("data: " + json.dumps({"e2e": b64(bytes(raw))}))
    with pytest.raises(SealBroken):
        opener.feed(stream.piece(chunk("x"), key=os.urandom(32)))
    with pytest.raises(SealBroken, match="too short"):
        opener.feed("data: " + json.dumps({"e2e": b64(b"\0" * 27)}))
    opener.feed(stream.piece(chunk("x", "stop") + "data: [DONE]\n\n"))
    with pytest.raises(SealError, match="after the model's end"):
        opener.feed(stream.piece(chunk("more")))


def test_chutes_relays_error_is_said_as_its_unauthenticated_words():
    opener, stream = streamed()
    opener.feed(stream.init())
    with pytest.raises(ProviderError) as caught:
        opener.feed('data: {"e2e_error": "Instance\\u202e gone\\nnow"}')
    assert str(caught.value) == "Chutes' relay said (unauthenticated): Instance gone now"
    assert not isinstance(caught.value, SealError)


def test_the_model_servers_own_error_inside_the_seal_is_said():
    opener, stream = streamed()
    opener.feed(stream.init())
    with pytest.raises(ProviderError, match="model's server reported an error: too long"):
        opener.feed(stream.piece('data: {"error": {"message": "too long"}}\n\n'))


# --- the key binding ------------------------------------------------------------------------


def test_the_binding_is_sha256_of_the_nonce_and_the_key_as_chutes_writes_them():
    """chutes verify.py: hashlib.sha256((nonce + self._e2e_pubkey).encode()).hexdigest(),
    written by sek8s as the report data's first 32 bytes (hex), the TLS key's
    hash after it."""
    nonce, pubkey = "ab" * 32, Instance().pubkey
    expected = bytes.fromhex(hashlib.sha256((nonce + pubkey).encode()).hexdigest())
    assert binding(nonce, pubkey) == expected
    report = expected + os.urandom(32)
    assert key_bound(report, nonce, pubkey)
    assert not key_bound(report, "cd" * 32, pubkey), "another nonce"
    assert not key_bound(report, nonce, Instance().pubkey), "another key"
    assert not key_bound(report, nonce.upper(), pubkey), "the nonce as sent, never recased"
    assert not key_bound(bytes(32) + expected, nonce, pubkey), "the second half is the TLS key's"
    assert not key_bound(report[:63], nonce, pubkey)


# --- measurements ---------------------------------------------------------------------------

EVIDENCE = json.loads((DATA / "chutes_evidence.json").read_text(encoding="utf-8"))


def registers(item: dict) -> tuple[bytes, tuple[bytes, ...]]:
    body = base64.b64decode(item["quote"])[48:632]
    return body[136:184], tuple(body[o : o + 48] for o in (328, 376, 424, 472))


@pytest.mark.parametrize("shape", ["measurements", "measurements_runtime"])
def test_each_real_instances_registers_are_one_of_chutes_published_sets(shape):
    sets = published_measurements(EVIDENCE[shape])
    for item in EVIDENCE["evidence"]["evidence"]:
        mrtd, rtmrs = registers(item)
        found = measured(mrtd, rtmrs, sets)
        assert found is not None and found.version in ("1.4.1", "1.5.0")
        changed = (rtmrs[0], rtmrs[1], rtmrs[2], bytes(48))
        assert measured(mrtd, changed, sets) is None, "RTMR3 counts too"
        assert measured(bytes(48), rtmrs, sets) is None


def test_a_published_list_that_cant_be_read_is_refused_and_bad_sets_are_left_out():
    good = EVIDENCE["measurements"][0]
    broken = [{**good, "rtmr2": "zz" * 48}, {**good, "mrtd": "AB"}, "x", {**good}]
    assert len(published_measurements(broken)) == 1
    with pytest.raises(SealError, match="aren't a list"):
        published_measurements({"sets": []})
    with pytest.raises(SealError, match="hold no set"):
        published_measurements([{"mrtd": "AB"}])


# --- the model list ------------------------------------------------------------------------


def test_only_confidential_models_are_offered_priced_per_million():
    listing = json.loads((DATA / "chutes_models.json").read_text(encoding="utf-8"))
    models = confidential_models(listing)
    assert len(models) == 11 and all(m["confidential_compute"] is True for m in models.values())
    assert "zai-org/GLM-5.3" not in models, "plaintext at Chutes: never offered"
    qwen = models["Qwen/Qwen3.8-27B-TEE"]
    assert qwen["chute_id"] == "9ecb73cc-909b-5334-9cd8-74985945cff5"
    price = parse_price(qwen)
    assert (price.prompt, price.completion) == (0.039, 1.25)
    assert price.cache_read == pytest.approx(0.0039)
    info = model_info("Qwen/Qwen3.8-27B-TEE", qwen)
    assert (info.context_length, info.max_output_tokens) == (262144, 65536)
    assert info.reasoning and info.vision and info.prompt_price == 0.039


def test_a_model_is_offered_only_when_chutes_says_so_plainly():
    base = {"id": "a/b-TEE", "chute_id": "9ecb73cc-909b-5334-9cd8-74985945cff5"}
    for listed in (
        {**base, "confidential_compute": "true"},
        {**base, "confidential_compute": 1},
        {**base, "confidential_compute": True, "chute_id": "../e2e/invoke"},
        {**base, "confidential_compute": True, "chute_id": None},
        {**base, "confidential_compute": True, "id": ""},
    ):
        assert confidential_models({"data": [listed]}) == {}
    priced = confidential_models(
        {
            "data": [
                {**base, "confidential_compute": True, "pricing": {"prompt": -1, "completion": 2}}
            ]
        }
    )
    assert "pricing" not in priced["a/b-TEE"]
    assert confidential_models({"data": "nothing"}) == {} == confidential_models([])
