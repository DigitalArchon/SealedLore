"""Intel TDX quote verification (DCAP), for the TEE/ models' attestation.

Ported from the author's AI-Assistant app (assistant/dcap.py, Sept 2026), with httpx
for requests; the checks are the same.

A TEE/ model's attestation carries an Intel TDX quote: a report of the trust domain the
model runs in, signed by Intel's quoting enclave (QE) on that machine. providers/tee.py
checks that the report binds the enclave's signing key to our nonce. This module checks that
the quote itself is genuine and current, the way Intel's own Quote Verification Library
does, from the quote's bytes and Intel's published collateral:

  1. The PCK certificate chain carried in the quote (PCK leaf -> PCK Platform or
     Processor CA) verifies up to Intel's SGX Root CA, which is pinned below rather than
     taken from the quote. Every certificate must be within its validity period.
  2. The QE report is signed by that PCK key, and its report data is
     sha256(attestation key || QE authentication data): the QE vouches for the key that
     signed the quote.
  3. The quote - header and TD report, report data (our nonce) included - is signed by
     that attestation key.
  4. Neither the PCK certificate nor its CA is revoked (Intel's PCK and root CRLs,
     signature-checked).
  5. The TCB: Intel's signed TCB info for the platform's FMSPC gives a status for its
     microcode and firmware (from the PCK certificate) and its TDX module (from the TD
     report); Intel's signed QE identity checks the quoting enclave and gives it a
     status too. Both are signed by Intel's TCB signing key, chained to the same root.
  6. The TD is not a debug TD, whose memory the host can read.

A failure of 1-4 or 6, or a TCB that is Revoked or older than any Intel lists, is a quote
that must not be trusted: DcapError, and nothing is sent. Collateral that cannot be
fetched (5, and the CRLs: Intel's service unreachable or answering with an error) is
reported as that, not as a pass: the caller shows the attestation as partial. So is any
TCB status but UpToDate, named as Intel names it. Collateral that did arrive but cannot
be read (a CRL or signed document that doesn't parse, a missing issuer chain) is not an
outage: it refuses like a bad signature would, since an answer shaped to fail parsing
must not buy a pass as "unchecked".

Checked live against TEE/glm-5.3-flash's quote (Sept 2026): version 4, ECDSA-256
attestation key, TDX; PCK Platform CA; FMSPC 20a06f000000; TDX module TDX_03; every
status UpToDate.

Collateral is cached for an hour: it is the same for every quote from a platform, and
Intel reissues it monthly.
"""

import hashlib
import json
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx

from sealedlore.providers.http import make_client

