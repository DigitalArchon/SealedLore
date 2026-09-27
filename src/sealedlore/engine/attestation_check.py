"""Test a private model's attestation before choosing it.

The author (Sept 2026): rather than choose a TEE model, start a chat and have
it refused, the user should see it attest when they pick it. This runs the
very check a private scene or TEE chat runs on entering: `TeeClient.attest`
for a `TEE/` model, the Private Mode provider's attestation for a `private/`
one. Network; call it off the GUI thread.
"""

from __future__ import annotations

from dataclasses import dataclass

from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import ProviderError
from sealedlore.providers.tee import TeeRefused, is_private_mode, is_tee


@dataclass(frozen=True)
class AttestationCheck:
    # "full", "partial", "encrypted", "refused", or "error" (the check itself
    # couldn't run, e.g. no network: not the enclave's fault).
    outcome: str
    headline: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome in ("full", "partial", "encrypted")


def testable(model: str) -> bool:
    return is_tee(model)  # TEE/ and private/ both


def check_attestation(settings: ProviderConfig) -> AttestationCheck:
    """Attest `settings.model` on `settings`' endpoint, as entering would."""
    model = settings.model.strip()
    if is_private_mode(model):
        from sealedlore.providers.private_mode import PrivateModeProvider

        provider = PrivateModeProvider(settings)
        try:
            attestation = provider.attest()
        except ProviderError as exc:
            return AttestationCheck("refused", f"✗ Refused: {exc}", "Nothing would be sent to it.")
        finally:
            provider.close()
        older = attestation.latest_release
        return AttestationCheck(
            "encrypted",
            "✓ End-to-end encrypted"
            + (f" (⚠ an older enclave release than the latest, {older})" if older else ""),
            attestation.detail,
        )
    if not is_tee(model):
        return AttestationCheck("error", "Only TEE/ and private/ models are attested.")
    from sealedlore.providers.tee import TeeClient

    tee = TeeClient(settings.base_url, settings.api_key, model)
    try:
        attestation = tee.attest()
    except TeeRefused as exc:
        return AttestationCheck("refused", f"✗ Refused: {exc}", "Nothing would be sent to it.")
    except Exception as exc:  # noqa: BLE001 - shown: the check couldn't run
        return AttestationCheck("error", f"The check couldn't run: {exc}")
    finally:
        tee.close()
    parts = []
    if attestation.instances > 1:
        parts.append(f"{attestation.instances} instances")
    if attestation.gpus:
        parts.append(f"{attestation.gpus} GPU{'s' if attestation.gpus != 1 else ''} verified")
    if attestation.tcb_status:
        parts.append(f"Intel {attestation.tcb_status}")
    summary = f" ({', '.join(parts)})" if parts else ""
    if attestation.level == "partial":
        return AttestationCheck(
            "partial",
            f"✓ TEE attested, partially: {', '.join(attestation.shortfalls)}{summary}",
            attestation.detail,
        )
    return AttestationCheck("full", f"✓ TEE attested{summary}", attestation.detail)
