"""Chutes' end-to-end encryption, the parts that are pure: no network, no
clock, nothing read or written but what each function is given.

Chutes (api.chutes.ai) relays a request it can't read to one of a model's
instances, a TDX machine a third-party miner runs, which alone holds the
private half of the instance's ML-KEM-768 key (made inside it at start-up).
The protocol, as read from Chutes' own code (Nov 14 2026, MIT: chutes-api
`api/e2e/router.py` and `scripts/test_e2e_client.py`, chutes
`chutes/entrypoint/run.py` and `verify.py`, sek8s `providers/base.py`):

- **A request** (`seal_request`): encapsulate to the instance's `e2e_pubkey`
  (base64, 1184 bytes), giving a ciphertext `ct` (1088 bytes) and a shared
  secret; key = HKDF-SHA256(secret, salt = ct[:16], info = b"e2e-req-v1"),
  32 bytes. The body is the OpenAI JSON with `e2e_response_pk` (a fresh
  ML-KEM-768 key of ours, base64) added, gzipped, sealed with
  ChaCha20-Poly1305 (a random 12-byte nonce, no associated data): the blob
  `ct ‖ nonce ‖ ciphertext ‖ tag`, POSTed to `/e2e/invoke`.
- **A reply sent whole** (`open_reply`, which the app no longer asks for: it
  streams every request): the same layout, encapsulated to our response
  key, info b"e2e-resp-v1"; the text inside is gzipped JSON.
- **A streamed reply** (`StreamOpener`): SSE events. `{"e2e_init": b64(ct)}`
  first, the stream key from it with info b"e2e-stream-v1"; then
  `{"e2e": b64(nonce ‖ ciphertext ‖ tag)}` per piece of the model server's
  own SSE bytes (its `data: [DONE]` included); beside them, unsealed,
  `{"usage": …}` (Chutes' copy, for its billing), and, in Chutes' own client,
  `{"e2e_error": …}` and a `data: [DONE]`.
- **The key binding** (`key_bound`): asked for evidence with our nonce, an
  instance's workload asks for a TDX quote whose report data begins with
  sha256(nonce_hex + e2e_pubkey_b64), the two strings as written, UTF-8
  (chutes `TeeEvidenceService._fetch_evidence`: `hashlib.sha256((nonce +
  self._e2e_pubkey).encode()).hexdigest()`, which sek8s's
  `_build_report_data` writes, as hex, before the TLS key's hash). So the
  hardware vouches for the key a request is sealed to, for our nonce.
- **Measurements** (`measured`): the quote's MRTD and RTMR0-3 against the
  sets Chutes publishes (`/servers/tdx/measurements`, or the same in
  `/servers/tee/measurements`' shape).
- **Models** (`listed_models`, `confidential_models`): `llm.chutes.ai/v1/models`,
  keyless; its end-to-end encryption goes only to those listed
  `confidential_compute: true` (exactly its -TEE ids, Nov 15 2026). A Chutes
  endpoint offers every model it lists, its others in the clear, as any
  provider's (the author, Nov 15 2026; models/config.py `chutes_tee`).

Chutes' own Python client (`chutes-e2ee` 0.1.0) is not used: it attests
nothing, sends a body it can't read as JSON in the clear, and takes any
unsealed event naming `usage` for the model's (see `StreamOpener`).
"""

from __future__ import annotations

import base64
import binascii
import codecs
import gzip
import hashlib
import hmac
import json
import os
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sealedlore.providers.base import ProviderError, relay_said
from sealedlore.storage.atomic import NestedTooDeep, outside_json

PUBLIC_KEY_BYTES = 1184  # ML-KEM-768 encapsulation key
CIPHERTEXT_BYTES = 1088  # ML-KEM-768 ciphertext
SHARED_SECRET_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
INFO_REQUEST = b"e2e-req-v1"
INFO_RESPONSE = b"e2e-resp-v1"
INFO_STREAM = b"e2e-stream-v1"
RESPONSE_KEY_FIELD = "e2e_response_pk"
# What a sealed reply may open to, decoded, at most (the DA apps' standard
# for a reply read whole): its gzip is inflated a piece at a time and refused
# past this, never inflated whole first.
MAX_DECODED_BYTES = 32 * 1024 * 1024
# A line of a sealed stream's own text, at most (as `streaming.MAX_LINE_CHARS`).
MAX_LINE_CHARS = 32 * 1024 * 1024
_INFLATE_PIECE = 256 * 1024
_HEX96 = re.compile(r"[0-9a-fA-F]{96}")