# Intel SGX Root CA, as Intel publishes it at
# https://certificates.trustedservices.intel.com/Intel_SGX_Provisioning_Certification_RootCA.cer
# (sha256 44a0196b2b99f889b8e149e95b807a350e7424964399e885a7cbb8ccfab674d3). Every chain
# is verified up to this copy; the root a quote carries is never trusted for itself.
INTEL_ROOT_PEM = b"""-----BEGIN CERTIFICATE-----
MIICjzCCAjSgAwIBAgIUImUM1lqdNInzg7SVUr9QGzknBqwwCgYIKoZIzj0EAwIw
aDEaMBgGA1UEAwwRSW50ZWwgU0dYIFJvb3QgQ0ExGjAYBgNVBAoMEUludGVsIENv
cnBvcmF0aW9uMRQwEgYDVQQHDAtTYW50YSBDbGFyYTELMAkGA1UECAwCQ0ExCzAJ
BgNVBAYTAlVTMB4XDTE4MDUyMTEwNDUxMFoXDTQ5MTIzMTIzNTk1OVowaDEaMBgG
A1UEAwwRSW50ZWwgU0dYIFJvb3QgQ0ExGjAYBgNVBAoMEUludGVsIENvcnBvcmF0
aW9uMRQwEgYDVQQHDAtTYW50YSBDbGFyYTELMAkGA1UECAwCQ0ExCzAJBgNVBAYT
AlVTMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEC6nEwMDIYZOj/iPWsCzaEKi7
1OiOSLRFhWGjbnBVJfVnkY4u3IjkDYYL0MxO4mqsyYjlBalTVYxFP2sJBK5zlKOB
uzCBuDAfBgNVHSMEGDAWgBQiZQzWWp00ifODtJVSv1AbOScGrDBSBgNVHR8ESzBJ
MEegRaBDhkFodHRwczovL2NlcnRpZmljYXRlcy50cnVzdGVkc2VydmljZXMuaW50
ZWwuY29tL0ludGVsU0dYUm9vdENBLmRlcjAdBgNVHQ4EFgQUImUM1lqdNInzg7SV
Ur9QGzknBqwwDgYDVR0PAQH/BAQDAgEGMBIGA1UdEwEB/wQIMAYBAf8CAQEwCgYI
KoZIzj0EAwIDSQAwRgIhAOW/5QkR+S9CiSDcNoowLuPRLsWGf/Yi7GSX94BgwTwg
AiEA4J0lrHoMs+Xo5o/sX6O9QWxHRAvZUGOdRQ7cvqRXaqI=
-----END CERTIFICATE-----
"""
INTEL_ROOT_SHA256 = "44a0196b2b99f889b8e149e95b807a350e7424964399e885a7cbb8ccfab674d3"

PCS = "https://api.trustedservices.intel.com"
TCB_INFO_URL = PCS + "/tdx/certification/v4/tcb"
QE_IDENTITY_URL = PCS + "/tdx/certification/v4/qe/identity"
PCK_CRL_URL = PCS + "/sgx/certification/v4/pckcrl"
ROOT_CRL_URL = "https://certificates.trustedservices.intel.com/IntelSGXRootCA.der"
TIMEOUT_SECONDS = 30
COLLATERAL_TTL = 3600

INTEL_QE_VENDOR_ID = bytes.fromhex("939a7233f79c4ca9940a0db3957f0607")
TDX_TEE_TYPE = 0x81
ECDSA_P256 = 2
HEADER_LEN = 48
TD_REPORT_LEN = 584  # TDX 1.0 and 1.5 TD report body in a version 4 quote
QE_REPORT_LEN = 384
PCK_CHAIN = 5  # certification data: the PCK chain, PEM
QE_REPORT_DATA = 6  # certification data: QE report, then the PCK chain

SGX_EXTENSION = "1.2.840.113741.1.13.1"

# Intel's TCB statuses, best first; a combination of statuses takes the worst.
STATUSES = [
    "UpToDate",
    "SWHardeningNeeded",
    "ConfigurationNeeded",
    "ConfigurationAndSWHardeningNeeded",
    "OutOfDate",
    "OutOfDateConfigurationNeeded",
    "Revoked",
]


class DcapError(Exception):
    """The quote is not one to trust: forged, revoked, debug, or unrecognised."""


class CollateralUnavailable(Exception):
    """Intel's collateral could not be fetched: the service out of reach, or an
    error status. What arrives and can't be read is a DcapError instead."""


# ---- the quote ----------------------------------------------------------------------


@dataclass
class Quote:
    signed: bytes  # header + TD report: what the attestation key signed
    body: bytes  # the TD report
    signature: bytes  # r || s
    attestation_key: bytes  # x || y
    qe_report: bytes
    qe_report_signature: bytes
    qe_auth_data: bytes
    pck_chain_pem: bytes

    @property
    def tee_tcb_svn(self) -> bytes:
        return self.body[0:16]

    @property
    def mr_signer_seam(self) -> bytes:
        return self.body[64:112]

    @property
    def seam_attributes(self) -> bytes:
        return self.body[112:120]

    @property
    def td_attributes(self) -> bytes:
        return self.body[120:128]

    @property
    def report_data(self) -> bytes:
        return self.body[520:584]


