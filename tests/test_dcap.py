"""Intel's side of a TEE/ model's attestation (providers/dcap.py): what a quote is
held to since Nov 2026, as in the author's other apps that attest TEE models. Ported
from DA Vibe Manager's and DA Assistant's tests of their copies of the module. The
real capture's other checks are in test_tee.py."""

# ruff: noqa: F811 - the real capture's fixture comes from test_tee

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from urllib.parse import quote as url_quote

import httpx
import pytest

from sealedlore.providers import dcap
from tests.test_tee import intel, real  # noqa: F401

# --- the quoting enclave ------------------------------------------------------------------


def test_the_quoting_enclave_must_be_intels_whether_or_not_its_collateral_can_be_had(
    real, monkeypatch
):
    """It is what makes the attestation key the QE's, not any enclave's the platform
    certified. It was checked only against Intel's QE identity, fetched with the
    collateral: out of reach, or made so, a quote such an enclave signed passed as
    partial."""
    quote = dcap.parse_quote(bytes.fromhex(real["intel_quote"]))
    dcap.qe_is_intels(dcap.INTEL_TD_QE, quote.qe_report)  # the real one is Intel's
    other = bytearray(quote.qe_report)
    other[128:160] = b"\x11" * 32  # another's MRSIGNER
    with pytest.raises(dcap.DcapError, match="not signed by Intel"):
        dcap.qe_is_intels(dcap.INTEL_TD_QE, bytes(other))
    checked = []
    real_check = dcap.qe_is_intels
    monkeypatch.setattr(
        dcap,
        "qe_is_intels",
        lambda identity, report: checked.append(identity) or real_check(identity, report),
    )
    result = intel(real, intel_status=503)
    assert not result.collateral and checked == [dcap.INTEL_TD_QE]
    # The real quote, held to an identity its QE doesn't have, with Intel's
    # collateral out of reach: refused, not partial.
    monkeypatch.setattr(dcap, "INTEL_TD_QE", dict(dcap.INTEL_TD_QE, mrsigner="11" * 32))
    with pytest.raises(dcap.DcapError, match="not signed by Intel"):
        intel(real, intel_status=503)


def test_the_td_qe_identity_pinned_is_the_one_intel_signed(real):
    item = next(c for c in real["collateral"] if c["url"] == dcap.QE_IDENTITY_URL)
    signed = json.loads(base64.b64decode(item["body"]))["enclaveIdentity"]
    assert dcap.INTEL_TD_QE == {key: signed[key] for key in dcap.INTEL_TD_QE}


# --- the TCB ------------------------------------------------------------------------------


def test_a_revoked_tcb_is_refused_whatever_else_is_named(real, monkeypatch):
    """Only the worst status was compared with Revoked, and one this app doesn't list
    ranks above it: a platform Intel had revoked passed as partial beside it."""
    for platform, module, qe in (
        ("Revoked", "TDRelaunchAdvised", "UpToDate"),
        ("UpToDate", "Revoked", "SomethingNew"),
        ("SomethingNew", "UpToDate", "Revoked"),
    ):
        dcap._cache.clear()
        monkeypatch.setattr(dcap, "platform_level", lambda *_a, s=platform: {"tcbStatus": s})
        monkeypatch.setattr(dcap, "module_status", lambda *_a, s=module: s)
        monkeypatch.setattr(dcap, "qe_status", lambda *_a, s=qe: s)
        with pytest.raises(dcap.DcapError, match="revoked this platform"):
            intel(real)
    monkeypatch.undo()
    dcap._cache.clear()
    monkeypatch.setattr(dcap, "module_status", lambda *_a: "TDRelaunchAdvised")
    assert intel(real).status == "TDRelaunchAdvised"  # partial, and named


def test_tcb_information_signed_with_a_pck_chain_is_refused(real):
    """A PCK certificate chains to Intel's root just as the TCB signing one does, and
    its key lives in the very platform whose TCB is being judged."""
    pck_chain = dcap.parse_quote(bytes.fromhex(real["intel_quote"])).pck_chain_pem.decode()
    for index, header, key in (
        (2, "TCB-Info-Issuer-Chain", "tcbInfo"),
        (3, "SGX-Enclave-Identity-Issuer-Chain", "enclaveIdentity"),
    ):
        dcap._cache.clear()
        forged = json.loads(json.dumps(real))
        forged["collateral"][index]["headers"][header] = url_quote(pck_chain)
        with pytest.raises(
            dcap.DcapError, match=f"Intel's {key} isn't signed by Intel's TCB signing certificate"
        ):
            intel(forged)


