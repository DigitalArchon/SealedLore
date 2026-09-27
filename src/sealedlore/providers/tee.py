"""TEE models on nano-gpt: attestation when a private scene or TEE chat begins,
and a signature check on every reply.

nano-gpt's `TEE/*` models run inside a trusted execution environment. Two
calls let a client check that (docs.nano-gpt.com/api-reference/tee-verification):

- `GET {base}/tee/attestation?model=&nonce=` returns the enclave's
  `signing_address`, an Intel TDX quote (`intel_quote`) and NVIDIA GPU evidence
  (`nvidia_payload`). The quote's report data, read from its own bytes, must be
  the signing address, zero padding, then our nonce. The quote is verified up
  to Intel's pinned root, with Intel's revocation lists and TCB ratings
  (providers/dcap.py). The GPU evidence goes to NVIDIA's attestation service,
  and NVIDIA's signed answer must be for our nonce and pass
  (providers/nras.py). `attest` refuses (`TeeRefused`) or returns a "full" or
  "partial" attestation: see its docstring.
- `GET {base}/tee/signature/{completion id}?model=&signing_algo=ecdsa` returns
  a `text` and an EIP-191 `signature`; the signer recovered from them must be
  the attested address (providers/ethsig.py).

What this proves: a genuine, current, non-debug Intel TDX machine (and, when
evidence is offered, NVIDIA GPUs) holds the key, bound to our nonce, and a
record for each reply's id was signed by that key. What it doesn't prove:
which software that machine runs (no measurement is compared to a published
one), or that the reply's *content* is what was signed. The record's text is
`sha256(request):sha256(response)` as nano-gpt's gateway hashed them, and
neither matches the bytes this app sends or receives (checked live, Sept 24
2026). Nor is the call end-to-end private: plaintext passes nano-gpt's
gateway. For that, a `private/` model (providers/private_mode.py).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from sealedlore.models.private import Attestation
from sealedlore.providers import dcap, nras
from sealedlore.providers.base import ProviderError
from sealedlore.providers.ethsig import recover_personal
from sealedlore.providers.http import make_client

NRAS_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
TIMEOUT_SECONDS = 30.0


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _jwt_claims(value: Any) -> list[dict[str, Any]]:
    """Every JWT's payload found in a JSON value, in order."""
    found: list[dict[str, Any]] = []
    if isinstance(value, str) and value.count(".") == 2:
        part = value.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, binascii.Error):
            return found
        if isinstance(claims, dict):
            found.append(claims)
    elif isinstance(value, list):
        for item in value:
            found.extend(_jwt_claims(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_jwt_claims(item))
    return found


def is_tee(model: str) -> bool:
    """A model that runs in a trusted execution environment: nano-gpt's
    `TEE/*` (attested and signed) or `private/*` (end-to-end encrypted)."""
    return model.startswith(("TEE/", "private/"))


def is_private_mode(model: str) -> bool:
    """nano-gpt's Private Mode: end-to-end encrypted to an attested enclave
    (providers/private_mode.py). Never sent any other way."""
    return model.startswith("private/")


def move_refusal(current: str, new: str) -> str | None:
    """Why a chat on `current` may not move to `new`, or None: a TEE chat stays
    on TEE models, and an end-to-end encrypted one on encrypted models."""
    if is_private_mode(current) and not is_private_mode(new):
        return (
            f"{new} isn't end-to-end encrypted, and this chat is: choose a private/ "
            "model, or start a new chat."
        )
    if is_tee(current) and not is_tee(new):
        return (
            f"{new} isn't a TEE model, and this chat is kept to TEE models: choose a "
            "TEE/ or private/ model, or start a new chat."
        )
    return None


def _bases(base_url: str) -> list[str]:
    """Where the TEE endpoints may live: the docs give the path three ways."""
    base = base_url.rstrip("/")
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    found = [base, f"{root}/api/v1", f"{root}/v1"]
    return list(dict.fromkeys(found))


def _error_text(response: httpx.Response) -> str:
    """The provider's message from an error response, or ""."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        return str(error or body.get("message") or "")[:200]
    return ""


def _instance_key(certificate: bytes, body: bytes, signature: bytes) -> bytes | None:
    """sha256 of the certificate's public key (DER SubjectPublicKeyInfo) when its
    signature over `body` holds, else None (a Chutes instance's evidence)."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    try:
        key = x509.load_der_x509_certificate(certificate).public_key()
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
        else:
            return None
    except (InvalidSignature, ValueError):
        return None
    return hashlib.sha256(
        key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).digest()


class _Checks:
    """What one attestation's checks found, gathered across quotes and instances."""

    def __init__(self, *notes: str) -> None:
        self.notes = list(notes)
        self.shortfalls: list[str] = []
        self.status = ""
        self.gpus = 0

    def note(self, *notes: str) -> None:
        for note in notes:
            if note not in self.notes:
                self.notes.append(note)

    def fall_short(self, what: str) -> None:
        if what not in self.shortfalls:
            self.shortfalls.append(what)

    def tcb(self, status: str) -> None:
        """Keep the worst Intel TCB status seen."""

        def rank(value: str) -> int:
            return dcap.STATUSES.index(value) if value in dcap.STATUSES else len(dcap.STATUSES)

        if status and (not self.status or rank(status) > rank(self.status)):
            self.status = status

    def merge(self, other: _Checks) -> None:
        self.note(*other.notes)
        for what in other.shortfalls:
            self.fall_short(what)
        self.tcb(other.status)
        self.gpus += other.gpus

    def attestation(self, *, signing_address: str | None = None, instances: int = 0) -> Attestation:
        return Attestation(
            verified=True,
            level="partial" if self.shortfalls else "full",
            shortfalls=list(self.shortfalls),
            signing_address=signing_address,
            nonce_ok=True,
            gpu_ok=True if self.gpus else None,
            gpus=self.gpus,
            tcb_status=self.status,
            instances=instances,
            detail="; ".join(self.notes),
        )


class TeeRefused(ProviderError):
    """The enclave's attestation doesn't hold: nothing may be sent to it."""


class TeeClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        client: httpx.Client | None = None,
        intel: Callable[..., dcap.Result] | None = None,
        nvidia: Callable[..., int] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client = client or make_client(base_url, timeout=httpx.Timeout(TIMEOUT_SECONDS))
        self._owns = client is None
        self.signing_address: str | None = None
        self._base: str | None = None
        # The two verifiers, replaceable so tests can stand in for Intel and NVIDIA.
        self._intel = intel or dcap.verify
        self._nvidia = nvidia or nras.verify

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last: str = ""
        for base in [self._base] if self._base else _bases(self.base_url):
            try:
                response = self._client.get(f"{base}{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                last = str(exc)
                continue
            if response.status_code == 404:
                last = f"{base}{path}: 404"
                continue
            if response.status_code >= 400:
                # nano-gpt's own words, when it gives any: "Unable to reach RedPill
                # attestation service" says far more than a status code.
                reason = _error_text(response)
                raise ProviderError(
                    f"nano-gpt answered HTTP {response.status_code}"
                    + (f": {reason}" if reason else ""),
                    status_code=response.status_code,
                    body=reason,
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(f"{base}{path} returned a non-JSON body") from exc
            if not isinstance(body, dict):
                raise ProviderError(f"{base}{path} returned {type(body).__name__}")
            self._base = base
            return body
        raise ProviderError(f"no TEE endpoint answered ({last})")

    def attest(self) -> Attestation:
        """Check the enclave now, with a fresh nonce (network; a worker thread).

        nano-gpt's TEE/ models come from more than one provider, attesting in two
        shapes (both measured live, Sept 2026; ported from the author's
        AI-Assistant app):

        - aci/1: one enclave, whose TDX quote binds its reply-signing key and our
          nonce, with GPU evidence made with our nonce (`_attest_signing_key`);
        - per instance (Chutes: TEE/kimi-k3 and others): a list of instances, each
          quote binding that instance's own key, which signs a body carrying our
          nonce, the quote and the GPU evidence (`_attest_instances`).

        Refuses (`TeeRefused`, and nothing may be sent) when an enclave can't be
        trusted: no attestation, a quote that doesn't bind our nonce (directly, or
        by a key the quote binds), a quote Intel's chain doesn't vouch for (forged,
        revoked, a debug TD, a TCB Intel revoked or never listed), GPU evidence
        whose NVIDIA verdict is unsigned, stale, for another nonce or negative
        (providers/dcap.py, providers/nras.py), any instance that failed to attest
        (a request could be routed to it), or evidence relayed for a nonce the
        relay chose (RedPill: replayable).

        What merely couldn't be checked (Intel's collateral or NVIDIA's service out
        of reach, no GPU evidence offered) or isn't current (any Intel TCB status
        but UpToDate) makes the attestation "partial", each reason in `shortfalls`.

        Until Sept 2026 a nonce that merely came back in nano-gpt's JSON counted,
        the report data was read from nano-gpt's copy, and neither Intel's nor
        NVIDIA's signatures were checked. Anything can echo a string.
        """
        nonce = secrets.token_hex(32)
        try:
            body = self._get("/tee/attestation", {"model": self.model, "nonce": nonce})
        except ProviderError as exc:
            reason = exc.body or ""
            if exc.status_code == 400:
                # Live (Sept 2026): the Tinfoil-hosted TEE/ models offer no
                # attestation here; nano-gpt says fresh attestation for them only
                # comes through Tinfoil's own flow, the one their private/ twins use.
                hint = (
                    "; its end-to-end encrypted private/ version, where nano-gpt offers "
                    "one, is attested that way"
                    if "tinfoil" in reason.lower()
                    else ""
                )
                raise TeeRefused(
                    f"{self.model} offers no attestation"
                    + (f" (nano-gpt: {reason})" if reason else "")
                    + hint
                ) from exc
            if exc.status_code is not None and exc.status_code >= 500:
                raise TeeRefused(
                    f"nano-gpt couldn't fetch {self.model}'s attestation just now ({exc}); "
                    "this is on the provider's side, and may pass if tried again"
                ) from exc
            raise TeeRefused(f"{self.model}'s attestation couldn't be fetched: {exc}") from exc
        if isinstance(body.get("evidence"), list):
            return self._attest_instances(body, nonce)
        if isinstance(body.get("all_attestations"), list) and "signing_address" not in body:
            # RedPill relaying Chutes (live, Sept 2026: TEE/glm-5.1, TEE/qwen3.5-27b,
            # TEE/qwen3.5-397b-a17b). The quotes are made for a nonce the relay
            # chose, not ours nor derived from it, and nothing signed ties them to
            # ours: they would read the same replayed from any earlier day.
            why = (
                "made for a nonce the relay chose, not ours"
                if body.get("nonce") != nonce
                else "carries no binding of our nonce this app can check"
            )
            raise TeeRefused(
                f"{self.model}'s attestation, relayed by its provider "
                f"({body.get('attestation_type') or 'unnamed'}), was {why}, so it can't "
                "show the enclave is live for this request"
            )
        if body.get("signing_address") or body.get("intel_quote"):
            return self._attest_signing_key(body, nonce)
        if body.get("error"):
            raise TeeRefused(f"nano-gpt couldn't attest {self.model}: {body['error']}")
        raise TeeRefused(
            f"{self.model}'s attestation is in a shape this app doesn't recognise "
            f"(it holds {', '.join(sorted(body)) or 'nothing'})"
        )

    def _attest_signing_key(self, body: dict[str, Any], nonce: str) -> Attestation:
        """aci/1: one enclave, its reply-signing key and our nonce in its quote."""
        address = body.get("signing_address")
        address = address if isinstance(address, str) and address.startswith("0x") else None
        if address is None:
            raise TeeRefused(f"{self.model}'s attestation named no signing key")
        try:
            raw = bytes.fromhex(str(body.get("intel_quote") or ""))
            quote = dcap.parse_quote(raw)
        except (ValueError, dcap.DcapError) as exc:
            raise TeeRefused(f"{self.model}'s Intel TDX quote couldn't be read ({exc})") from exc
        # From the quote's own bytes, the part Intel's chain signs, never from
        # nano-gpt's JSON copy: the signing address, zero padding, our nonce.
        report_data = quote.report_data
        if (
            report_data[:20] != bytes.fromhex(address[2:])
            or any(report_data[20:32])
            or report_data[32:] != bytes.fromhex(nonce)
        ):
            raise TeeRefused(f"{self.model}'s attestation didn't bind its signing key to our nonce")
        check = _Checks("the Intel TDX quote binds the signing key to our nonce")
        self._check_quote(raw, check)
        payload = _json(body.get("nvidia_payload"))
        self._check_gpus(
            payload if isinstance(payload, dict) else None, nonce, check, "this request's nonce"
        )
        self.signing_address = address
        return check.attestation(signing_address=address)

    def _attest_instances(self, body: dict[str, Any], nonce: str) -> Attestation:
        """Per instance: each quote binds an instance key, which signed our nonce.

        For each instance, all of these before it counts:
        - its certificate's public key hashes to the second half of its quote's
          report data, so the key belongs to that TD (the certificate's issuer, the
          provider's private CA, is not a trust anchor and isn't relied on);
        - that key's signature over `attested_body` verifies, and the body carries
          our nonce and exactly this quote: an instance whose key the hardware
          vouches for answered this request;
        - the quote passes Intel's checks, like any other;
        - the GPU evidence (the signed copy) passes NVIDIA's checks. It is made
          with the first half of the report data as its nonce (measured live:
          NVIDIA's verdict matches that and nothing else), so it is bound to the
          quote, and through the signature to this request.
        The instances are checked in parallel; the attestation takes the worst.
        """
        failed = [str(i) for i in body.get("failed_instance_ids") or []]
        instances = body["evidence"]
        if failed:
            raise TeeRefused(
                f"{len(failed)} of {len(failed) + len(instances)} of {self.model}'s "
                "instances couldn't be attested, and a request could be routed to one"
            )
        if not instances:
            raise TeeRefused(f"{self.model}'s attestation listed no instances")
        with ThreadPoolExecutor(max_workers=min(8, len(instances))) as pool:
            results = list(pool.map(lambda item: self._check_instance(item, nonce), instances))
        check = _Checks(
            f"each of {len(instances)} instances' keys, bound into its TDX quote, signed our nonce"
        )
        for result in results:
            check.merge(result)
        attestation = check.attestation(instances=len(instances))
        attestation.detail += "; this provider signs no replies, so answers show as unsigned"
        return attestation

    def _check_instance(self, item: dict[str, Any], nonce: str) -> _Checks:
        name = str(item.get("instance_id") or "an instance")[:8]
        try:
            raw = base64.b64decode(item["quote"])
            quote = dcap.parse_quote(raw)
            signed = base64.b64decode(item["attested_body"])
            certificate = base64.b64decode(item["certificate"])
            signature = base64.b64decode(item["signature"])
        except (KeyError, TypeError, ValueError, binascii.Error, dcap.DcapError) as exc:
            raise TeeRefused(
                f"{self.model}'s instance {name} sent unreadable evidence ({exc})"
            ) from exc
        key_hash = _instance_key(certificate, signed, signature)
        if key_hash is None:
            raise TeeRefused(
                f"{self.model}'s instance {name} didn't sign its evidence with the key it presented"
            )
        if quote.report_data[32:] != key_hash:
            raise TeeRefused(
                f"{self.model}'s instance {name} signed with a key its TDX quote doesn't bind"
            )
        try:
            attested = json.loads(signed)
            evidence = attested["evidence"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TeeRefused(f"{self.model}'s instance {name} signed unreadable evidence") from exc
        if attested.get("nonce") != nonce:
            raise TeeRefused(f"{self.model}'s instance {name} didn't sign our nonce")
        if base64.b64decode(evidence.get("tdx_quote") or "") != raw:
            raise TeeRefused(f"{self.model}'s instance {name} signed a different quote")
        check = _Checks()
        self._check_quote(raw, check)
        gpu = _json(evidence.get("nvtrust_evidence"))
        payload = None
        gpu_nonce = quote.report_data[:32].hex()
        if isinstance(gpu, list) and gpu:
            payload = {
                "nonce": gpu_nonce,
                "arch": gpu[0].get("arch", "HOPPER"),
                "evidence_list": gpu,
            }
        self._check_gpus(payload, gpu_nonce, check, "the nonce its quote binds")
        return check

    def _check_quote(self, raw: bytes, check: _Checks) -> None:
        """Intel's verification of one quote, into `check`. Refuses a bad quote."""
        try:
            intel = self._intel(raw, self._client)
        except dcap.DcapError as exc:
            raise TeeRefused(f"{self.model}'s Intel TDX quote failed verification: {exc}") from exc
        check.note(*intel.notes)
        check.tcb(intel.status)
        if not intel.collateral:
            check.fall_short("Intel TCB unchecked")
        elif intel.status != "UpToDate":
            check.fall_short(f"Intel TCB {intel.status}")

    def _check_gpus(
        self, payload: dict[str, Any] | None, nonce: str, check: _Checks, bound_to: str
    ) -> None:
        """NVIDIA's verdict on one set of GPU evidence, into `check`."""
        if not (isinstance(payload, dict) and payload.get("evidence_list")):
            # Live, several TEE/ models send an empty evidence list: not a
            # rejection, but nothing for NVIDIA to vouch for either.
            check.fall_short("no GPU evidence")
            check.note("no GPU evidence was offered")
            return
        answer = self._nras_answer(payload)
        if answer is None:
            check.fall_short("GPUs unverified")
            check.note("NVIDIA's attestation service couldn't be reached")
            return
        try:
            gpus = self._nvidia(answer, nonce, self._client)
        except nras.KeysUnavailable as exc:
            # Unchecked, but a verdict that says no is still believed.
            if not any(
                claims.get("x-nvidia-overall-att-result") is True for claims in _jwt_claims(answer)
            ):
                raise TeeRefused(f"NVIDIA rejected {self.model}'s GPU evidence") from exc
            check.fall_short("NVIDIA signature unchecked")
            check.note(f"{exc}, so NVIDIA's verdict is unchecked")
        except nras.NrasError as exc:
            raise TeeRefused(f"{self.model}'s GPU attestation failed: {exc}") from exc
        else:
            check.gpus += gpus
            check.note(
                f"NVIDIA's signed verdict vouches for {gpus} GPU{'s' if gpus != 1 else ''}, "
                f"for {bound_to}"
            )

    def _nras_answer(self, payload: Any) -> Any:
        """NVIDIA's answer to the GPU evidence, or None if the service couldn't be
        reached. HTTP 4xx is NVIDIA refusing the evidence: that refuses. (It used
        to read "rejected" for an outage too.)"""
        try:
            response = self._client.post(NRAS_URL, json=payload)
        except httpx.HTTPError:
            return None
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            raise TeeRefused(
                f"NVIDIA rejected {self.model}'s GPU evidence (HTTP {response.status_code})"
            )
        try:
            return response.json()
        except ValueError:
            return None

    def verify_reply(self, response_id: str | None) -> bool:
        """Was this reply signed by the attested enclave? Raises ProviderError
        when the check couldn't be made (the reply is then "unchecked")."""
        if not response_id:
            raise ProviderError("the reply carried no id to check")
        if self.signing_address is None:
            # Per-instance providers (Chutes) sign no replies: nano-gpt answers "TEE
            # signature is not available for this model". Every instance that
            # could answer was attested before sending instead.
            raise ProviderError("this model's provider signs no replies")
        try:
            body = self._get(
                f"/tee/signature/{quote(response_id, safe='')}",
                {"model": self.model, "signing_algo": "ecdsa"},
            )
        except ProviderError as exc:
            # Live: some TEE/ models' providers keep no signature for a reply.
            raise ProviderError("the provider has no signature for this reply") from exc
        text, signature = body.get("text"), body.get("signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            raise ProviderError("the signature reply held no text or signature")
        try:
            signer = recover_personal(text, signature)
        except ValueError:
            return False
        return signer.lower() == self.signing_address.lower()