def parse_quote(raw: bytes) -> Quote:
    """Split a version 4 TDX quote into its parts. Raises DcapError if it is not one."""
    try:
        version, key_type, tee_type = struct.unpack_from("<HHI", raw, 0)
        if version != 4:
            raise DcapError(f"quote version {version}, not 4")
        if key_type != ECDSA_P256:
            raise DcapError(f"attestation key type {key_type}, not ECDSA-256")
        if tee_type != TDX_TEE_TYPE:
            raise DcapError(f"TEE type {tee_type:#x}, not TDX")
        if raw[12:28] != INTEL_QE_VENDOR_ID:
            raise DcapError("the quoting enclave is not Intel's")
        end = HEADER_LEN + TD_REPORT_LEN
        (length,) = struct.unpack_from("<I", raw, end)
        data = raw[end + 4 : end + 4 + length]
        if len(data) != length:
            raise DcapError("the quote is truncated")
        signature, attestation_key = data[0:64], data[64:128]
        cert_type, cert_size = struct.unpack_from("<HI", data, 128)
        cert = data[134 : 134 + cert_size]
        if cert_type != QE_REPORT_DATA:
            raise DcapError(f"certification data type {cert_type}, not a QE report")
        qe_report, qe_signature = cert[0:QE_REPORT_LEN], cert[QE_REPORT_LEN : QE_REPORT_LEN + 64]
        at = QE_REPORT_LEN + 64
        (auth_len,) = struct.unpack_from("<H", cert, at)
        auth = cert[at + 2 : at + 2 + auth_len]
        at += 2 + auth_len
        inner_type, inner_size = struct.unpack_from("<HI", cert, at)
        if inner_type != PCK_CHAIN:
            raise DcapError(f"inner certification data type {inner_type}, not a PCK chain")
        chain = cert[at + 6 : at + 6 + inner_size].rstrip(b"\0")
    except struct.error as e:
        raise DcapError(f"the quote is malformed ({e})") from e
    return Quote(
        signed=raw[0:end],
        body=raw[HEADER_LEN:end],
        signature=signature,
        attestation_key=attestation_key,
        qe_report=qe_report,
        qe_report_signature=qe_signature,
        qe_auth_data=auth,
        pck_chain_pem=chain,
    )


# ---- DER, just enough for Intel's SGX certificate extension ---------------------------


def _der(data: bytes, at: int = 0) -> tuple[int, bytes, int]:
    """(tag, value, next offset) of the DER element at `at`."""
    tag = data[at]
    length = data[at + 1]
    at += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[at : at + count], "big")
        at += count
    return tag, data[at : at + length], at + length


def _children(value: bytes) -> Iterator[tuple[int, bytes]]:
    at = 0
    while at < len(value):
        tag, inner, at = _der(value, at)
        yield tag, inner