MLKEM_MISSING = (
    "This computer's cryptography library has no ML-KEM-768 (it needs one built with "
    "OpenSSL 3.5 or later, as the cryptography packages from PyPI are), so Chutes' "
    "end-to-end encryption can't be used here; nothing was sent."
)


class SealError(ProviderError):
    """A request couldn't be sealed, or a reply opened: nothing of it is used."""


class SealBroken(SealError):
    """The reply's seal doesn't hold (its length, its key or its tag): the
    relay changed it, or it was sealed to another key. Its attestation is
    doubted (providers/chutes_provider.py)."""


# --- ML-KEM-768 from the cryptography already pinned (the author's D2) ------------------

_available: bool | None = None


def mlkem_available() -> bool:
    """Whether this `cryptography` can make and use ML-KEM-768 keys: a build
    against an OpenSSL older than 3.5 (some distributions') raises
    UnsupportedAlgorithm. Asked once a process."""
    global _available
    if _available is None:
        try:
            from cryptography.exceptions import UnsupportedAlgorithm
        except ImportError:
            _available = False
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric import mlkem

            key = mlkem.MLKEM768PrivateKey.generate()
            secret, ciphertext = key.public_key().encapsulate()
            _available = key.decapsulate(ciphertext) == secret
        except (ImportError, AttributeError, UnsupportedAlgorithm):
            _available = False
    return _available


def mlkem_refusal() -> str | None:
    """Why Chutes can't be used on this computer, or None."""
    return None if mlkem_available() else MLKEM_MISSING


def _refuse_unless_available() -> None:
    if not mlkem_available():
        raise SealError(MLKEM_MISSING)


def _b64(text: Any, what: str) -> bytes:
    """Base64 (the standard alphabet, padded), strictly: whatever else it is
    isn't read."""
    if not isinstance(text, str) or not text.isascii():
        raise SealError(f"{what} isn't base64")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SealError(f"{what} isn't base64") from exc


def public_key(e2e_pubkey_b64: Any) -> Any:
    """An instance's ML-KEM-768 key from the base64 Chutes lists, exactly 1184
    bytes and one FIPS 203 accepts (its coefficients in range: the
    cryptography library checks), or SealError."""
    _refuse_unless_available()
    from cryptography.hazmat.primitives.asymmetric import mlkem

    raw = _b64(e2e_pubkey_b64, "the instance's key")
    if len(raw) != PUBLIC_KEY_BYTES:
        raise SealError(
            f"the instance's key is {len(raw)} bytes, not an ML-KEM-768 key's {PUBLIC_KEY_BYTES}"
        )
    try:
        return mlkem.MLKEM768PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SealError("the instance's key isn't a valid ML-KEM-768 key") from exc


