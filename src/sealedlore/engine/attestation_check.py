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
from sealedlore.providers.privacy import CHUTES, NANOGPT_PRIVATE, TINFOIL, not_built, privacy_for
from sealedlore.providers.tee import (
    TeeRefused,
    failure_words,
    what_was_attested,
)


@dataclass(frozen=True)
class AttestationCheck:
    # "full", "partial", "encrypted", "refused", "retired" (nano-gpt no longer
    # offers the private/ model), or "error" (the check itself couldn't run,
    # e.g. no network: not the enclave's fault).
    outcome: str
    headline: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome in ("full", "partial", "encrypted")


def testable(model: str, endpoint: ProviderConfig | None = None) -> bool:
    """A TEE or end-to-end encrypted model on `endpoint` (by the id alone
    without one): one there is an enclave of to attest."""
    return privacy_for(model, endpoint).private


def check_attestation(settings: ProviderConfig) -> AttestationCheck:
    """Attest `settings.model` on `settings`' endpoint, as entering would."""
    settings = settings.sendable()
    model = settings.model.strip()
    privacy = privacy_for(model, settings)
    refusal = not_built(privacy, settings.base_url)
    if refusal:
        return AttestationCheck("error", f"The check couldn't run: {refusal}")
    if privacy.service == TINFOIL:
        # Keyless: Tinfoil's bundle and model list are public.
        return _check_tinfoil(settings, model)
    if privacy.service == CHUTES:
        # With the key, as a request would attest; without, what is public.
        return _check_chutes(settings, model)
    if settings.key_withheld:
        return AttestationCheck("error", f"The check couldn't run: {settings.key_withheld}")
    if privacy.service == NANOGPT_PRIVATE:
        from sealedlore.providers.private_mode import PrivateModeProvider

        provider = PrivateModeProvider(settings)
        try:
            # The enclave is Tinfoil's router whatever the model, so a retired
            # model would attest: ask the list first.
            if provider.still_offered(model) is False:
                return AttestationCheck(
                    "retired",
                    "✗ No longer offered by NanoGPT's Private Mode",
                    "Choose another private model.",
                )
            attestation = provider.attest()
        except ProviderError as exc:
            return AttestationCheck(
                "refused", f"✗ Refused: {failure_words(exc)}", "Nothing would be sent to it."
            )
        finally:
            provider.close()
        older = attestation.latest_release
        return AttestationCheck(
            "encrypted",
            "✓ End-to-end encrypted"
            + (f" (⚠ an older enclave release than the latest, {older})" if older else ""),
            attestation.detail,
        )
    if not privacy.private:
        return AttestationCheck("error", "Only TEE/ and private/ models are attested.")
    from sealedlore.providers.tee import TeeClient

    tee = TeeClient(settings.base_url, settings.api_key, model)
    try:
        attestation = tee.attest()
    except TeeRefused as exc:
        return AttestationCheck(
            "refused", f"✗ Refused: {failure_words(exc)}", "Nothing would be sent to it."
        )
    except Exception as exc:  # noqa: BLE001 - shown: the check couldn't run
        return AttestationCheck("error", f"The check couldn't run: {failure_words(exc)}")
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
    # What was attested leads the details (the author, Nov 11 2026: "always
    # make clear to the user what is actually happening"): RedPill's gateway
    # is not the machine that runs the model.
    what = "TEE gateway attested" if attestation.attested == "gateway" else "TEE attested"
    detail = "\n\n".join(p for p in (what_was_attested(attestation), attestation.detail) if p)
    if attestation.level == "partial":
        return AttestationCheck(
            "partial",
            f"⚠ {what}, partially: {', '.join(attestation.shortfalls)}{summary}",
            detail,
        )
    return AttestationCheck("full", f"✓ {what}{summary}", detail)


def _check_tinfoil(settings: ProviderConfig, model: str) -> AttestationCheck:
    """Tinfoil's own API (providers/tinfoil_direct.py): its router attested
    from Tinfoil's bundle service, for the endpoint's own host, and the
    model still listed. Nothing is sent with the key."""
    from sealedlore.providers.tinfoil_direct import TinfoilDirect

    provider = TinfoilDirect(settings)
    try:
        if provider.still_offered(model) is False:
            return AttestationCheck(
                "retired", "✗ Not offered by Tinfoil", "Choose another of Tinfoil's models."
            )
        attestation = provider.attest()
    except ProviderError as exc:
        return AttestationCheck(
            "refused", f"✗ Refused: {failure_words(exc)}", "Nothing would be sent to it."
        )
    finally:
        provider.close()
    older = attestation.latest_release
    return AttestationCheck(
        "encrypted",
        "✓ End-to-end encrypted, Tinfoil's router attested"
        + (f" (⚠ an older enclave release than the latest, {older})" if older else ""),
        "\n\n".join(p for p in (what_was_attested(attestation), attestation.detail) if p),
    )


def _check_chutes(settings: ProviderConfig, model: str) -> AttestationCheck:
    """Chutes' end-to-end encryption (providers/chutes_provider.py), always
    partial. With the key: discovery and every instance's checks, the key
    binding included, as a request attests. Without it: the chute's public
    evidence through the same checks, the key binding left unchecked (only
    discovery, with the key, names each instance's key), and said."""
    from sealedlore.providers.chutes_provider import ChutesE2EEProvider, NotOffered
    from sealedlore.providers.tee import TeeRefused

    keyed = bool(settings.api_key) and not settings.key_withheld
    provider = ChutesE2EEProvider(settings)
    try:
        attestation = provider.attest(model) if keyed else provider.attest_public(model)
    except NotOffered:
        return AttestationCheck(
            "retired",
            "✗ Not one of Chutes' end-to-end encrypted models",
            "Chutes offers only its confidential-compute models end to end; choose one of them.",
        )
    except TeeRefused as exc:
        return AttestationCheck(
            "refused", f"✗ Refused: {failure_words(exc)}", "Nothing would be sent to it."
        )
    except ProviderError as exc:
        return AttestationCheck("error", f"The check couldn't run: {failure_words(exc)}")
    finally:
        provider.close()
    without = (
        "" if keyed else " Without a Chutes key, discovery and the key binding weren't checked."
    )
    # Without the key nothing was sealed to anything: say what was checked,
    # not what a message would be.
    what = what_was_attested(attestation) if keyed else KEYLESS_CHUTES
    return AttestationCheck(
        "partial",
        f"⚠ End-to-end encrypted through Chutes, partially: {', '.join(attestation.shortfalls)} "
        f"({attestation.instances} instance{'s' if attestation.instances != 1 else ''})" + without,
        "\n\n".join(p for p in (what, attestation.detail) if p),
    )


KEYLESS_CHUTES = (
    "Checked without a key, from Chutes' public evidence for our nonce: each instance's TDX "
    "quote up to Intel's root, NVIDIA's verdict on its GPUs, and its VM image among those "
    "Chutes publishes. Not checked: that a quote binds the key a message would be sealed to "
    "(only Chutes' list of instances, which needs the key, names it). With the key, that is "
    "checked before anything is sealed. The workload that holds the key is Chutes' signed "
    "program, not measured, so it is partial either way."
)