def _oid(value: bytes) -> str:
    first = value[0]
    parts = [str(min(first // 40, 2)), str(first - 40 * min(first // 40, 2))]
    number = 0
    for byte in value[1:]:
        number = (number << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(number))
            number = 0
    return ".".join(parts)


def sgx_extension(value: bytes) -> dict[str, Any]:
    """{fmspc, pcesvn, cpusvn: [16 ints]} from the PCK certificate's SGX extension."""
    out = {}
    _tag, seq, _next = _der(value)
    for _tag, entry in _children(seq):
        items = list(_children(entry))
        oid = _oid(items[0][1])
        tag, content = items[1]
        if oid == SGX_EXTENSION + ".4":
            out["fmspc"] = content.hex()
        elif oid == SGX_EXTENSION + ".2":
            svns = {}
            for _t, component in _children(content):
                parts = list(_children(component))
                svns[_oid(parts[0][1])] = int.from_bytes(parts[1][1], "big")
            out["cpusvn"] = [svns.get(f"{SGX_EXTENSION}.2.{i}", 0) for i in range(1, 17)]
            out["pcesvn"] = svns.get(f"{SGX_EXTENSION}.2.17", 0)
    if set(out) != {"fmspc", "cpusvn", "pcesvn"}:
        raise DcapError("the PCK certificate's SGX extension is incomplete")
    return out


# ---- signatures ---------------------------------------------------------------------


def _crypto() -> tuple[Any, Any, Any, Any, Any]:
    """cryptography's pieces. Raises ImportError when it is not installed."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    return x509, InvalidSignature, hashes, ec, encode_dss_signature


def _raw_verify(public_key: Any, signature: bytes, data: bytes) -> bool:
    """An ECDSA P-256/SHA-256 signature given as r || s."""
    _x509, InvalidSignature, hashes, ec, encode = _crypto()
    half = len(signature) // 2
    der = encode(int.from_bytes(signature[:half], "big"), int.from_bytes(signature[half:], "big"))
    try:
        public_key.verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def _key_from_xy(xy: bytes) -> Any:
    _x509, _invalid, _hashes, ec, _encode = _crypto()
    return ec.EllipticCurvePublicNumbers(
        int.from_bytes(xy[:32], "big"), int.from_bytes(xy[32:], "big"), ec.SECP256R1()
    ).public_key()


def _utc(cert: Any, name: str) -> datetime:
    value = getattr(cert, f"{name}_utc", None)
    if value is None:  # cryptography < 42
        value = getattr(cert, name).replace(tzinfo=UTC)
    return value


def intel_root() -> Any:
    x509 = _crypto()[0]
    root = x509.load_pem_x509_certificate(INTEL_ROOT_PEM)
    from cryptography.hazmat.primitives.serialization import Encoding

    if hashlib.sha256(root.public_bytes(Encoding.DER)).hexdigest() != INTEL_ROOT_SHA256:
        raise DcapError("the pinned Intel root certificate is damaged")
    return root


def verify_chain(certs: list[Any], root: Any, now: datetime) -> None:
    """Each certificate signed by the next, the last by `root`, all of them current."""
    _x509, InvalidSignature, _hashes, ec, _encode = _crypto()
    chain = list(certs) + [root]
    for cert, issuer in zip(chain, chain[1:], strict=False):
        subject = cert.subject.rfc4514_string()
        if cert.issuer != issuer.subject:
            raise DcapError(f"{subject} was not issued by {issuer.subject.rfc4514_string()}")
        try:
            issuer.public_key().verify(
                cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm)
            )
        except InvalidSignature:
            raise DcapError(f"{subject}'s signature does not verify") from None
    for cert in chain:
        if not _utc(cert, "not_valid_before") <= now <= _utc(cert, "not_valid_after"):
            raise DcapError(f"{cert.subject.rfc4514_string()} is outside its validity period")


def _common_name(cert: Any) -> str:
    x509 = _crypto()[0]
    names = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return names[0].value if names else ""


# ---- collateral ---------------------------------------------------------------------

_cache: dict[tuple, tuple[float, bytes, dict[str, str]]] = {}
_cache_lock = threading.Lock()


def _fetch(
    session: httpx.Client, url: str, params: dict[str, str] | None = None
) -> tuple[bytes, dict[str, str]]:
    """(body bytes, headers) of Intel's collateral, cached for COLLATERAL_TTL."""
    key = (url, tuple(sorted((params or {}).items())))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < COLLATERAL_TTL:
            return hit[1], hit[2]
    try:
        response = session.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise CollateralUnavailable(f"{url}: {e}") from e
    if response.status_code != 200:
        raise CollateralUnavailable(f"{url}: HTTP {response.status_code}")
    content = response.content
    headers = dict(response.headers)
    with _cache_lock:
        _cache[key] = (time.monotonic(), content, headers)
    return content, headers


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return unquote(value)
    raise DcapError(f"Intel's response had no {name}")


def _load_crl(data: bytes) -> Any:
    x509 = _crypto()[0]
    try:
        return x509.load_der_x509_crl(data)
    except ValueError:
        try:
            return x509.load_pem_x509_crl(data)
        except ValueError:
            raise DcapError("a CRL from Intel could not be read") from None


def _check_crl(crl: Any, issuer: Any, now: datetime, *certs: Any) -> bool:
    if not crl.is_signature_valid(issuer.public_key()):
        raise DcapError(f"{_common_name(issuer)}'s CRL is not signed by it")
    update = getattr(crl, "next_update_utc", None) or (
        crl.next_update.replace(tzinfo=UTC) if crl.next_update else None
    )
    for cert in certs:
        if crl.get_revoked_certificate_by_serial_number(cert.serial_number) is not None:
            raise DcapError(f"{cert.subject.rfc4514_string()} has been revoked by Intel")
    return update is not None and update < now


def _signed_json(
    content: bytes, key: str, chain_pem: str, root: Any, now: datetime
) -> tuple[dict[str, Any], Any]:
    """The object under `key`, once Intel's signature over its exact bytes verifies."""
    x509 = _crypto()[0]
    text = content.decode("utf-8")
    start = text.find(f'"{key}"')
    if start < 0:
        raise DcapError(f"Intel's {key} was missing from its reply")
    start = text.index("{", start)
    depth, in_string, escaped = 0, False, False
    for end in range(start, len(text)):
        char = text[end]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
    raw = text[start : end + 1]
    document = json.loads(text)
    signer = x509.load_pem_x509_certificates(chain_pem.encode())
    verify_chain(signer[:-1] if len(signer) > 1 else signer, root, now)
    if not _raw_verify(
        signer[0].public_key(), bytes.fromhex(document["signature"]), raw.encode("utf-8")
    ):
        raise DcapError(f"Intel's signature on its {key} does not verify")
    return json.loads(raw), signer[0]


def _stale(document: dict[str, Any], now: datetime) -> bool:
    try:
        update = datetime.strptime(document["nextUpdate"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, ValueError):
        return True
    return update.replace(tzinfo=UTC) < now


# ---- the TCB ------------------------------------------------------------------------


def _worst(*statuses: str) -> str:
    known = [s for s in statuses if s]
    return max(known, key=lambda s: STATUSES.index(s) if s in STATUSES else len(STATUSES))


def platform_level(
    tcb_info: dict[str, Any], sgx: dict[str, Any], tee_tcb_svn: bytes
) -> dict[str, Any] | None:
    """The first (highest) TCB level this platform meets, or None.

    As Intel's QVL: the PCK certificate's CPU SVN components and PCE SVN against the
    level's SGX components, then the TD report's TEE TCB SVN against its TDX components
    - from index 2 when TEE_TCB_SVN[1], the TDX module's major version, is set, since the
    module itself is then judged by tdxModuleIdentities instead.
    """
    start = 2 if tee_tcb_svn[1] > 0 else 0
    for level in tcb_info.get("tcbLevels", []):
        tcb = level["tcb"]
        wanted = [c["svn"] for c in tcb["sgxtcbcomponents"]]
        if any(have < want for have, want in zip(sgx["cpusvn"], wanted, strict=False)):
            continue
        if sgx["pcesvn"] < tcb["pcesvn"]:
            continue
        tdx = [c["svn"] for c in tcb.get("tdxtcbcomponents", [])]
        if any(tee_tcb_svn[i] < tdx[i] for i in range(start, min(16, len(tdx)))):
            continue
        return level
    return None


def _masked(value: bytes, mask: bytes) -> bytes:
    return bytes(a & b for a, b in zip(value, mask, strict=False))


def module_status(tcb_info: dict[str, Any], quote: Quote) -> str | None:
    """The TDX module's status, or None for a module 1.0 (judged by its platform level).
    Raises DcapError when the module is not Intel's or older than any Intel lists."""
    tee = quote.tee_tcb_svn
    major, minor = tee[1], tee[0]
    if major == 0:
        module = tcb_info.get("tdxModule") or {}
        identity, levels = module, None
        name = "TDX module 1.0"
    else:
        name = f"TDX_{major:02d}"
        identity = next(
            (m for m in tcb_info.get("tdxModuleIdentities", []) if m.get("id") == name), None
        )
        if identity is None:
            raise DcapError(f"the TDX module {name} is not one Intel lists")
        levels = identity.get("tcbLevels", [])
    if quote.mr_signer_seam != bytes.fromhex(identity.get("mrsigner", "")):
        raise DcapError(f"the {name} module was not signed by Intel")
    mask = bytes.fromhex(identity.get("attributesMask", "00" * 8))
    if _masked(quote.seam_attributes, mask) != bytes.fromhex(identity.get("attributes", "00" * 8)):
        raise DcapError(f"the {name} module's attributes are not Intel's")
    if levels is None:
        return None
    for level in levels:
        if minor >= level["tcb"]["isvsvn"]:
            return level["tcbStatus"]
    raise DcapError(f"the {name} module (SVN {minor}) is older than any Intel lists")


def qe_status(identity: dict[str, Any], qe_report: bytes) -> str:
    """The quoting enclave's status. Raises DcapError when it is not Intel's QE."""
    if identity.get("id") != "TD_QE":
        raise DcapError(f"Intel's QE identity is for {identity.get('id')}, not TD_QE")
    (miscselect,) = struct.unpack_from("<I", qe_report, 16)
    if miscselect & int(identity["miscselectMask"], 16) != int(identity["miscselect"], 16):
        raise DcapError("the quoting enclave's MISCSELECT is not Intel's")
    attributes = qe_report[48:64]
    if _masked(attributes, bytes.fromhex(identity["attributesMask"])) != bytes.fromhex(
        identity["attributes"]
    ):
        raise DcapError("the quoting enclave's attributes are not Intel's")
    if qe_report[128:160] != bytes.fromhex(identity["mrsigner"]):
        raise DcapError("the quoting enclave was not signed by Intel")
    (product,) = struct.unpack_from("<H", qe_report, 256)
    if product != identity["isvprodid"]:
        raise DcapError("the quoting enclave is not Intel's TDX QE")
    (svn,) = struct.unpack_from("<H", qe_report, 258)
    for level in identity.get("tcbLevels", []):
        if svn >= level["tcb"]["isvsvn"]:
            return level["tcbStatus"]
    raise DcapError(f"the quoting enclave (SVN {svn}) is older than any Intel lists")


# ---- the whole check ----------------------------------------------------------------


@dataclass
class Result:
    status: str = ""  # the combined TCB status; "" without collateral
    platform: str = ""
    module: str = ""
    qe: str = ""
    advisories: list[str] = field(default_factory=list)
    fmspc: str = ""
    collateral: bool = True  # False: the TCB and revocation were not checked
    notes: list[str] = field(default_factory=list)


def verify(raw: bytes, session: httpx.Client | None = None, now: datetime | None = None) -> Result:
    """Verify a TDX quote (bytes). Returns a Result, or raises DcapError.

    Raises ImportError when the `cryptography` package is not available: nothing can
    be verified then, and the caller must say so rather than pass it.
    """
    _crypto()  # ImportError here, before anything is claimed
    own = session is None
    session = session or make_client(ROOT_CRL_URL, timeout=httpx.Timeout(TIMEOUT_SECONDS))
    now = now or datetime.now(UTC)
    try:
        return _verify(raw, session, now)
    finally:
        if own:
            session.close()


def _verify(raw: bytes, session: httpx.Client, now: datetime) -> Result:
    x509 = _crypto()[0]
    quote = parse_quote(raw)

    if quote.td_attributes[0] & 0x01:
        raise DcapError("it is a debug TD, whose memory the host can read")

    root = intel_root()
    certs = x509.load_pem_x509_certificates(quote.pck_chain_pem)
    if len(certs) < 2:
        raise DcapError("the quote's PCK chain is incomplete")
    pck, ca = certs[0], certs[1]
    verify_chain([pck, ca], root, now)
    if not _raw_verify(pck.public_key(), quote.qe_report_signature, quote.qe_report):
        raise DcapError("the quoting enclave's report is not signed by the PCK key")
    expected = hashlib.sha256(quote.attestation_key + quote.qe_auth_data).digest()
    if quote.qe_report[320:352] != expected or any(quote.qe_report[352:384]):
        raise DcapError("the quoting enclave does not vouch for the attestation key")
    if not _raw_verify(_key_from_xy(quote.attestation_key), quote.signature, quote.signed):
        raise DcapError("the quote's signature does not verify")

    sgx = sgx_extension(
        pck.extensions.get_extension_for_oid(x509.ObjectIdentifier(SGX_EXTENSION)).value.value
    )
    result = Result(
        fmspc=sgx["fmspc"],
        notes=["Intel's certificate chain, the quoting enclave and the quote's signature verify"],
    )
    try:
        stale = False
        content, _headers = _fetch(session, ROOT_CRL_URL)
        stale |= _check_crl(_load_crl(content), root, now, ca)
        kind = "processor" if "Processor" in _common_name(ca) else "platform"
        content, _headers = _fetch(session, PCK_CRL_URL, {"ca": kind, "encoding": "der"})
        stale |= _check_crl(_load_crl(content), ca, now, pck)

        content, headers = _fetch(session, TCB_INFO_URL, {"fmspc": sgx["fmspc"]})
        tcb_info, signer = _signed_json(
            content, "tcbInfo", _header(headers, "TCB-Info-Issuer-Chain"), root, now
        )
        content, _root_headers = _fetch(session, ROOT_CRL_URL)
        _check_crl(_load_crl(content), root, now, signer)
        if tcb_info.get("id") != "TDX" or tcb_info.get("fmspc", "").lower() != sgx["fmspc"]:
            raise DcapError("Intel's TCB info is not for this platform")
        content, headers = _fetch(session, QE_IDENTITY_URL)
        identity, _signer = _signed_json(
            content,
            "enclaveIdentity",
            _header(headers, "SGX-Enclave-Identity-Issuer-Chain"),
            root,
            now,
        )
        stale |= _stale(tcb_info, now) or _stale(identity, now)
    except CollateralUnavailable as e:
        result.collateral = False
        result.notes.append(
            f"Intel's revocation lists and TCB information could not be "
            f"fetched ({e}), so revocation and the TCB were not checked"
        )
        return result
    except (ValueError, KeyError, TypeError) as e:
        # Fetched, but not what Intel publishes: refused, never "unchecked".
        raise DcapError(f"Intel's collateral could not be read ({e})") from e

    level = platform_level(tcb_info, sgx, quote.tee_tcb_svn)
    if level is None:
        raise DcapError("the platform's TCB is older than any level Intel lists")
    result.platform = level["tcbStatus"]
    result.advisories = list(level.get("advisoryIDs") or [])
    result.module = module_status(tcb_info, quote) or ""
    result.qe = qe_status(identity, quote.qe_report)
    result.status = _worst(result.platform, result.module, result.qe)
    if result.status == "Revoked":
        raise DcapError("Intel has revoked this platform's TCB")
    parts = [f"platform {result.platform}"]
    if result.module:
        parts.append(f"TDX module {result.module}")
    parts.append(f"quoting enclave {result.qe}")
    result.notes.append(
        "not revoked; Intel TCB "
        + ", ".join(parts)
        + (
            f" (advisories {', '.join(result.advisories)})"
            if result.advisories and result.status != "UpToDate"
            else ""
        )
    )
    if stale:
        result.collateral = False
        result.notes.append("some of Intel's collateral was past its next update")
    return result