def derive_key(shared_secret: bytes, ciphertext: bytes, info: bytes) -> bytes:
    """HKDF-SHA256 of the shared secret, the ML-KEM ciphertext's first 16 bytes
    as its salt, 32 bytes (chutes-api `scripts/test_e2e_client.py`)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(shared_secret) != SHARED_SECRET_BYTES or len(ciphertext) != CIPHERTEXT_BYTES:
        raise SealError("an ML-KEM secret or ciphertext of the wrong length")
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=ciphertext[:SALT_BYTES], info=info
    ).derive(shared_secret)


# --- a request ---------------------------------------------------------------------


@dataclass
class SealedRequest:
    """A request sealed to one instance, and the key of ours its reply comes
    back sealed to: made for this request alone, never kept past it."""

    blob: bytes
    response_key: Any = field(repr=False)


def seal_request(e2e_pubkey_b64: str, body: Mapping[str, Any]) -> SealedRequest:
    """`body` (the OpenAI JSON) sealed to the instance whose key Chutes lists
    as `e2e_pubkey_b64`, with a fresh response key of ours in it. Refuses
    (SealError) a key that isn't one, and a body that isn't a JSON object."""
    _refuse_unless_available()
    from cryptography.hazmat.primitives.asymmetric import mlkem
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    if not isinstance(body, Mapping) or not body:
        raise SealError("the request isn't a JSON object; nothing was sealed")
    instance = public_key(e2e_pubkey_b64)
    response_key = mlkem.MLKEM768PrivateKey.generate()
    plain = dict(body)
    plain[RESPONSE_KEY_FIELD] = base64.b64encode(
        response_key.public_key().public_bytes_raw()
    ).decode()
    try:
        text = json.dumps(plain, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise SealError("the request can't be written as JSON; nothing was sealed") from exc
    secret, ciphertext = instance.encapsulate()
    if len(ciphertext) != CIPHERTEXT_BYTES:
        raise SealError("ML-KEM-768 gave a ciphertext of the wrong length")
    key = derive_key(secret, ciphertext, INFO_REQUEST)
    nonce = os.urandom(NONCE_BYTES)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, gzip.compress(text, mtime=0), None)
    return SealedRequest(blob=ciphertext + nonce + sealed, response_key=response_key)


# --- a reply sent whole (the protocol's; the app streams every request) ----------


def _open(key: bytes, nonce: bytes, sealed: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    try:
        return ChaCha20Poly1305(key).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise SealBroken(
            "the reply's seal doesn't hold: it was changed, or sealed to another key"
        ) from exc


def _decapsulate(response_key: Any, ciphertext: bytes) -> bytes:
    try:
        return response_key.decapsulate(ciphertext)
    except ValueError as exc:
        raise SealBroken("the reply's ML-KEM ciphertext isn't one") from exc


def gunzip(data: bytes, most: int = MAX_DECODED_BYTES) -> bytes:
    """One gzip member, inflated a piece at a time and refused past `most`
    bytes; one cut short, or with anything after it, isn't whole."""
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    pending = data
    try:
        while not inflater.eof:
            piece = inflater.decompress(pending, _INFLATE_PIECE)
            out += piece
            if len(out) > most:
                raise SealError(
                    f"the sealed reply opens to more than {most // (1024 * 1024)} MB; not read"
                )
            left = inflater.unconsumed_tail
            if not piece and (not left or left == pending):
                break  # the input ran out before the gzip ended
            pending = left
    except zlib.error as exc:
        raise SealError("the sealed reply's compressed text couldn't be read") from exc
    if not inflater.eof or inflater.unused_data:
        raise SealError("the sealed reply's compressed text wasn't whole")
    return bytes(out)


def open_reply(blob: bytes, response_key: Any) -> dict[str, Any]:
    """A reply sent whole, opened with our response key: the model server's
    JSON (an OpenAI chat completion), or SealBroken when the seal doesn't hold
    and SealError when what it holds can't be read."""
    _refuse_unless_available()
    if len(blob) < CIPHERTEXT_BYTES + NONCE_BYTES + TAG_BYTES:
        raise SealBroken(f"the sealed reply is {len(blob)} bytes, too short to be one")
    ciphertext = blob[:CIPHERTEXT_BYTES]
    nonce = blob[CIPHERTEXT_BYTES : CIPHERTEXT_BYTES + NONCE_BYTES]
    secret = _decapsulate(response_key, ciphertext)
    text = gunzip(
        _open(
            derive_key(secret, ciphertext, INFO_RESPONSE),
            nonce,
            blob[CIPHERTEXT_BYTES + NONCE_BYTES :],
        )
    )
    try:
        reply = outside_json(text)
    except NestedTooDeep as exc:
        raise SealError("the sealed reply's JSON is nested too deeply to read") from exc
    except ValueError as exc:
        raise SealError("the sealed reply isn't JSON") from exc
    if not isinstance(reply, dict):
        raise SealError("the sealed reply isn't a JSON object")
    return reply