# --- Intel's chains -----------------------------------------------------------------------
#
# verify_chain held a certificate to its issuer's name, signature and dates, and no more:
# any certificate Intel's root vouched for (the TCB signing one, a PCK certificate whose
# key a compromised platform gave up) could issue certificates of its own that passed as
# Intel's. And Intel's TCB information and QE identity were believed signed by any
# certificate chaining to the root. Synthetic chains, under a root of their own.

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def key_usage(cert_sign: bool):
    """keyUsage as Intel's certificates have it: a CA's key signs certificates and
    CRLs, any other's signs data."""
    from cryptography import x509

    return x509.KeyUsage(
        digital_signature=not cert_sign,
        content_commitment=not cert_sign,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def synthetic_cert(name, key, issuer=None, ca=None, path_length=None, cert_sign=None):
    """A certificate named `name` for `key`, signed by `issuer` (its certificate, its
    key) or by itself; with basicConstraints if `ca` is given, and keyUsage if
    `cert_sign` is."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer or (None, key)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_cert.subject if issuer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2018, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2049, 1, 1, tzinfo=UTC))
    )
    if ca is not None:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=ca, path_length=path_length), critical=True
        )
    if cert_sign is not None:
        builder = builder.add_extension(key_usage(cert_sign), critical=True)
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def keys():
    from cryptography.hazmat.primitives.asymmetric import ec

    return {name: ec.generate_private_key(ec.SECP256R1()) for name in ("root", "ca", "leaf")}


def root_cert(keys, path_length=1):
    return synthetic_cert(
        "Test Root CA", keys["root"], ca=True, path_length=path_length, cert_sign=True
    )


def under(keys, issuer, issuer_key, name, key, **extensions):
    return synthetic_cert(name, keys[key], (issuer, keys[issuer_key]), **extensions)


def test_a_chain_of_authorities_allowed_to_sign_verifies(keys):
    root = root_cert(keys)
    ca = under(keys, root, "root", "Test CA", "ca", ca=True, path_length=0, cert_sign=True)
    leaf = under(keys, ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
    dcap.verify_chain([leaf, ca], root, NOW)


def test_a_chain_through_a_certificate_that_is_no_ca_is_refused(keys):
    root = root_cert(keys)
    signing = under(keys, root, "root", "Intel SGX TCB Signing", "ca", ca=False, cert_sign=False)
    leaf = under(keys, signing, "ca", "Intel SGX PCK Certificate", "leaf", ca=False)
    with pytest.raises(
        dcap.DcapError,
        match="CN=Intel SGX TCB Signing signed a certificate, and is not a certificate authority",
    ):
        dcap.verify_chain([leaf, signing], root, NOW)
    # Nor one that doesn't say what it is.
    bare = under(keys, root, "root", "Bare", "ca")
    leaf = under(keys, bare, "ca", "Test leaf", "leaf", ca=False)
    with pytest.raises(
        dcap.DcapError, match="Bare signed a certificate, and is not a certificate authority"
    ):
        dcap.verify_chain([leaf, bare], root, NOW)


def test_an_authority_beyond_its_path_length_is_refused(keys):
    root = root_cert(keys, path_length=0)
    ca = under(keys, root, "root", "Test CA", "ca", ca=True, path_length=0, cert_sign=True)
    leaf = under(keys, ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
    with pytest.raises(
        dcap.DcapError,
        match="Test Root CA may have 0 certificate authorities below it, and the chain puts 1",
    ):
        dcap.verify_chain([leaf, ca], root, NOW)


def test_an_authority_whose_key_is_not_for_certificates_is_refused(keys):
    root = root_cert(keys)
    ca = under(keys, root, "root", "Test CA", "ca", ca=True, path_length=0, cert_sign=False)
    leaf = under(keys, ca, "ca", "Test leaf", "leaf", ca=False, cert_sign=False)
    with pytest.raises(
        dcap.DcapError, match="Test CA signed a certificate with a key not meant for that"
    ):
        dcap.verify_chain([leaf, ca], root, NOW)


def signed_tcb_info(keys, signer_name: str, *, through_ca: bool = False):
    """Intel's TCB info as its service sends it, signed by a certificate named
    `signer_name` that the root issued (or, `through_ca`, that a CA under the root
    issued: a PCK certificate's place)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.serialization import Encoding

    root = root_cert(keys)
    chain = [root]
    issuer, issuer_key = root, "root"
    if through_ca:
        ca = under(keys, root, "root", "Intel SGX PCK Platform CA", "ca", ca=True, cert_sign=True)
        chain.insert(0, ca)
        issuer, issuer_key = ca, "ca"
    signer = under(keys, issuer, issuer_key, signer_name, "leaf", ca=False, cert_sign=False)
    raw = json.dumps({"id": "TDX", "fmspc": "20a06f000000"}, separators=(",", ":"))
    r, s = decode_dss_signature(keys["leaf"].sign(raw.encode(), ec.ECDSA(hashes.SHA256())))
    signature = (r.to_bytes(32, "big") + s.to_bytes(32, "big")).hex()
    content = f'{{"tcbInfo":{raw},"signature":"{signature}"}}'.encode()
    pem = b"".join(cert.public_bytes(Encoding.PEM) for cert in [signer, *chain])
    return dcap._signed_json(content, "tcbInfo", pem.decode(), root, NOW)


def test_tcb_info_signed_by_any_but_the_tcb_signing_certificate_is_refused(keys):
    """Not by any certificate under Intel's root: a platform's PCK certificate (whose
    key an out-of-date platform can give up) would otherwise rate its own platform's
    TCB."""
    info, _signer = signed_tcb_info(keys, "Intel SGX TCB Signing")
    assert info["fmspc"] == "20a06f000000"
    for name, through_ca in (
        ("Intel SGX PCK Certificate", True),
        ("Intel SGX PCK Certificate", False),
        ("Intel SGX TCB Signing 2", False),
        ("Intel SGX TCB Signing", True),  # its name, but not issued by the root itself
    ):
        with pytest.raises(
            dcap.DcapError, match="Intel's tcbInfo isn't signed by Intel's TCB signing certificate"
        ):
            signed_tcb_info(keys, name, through_ca=through_ca)


# --- a clock that counts the time asleep --------------------------------------------------
#
# Intel's collateral (and NVIDIA's keys, and the attestations) were aged by
# time.monotonic, which stops while the machine is suspended: a laptop that slept for
# hours took them all for as fresh as when it went to sleep. Here the boot clock moves on
# while the monotonic one stands still, as across a suspend.


@pytest.fixture
def asleep(monkeypatch):
    """Advance it by `hours` to sleep that long."""
    if not hasattr(time, "CLOCK_BOOTTIME"):
        pytest.skip("no CLOCK_BOOTTIME here")
    clock = {"boot": 1000.0}
    real_gettime = time.clock_gettime

    def clock_gettime(which):
        return clock["boot"] if which == time.CLOCK_BOOTTIME else real_gettime(which)

    monkeypatch.setattr(time, "clock_gettime", clock_gettime)
    monkeypatch.setattr(time, "monotonic", lambda: 50.0)

    def sleep(hours: float) -> None:
        clock["boot"] += hours * 3600

    return sleep


def test_the_boot_clock_is_read_and_the_monotonic_one_only_without_it(asleep, monkeypatch):
    assert dcap.since_boot() == 1000.0
    asleep(1)
    assert dcap.since_boot() == 4600.0
    monkeypatch.setattr(dcap, "_BOOTTIME", None)
    assert dcap.since_boot() == 50.0
    monkeypatch.undo()

    def unavailable(_which):
        raise OSError("no such clock")

    monkeypatch.setattr(time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(time, "clock_gettime", unavailable)
    assert dcap.since_boot() == 50.0


def test_intels_collateral_is_fetched_again_after_a_sleep(asleep, monkeypatch):
    monkeypatch.setattr(dcap, "_cache", {})
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, content=b"collateral")

    session = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(2):
        assert dcap._fetch(session, dcap.ROOT_CRL_URL)[0] == b"collateral"
    assert len(fetched) == 1
    asleep(2)
    dcap._fetch(session, dcap.ROOT_CRL_URL)
    assert len(fetched) == 2
