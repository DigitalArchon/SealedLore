"""NVIDIA's verdict on a TEE/ model's GPUs, with NVIDIA's signature checked.

Ported from the author's AI-Assistant app (assistant/nras.py, Sept 2026), with httpx
for requests; the checks are the same.

A TEE/ model's attestation carries NVIDIA GPU evidence, made with our nonce.
providers/tee.py sends it to NVIDIA's Remote Attestation Service (NRAS), which answers with signed
Entity Attestation Tokens (JWTs, ES384):

  [["JWT", <overall token>], {"GPU-0": <token>, "GPU-1": <token>, ...}]

The overall token carries `x-nvidia-overall-att-result`, `eat_nonce`, and in `submods` a
SHA-256 digest of each GPU's token. This module checks, before anything is believed:

  - every token's ES384 signature, against NVIDIA's published keys
    (nras.attestation.nvidia.com/.well-known/jwks.json, fetched over TLS from NVIDIA);
  - the issuer, and that each token is within its nbf/exp window;
  - that `eat_nonce` is our nonce, in every token - the evidence was made for this
    request, not replayed from another;
  - that the GPU tokens are exactly those the overall token lists, digest for digest;
  - the verdicts: the overall result, and for each GPU its measurements (`measres`),
    secure boot, debug disabled and the report's own nonce match.

Measured live (TEE/glm-5.3-flash, Sept 2026): eight H100 tokens, every digest matching,
keys named nv-eat-kid-prod-<date>-<uuid> and rotated daily. A key missing from the
cached key set is looked for once more before the token is refused.
"""

import base64
import hashlib
import json
import threading
import time
from typing import Any

import httpx

from sealedlore.providers.http import make_client

JWKS_URL = "https://nras.attestation.nvidia.com/.well-known/jwks.json"
ISSUER = "https://nras.attestation.nvidia.com"
TIMEOUT_SECONDS = 30
KEYS_TTL = 3600
LEEWAY_SECONDS = 300


class NrasError(Exception):
    """NVIDIA's answer is not a signed, current verdict for this request: refuse."""


class KeysUnavailable(Exception):
    """NVIDIA's signing keys could not be fetched: the verdict is unchecked."""


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


_keys: dict[str, Any] = {"at": 0.0, "keys": {}}
_keys_lock = threading.Lock()


def _jwks(session: httpx.Client, refresh: bool = False) -> dict[str, dict[str, Any]]:
    with _keys_lock:
        if not refresh and _keys["keys"] and time.monotonic() - _keys["at"] < KEYS_TTL:
            return _keys["keys"]
    try:
        response = session.get(JWKS_URL, timeout=TIMEOUT_SECONDS)
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as e:
        raise KeysUnavailable(f"NVIDIA's signing keys could not be fetched ({e})") from e
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise KeysUnavailable("NVIDIA's signing keys could not be read")
    keys = {k["kid"]: k for k in body["keys"] if isinstance(k, dict) and k.get("kid")}
    with _keys_lock:
        _keys.update(at=time.monotonic(), keys=keys)
    return keys


def _public_key(jwk: dict[str, Any]) -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec

    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-384":
        raise NrasError(f"NVIDIA's key {jwk.get('kid')} is not a P-384 key")
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(_b64(jwk["x"]), "big"), int.from_bytes(_b64(jwk["y"]), "big"), ec.SECP384R1()
    ).public_key()


def verify_token(token: str, session: httpx.Client, nonce: str, now: float) -> dict[str, Any]:
    """The claims of one NRAS token, once its signature, issuer, time and nonce hold."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_b64(header_part))
        claims = json.loads(_b64(claims_part))
        signature = _b64(signature_part)
    except (ValueError, AttributeError):
        raise NrasError("NVIDIA's answer held a malformed token") from None
    if header.get("alg") != "ES384" or len(signature) != 96:
        raise NrasError(f"a token is signed with {header.get('alg')}, not ES384")
    keys = _jwks(session)
    if header.get("kid") not in keys:
        keys = _jwks(session, refresh=True)
    jwk = keys.get(header.get("kid"))
    if jwk is None:
        raise NrasError(f"a token names a key NVIDIA does not publish ({header.get('kid')})")
    der = encode_dss_signature(
        int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
    )
    try:
        _public_key(jwk).verify(
            der, f"{header_part}.{claims_part}".encode("ascii"), ec.ECDSA(hashes.SHA384())
        )
    except InvalidSignature:
        raise NrasError("a token's signature does not verify against NVIDIA's key") from None
    if claims.get("iss") != ISSUER:
        raise NrasError(f"a token was issued by {claims.get('iss')}, not NVIDIA's service")
    if not (claims.get("nbf", 0) - LEEWAY_SECONDS <= now <= claims.get("exp", 0) + LEEWAY_SECONDS):
        raise NrasError("a token is outside its validity window")
    if claims.get("eat_nonce") != nonce:
        raise NrasError("NVIDIA's verdict was not made for this request's nonce")
    return claims


def verify(
    answer: Any, nonce: str, session: httpx.Client | None = None, now: float | None = None
) -> int:
    """Check NRAS's answer. Returns the number of GPUs vouched for, or raises NrasError
    (refuse), KeysUnavailable (unchecked), or ImportError (no cryptography)."""
    import cryptography  # noqa: F401  - ImportError here, before anything is claimed

    own = session is None
    session = session or make_client(JWKS_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))
    now = now if now is not None else time.time()
    try:
        return _verify(answer, nonce, session, now)
    finally:
        if own:
            session.close()


def _verify(answer: Any, nonce: str, session: httpx.Client, now: float) -> int:
    try:
        label, overall = answer[0]
        tokens = answer[1] if len(answer) > 1 else {}
    except (TypeError, ValueError, IndexError, KeyError):
        raise NrasError("NVIDIA's answer was not in the expected shape") from None
    if label != "JWT" or not isinstance(tokens, dict):
        raise NrasError("NVIDIA's answer was not in the expected shape")

    claims = verify_token(overall, session, nonce, now)
    if claims.get("x-nvidia-overall-att-result") is not True:
        raise NrasError("NVIDIA rejected the GPU evidence")
    listed = claims.get("submods")
    if not isinstance(listed, dict) or set(listed) != set(tokens) or not tokens:
        raise NrasError("the GPU tokens are not the ones NVIDIA's verdict lists")
    for gpu, token in tokens.items():
        try:
            digest = listed[gpu][1][1]
        except (TypeError, IndexError, KeyError):
            raise NrasError(f"NVIDIA's verdict gives no digest for {gpu}") from None
        if hashlib.sha256(token.encode("ascii")).hexdigest() != digest:
            raise NrasError(f"{gpu}'s token is not the one NVIDIA's verdict lists")
        gpu_claims = verify_token(token, session, nonce, now)
        failed = [
            name
            for name, ok in (
                ("measurements", gpu_claims.get("measres") == "success"),
                ("secure boot", gpu_claims.get("secboot") is True),
                ("debug disabled", gpu_claims.get("dbgstat") == "disabled"),
                (
                    "report nonce",
                    gpu_claims.get("x-nvidia-gpu-attestation-report-nonce-match") is True,
                ),
            )
            if not ok
        ]
        if failed:
            raise NrasError(f"NVIDIA found {gpu} failing: {', '.join(failed)}")
    return len(tokens)