# --- a streamed reply (how every request is answered) -----------------------------------
#
# ChutesE2EEProvider streams every request through this (D6 as the author
# changed it, Nov 15 2026). Each piece of a stream is sealed on its own, with no
# associated data; but its nonce is a counter (live, Nov 15 2026, two models
# on two instances, 52 and 54 pieces: four zero bytes, then the piece's
# number from 0, eight bytes little-endian), and a piece's nonce is part of
# what opens it. So holding each piece to its number (`stream_nonce`) makes a
# piece dropped, repeated or put out of its place a refusal: without it, all
# three opened and read as the model's text (one, two... came back as
# ",one two"). The end is held by the decrypted `[DONE]` and a finish
# reason. The counter is what Chutes' closed library does, not a documented
# promise: should it change, every stream is refused, never read wrongly.


def stream_nonce(index: int) -> bytes:
    """The nonce Chutes' stream seals its piece number `index` (from 0) under."""
    return b"\x00" * 4 + index.to_bytes(8, "little")


class StreamOpener:
    """A Chutes stream's SSE lines (each as `streaming.read_lines` gives it),
    opened: `feed` returns the model server's own chunks (OpenAI's stream
    JSON) from the sealed pieces, in order. Only sealed text counts:

    - an unsealed `{"usage": …}` and nothing else is Chutes' word, kept apart
      (`plain_usage`) for a cost estimate when the sealed usage is missing;
    - an unsealed `{"e2e_error": …}` is said as Chutes' relay's,
      unauthenticated, and ends the stream;
    - an unsealed `[DONE]` is ignored: only the sealed one ends it;
    - any other unsealed event is refused. Chutes' own client passed any
      event naming `usage` on as it came, so `{"usage": {}, "choices": […]}`
      from the relay read as the model's text.

    `finish` then requires the sealed `[DONE]` and a finish reason."""

    def __init__(self, response_key: Any) -> None:
        _refuse_unless_available()
        self._response_key = response_key
        self._key: bytes | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line = ""
        self.done = False
        self.finish_reason: str | None = None
        # The sealed usage (the model server's own), and Chutes' unsealed copy.
        self.usage: dict[str, Any] = {}
        self.plain_usage: dict[str, Any] = {}
        self.pieces = 0

    def feed(self, line: str) -> list[dict[str, Any]]:
        """One SSE line from Chutes: the model server's chunks it opens to."""
        if not line.startswith("data:"):
            return []  # a blank line between events, a comment, a keep-alive
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return []  # Chutes' own end of stream: not the sealed one
        try:
            event = outside_json(data)
        except ValueError as exc:
            raise SealError("Chutes' stream carried an event that isn't JSON; refused") from exc
        if not isinstance(event, dict) or len(event) != 1:
            raise SealError("Chutes' stream carried an unsealed event that isn't its own; refused")
        name, value = next(iter(event.items()))
        if name == "e2e_init":
            return self._init(value)
        if name == "e2e":
            return self._piece(value)
        if name == "usage":
            if not isinstance(value, dict):
                raise SealError("Chutes' unsealed usage isn't an object; refused")
            self.plain_usage = value
            return []
        if name == "e2e_error":
            said = relay_said(value if isinstance(value, str) else json.dumps(value))
            raise ProviderError(
                "Chutes' relay said (unauthenticated): " + (said or "an error, with no words")
            )
        raise SealError(
            f"Chutes' stream carried an unsealed {relay_said(name, 40)!r} event; refused"
        )

    def _init(self, value: Any) -> list[dict[str, Any]]:
        if self._key is not None or self.pieces:
            raise SealError("Chutes' stream began its encryption twice; refused")
        ciphertext = _b64(value, "the stream's key exchange")
        if len(ciphertext) != CIPHERTEXT_BYTES:
            raise SealBroken("the stream's key exchange isn't an ML-KEM-768 ciphertext")
        self._key = derive_key(
            _decapsulate(self._response_key, ciphertext), ciphertext, INFO_STREAM
        )
        return []

    def _piece(self, value: Any) -> list[dict[str, Any]]:
        if self._key is None:
            raise SealError("Chutes' stream sent a sealed piece before its key exchange; refused")
        raw = _b64(value, "a sealed piece of the stream")
        if len(raw) < NONCE_BYTES + TAG_BYTES:
            raise SealBroken("a sealed piece of the stream is too short to be one")
        if raw[:NONCE_BYTES] != stream_nonce(self.pieces):
            raise SealBroken(
                "a piece of the stream isn't the one that comes next: dropped, repeated or "
                "put out of its place on the way"
            )
        text = self._decoder.decode(_open(self._key, raw[:NONCE_BYTES], raw[NONCE_BYTES:]))
        self.pieces += 1
        if self.done:
            # The model server's SSE ends its [DONE] with a blank line, sealed
            # as a piece of its own (live): nothing else may follow.
            if text.strip():
                raise SealError("Chutes' stream went on after the model's end; refused")
            return []
        return self._lines(text)

    def _lines(self, text: str) -> list[dict[str, Any]]:
        """The model server's own SSE text, split into its lines across pieces."""
        chunks: list[dict[str, Any]] = []
        *ended, self._line = (
            (self._line + text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        )
        if len(self._line) > MAX_LINE_CHARS:
            raise SealError("the sealed stream sent a line too long to read")
        for line in ended:
            chunk = self._sealed_line(line)
            if chunk is not None:
                chunks.append(chunk)
        return chunks

    def _sealed_line(self, line: str) -> dict[str, Any] | None:
        if not line.startswith("data:"):
            return None
        data = line[len("data:") :].strip()
        if self.done:
            if data:
                raise SealError("the sealed stream went on after its end; refused")
            return None
        if data == "[DONE]":
            self.done = True
            return None
        try:
            chunk = outside_json(data)
        except ValueError as exc:
            raise SealError("the sealed stream carried a chunk that isn't JSON") from exc
        if not isinstance(chunk, dict):
            raise SealError("the sealed stream carried a chunk that isn't an object")
        if chunk.get("error"):
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise ProviderError(f"the model's server reported an error: {relay_said(str(message))}")
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                self.finish_reason = choice["finish_reason"]
        return chunk

    def finish(self) -> None:
        """The stream has ended: refused unless the sealed text said so."""
        if self._line.strip():
            self._lines("\n")
        if not (self.done and self.finish_reason):
            raise SealError("the encrypted reply ended before the model said it was done")


# --- the key binding -------------------------------------------------------------------


def binding(nonce_hex: str, e2e_pubkey_b64: str) -> bytes:
    """What an instance's quote holds in its report data's first half for our
    nonce and its key: sha256 of the two strings joined, UTF-8, as written
    (chutes `verify.py` `TeeEvidenceService._fetch_evidence`; chutes-api
    `api/instance/util.py` checks the same). The nonce is lowercase hex, as
    sek8s normalises it; the key is the base64 exactly as Chutes lists it,
    never decoded and written again."""
    return hashlib.sha256((nonce_hex + e2e_pubkey_b64).encode()).digest()


def key_bound(report_data: bytes, nonce_hex: str, e2e_pubkey_b64: str) -> bool:
    """Whether a quote's report data binds this key to this nonce."""
    if len(report_data) != 64 or not isinstance(e2e_pubkey_b64, str):
        return False
    return hmac.compare_digest(report_data[:32], binding(nonce_hex, e2e_pubkey_b64))


# --- measurements ----------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementSet:
    """One VM image and hardware Chutes publishes: its MRTD and RTMR0-3."""

    name: str
    version: str
    mrtd: bytes
    rtmrs: tuple[bytes, ...]


def _register(value: Any) -> bytes | None:
    return bytes.fromhex(value) if isinstance(value, str) and _HEX96.fullmatch(value) else None


def published_measurements(listing: Any) -> list[MeasurementSet]:
    """The sets in Chutes' list, in either shape it serves: flat (`mrtd`,
    `rtmr0`-`rtmr3`: `/servers/tdx/measurements`) or the runtime registers
    apart (`runtime_rtmrs`: `/servers/tee/measurements`). A set any part of
    which isn't a register of 48 bytes in hex is left out. SealError when
    nothing in it can be read."""
    if not isinstance(listing, list):
        raise SealError("Chutes' published measurements aren't a list")
    sets: list[MeasurementSet] = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        runtime = entry.get("runtime_rtmrs")
        if isinstance(runtime, dict):
            values = [runtime.get(f"RTMR{i}") for i in range(4)]
        else:
            values = [entry.get(f"rtmr{i}") for i in range(4)]
        mrtd = _register(entry.get("mrtd"))
        rtmrs = [_register(value) for value in values]
        if mrtd is None or any(value is None for value in rtmrs):
            continue
        sets.append(
            MeasurementSet(
                name=relay_said(str(entry.get("name") or ""), 80),
                version=relay_said(str(entry.get("version") or ""), 20),
                mrtd=mrtd,
                rtmrs=tuple(value for value in rtmrs if value is not None),
            )
        )
    if not sets:
        raise SealError("Chutes' published measurements hold no set that can be read")
    return sets


def measured(
    mrtd: bytes, rtmrs: tuple[bytes, ...], sets: list[MeasurementSet]
) -> MeasurementSet | None:
    """The published set a quote's MRTD and RTMR0-3 are, all five, or None."""
    return next(
        (s for s in sets if s.mrtd == mrtd and s.rtmrs == tuple(rtmrs)),
        None,
    )


# --- the models ------------------------------------------------------------------------

_CHUTE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_chute_id(value: Any) -> bool:
    """A chute's or an instance's id as Chutes writes them (a UUID, lowercase):
    nothing else goes into a path or a header."""
    return isinstance(value, str) and _CHUTE_ID.fullmatch(value) is not None


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def confidential_models(listing: Any) -> dict[str, dict[str, Any]]:
    """The models of Chutes' listing (`llm.chutes.ai/v1/models`) that run in a
    TEE, `confidential_compute: true` and nothing less (`listed_models`): the
    only ones its end-to-end encryption is sent to."""
    return {
        model_id: entry
        for model_id, entry in listed_models(listing).items()
        if entry["confidential_compute"] is True
    }


def listed_models(listing: Any) -> dict[str, dict[str, Any]]:
    """Every model of Chutes' listing (`llm.chutes.ai/v1/models`), each as the
    app's model list holds an entry (engine/catalog.py, engine/pricing.py),
    keyed by its id, with its `chute_id` and whether Chutes lists it as
    confidential compute (true, and nothing else, counts): its price per
    million tokens (`unit: per_million_tokens`), context, output limit, what
    it reads and whether it reasons. A model without a chute id isn't
    listed. How private each is isn't the listing's to say: its id decides
    (models/config.py `chutes_tee`)."""
    items = listing.get("data") if isinstance(listing, dict) else None
    entries: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        model_id, chute_id = item.get("id"), item.get("chute_id")
        if not (isinstance(model_id, str) and model_id.strip() and is_chute_id(chute_id)):
            continue
        entry: dict[str, Any] = {
            "id": model_id,
            "name": model_id,
            "chute_id": chute_id,
            "confidential_compute": item.get("confidential_compute") is True,
        }
        if isinstance(item.get("owned_by"), str):
            entry["owned_by"] = item["owned_by"]
        for field_name, source in (
            ("context_length", "context_length"),
            ("max_output_tokens", "max_output_length"),
        ):
            value = item.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                entry[field_name] = value
        inputs, outputs = item.get("input_modalities"), item.get("output_modalities")
        if isinstance(inputs, list) or isinstance(outputs, list):
            entry["architecture"] = {
                "input_modalities": [m for m in inputs or [] if isinstance(m, str)],
                "output_modalities": [m for m in outputs or [] if isinstance(m, str)],
            }
        features = item.get("supported_features")
        if isinstance(features, list):
            entry["capabilities"] = {"reasoning": "reasoning" in features}
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            prompt, completion = _price(pricing.get("prompt")), _price(pricing.get("completion"))
            if prompt is not None and completion is not None:
                entry["pricing"] = {
                    "prompt": prompt,
                    "completion": completion,
                    "unit": "per_million_tokens",
                }
                cached = _price(pricing.get("input_cache_read"))
                if cached is not None:
                    entry["pricing"]["input_cache_read"] = cached
        entries[model_id] = entry
    return entries
