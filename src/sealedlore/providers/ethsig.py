"""Ethereum-style message signatures, in pure Python: Keccak-256 and secp256k1
public-key recovery, enough to check a TEE model's signed reply.

nano-gpt's TEE models sign each response with a key that lives only inside
the attested enclave; the signature is EIP-191 ("personal_sign") over a text,
and checking it means recovering the signer's address and comparing it with
the address the attestation vouched for. `eth-account` does this, but pulls in
about eight packages (some compiled) for two small functions, so they are
here instead, checked against fixed vectors in tests/test_tee.py.

Not constant-time and not for signing anything real: `sign` exists so the
tests can round-trip.
"""

from __future__ import annotations

# --- Keccak-256 (the pre-standard padding Ethereum uses, not SHA3-256) ---------

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
# Rotation offsets, indexed [x][y].
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1
_RATE = 136  # bytes, for a 256-bit output


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(lanes: list[list[int]]) -> None:
    for rc in _RC:
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                lanes[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(lanes[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                lanes[x][y] = b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y] & _MASK)
        lanes[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE:
        padded.append(0)
    padded[-1] |= 0x80
    lanes = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), _RATE):
        block = padded[offset : offset + _RATE]
        for i in range(_RATE // 8):
            x, y = i % 5, i // 5
            lanes[x][y] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        _keccak_f(lanes)
    out = b"".join(lanes[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


# --- secp256k1 ---------------------------------------------------------------------

_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = tuple[int, int] | None


def _add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        slope = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P) % _P
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P) % _P
    x = (slope * slope - a[0] - b[0]) % _P
    return x, (slope * (a[0] - x) - a[1]) % _P


def _mul(k: int, point: Point) -> Point:
    result: Point = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _address(point: Point) -> str:
    assert point is not None
    public = point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")
    return "0x" + keccak256(public)[-20:].hex()


def address_of(private_key: int) -> str:
    return checksum(_address(_mul(private_key, _G)))


def checksum(address: str) -> str:
    """EIP-55 mixed-case form."""
    bare = address.lower().removeprefix("0x")
    digest = keccak256(bare.encode()).hex()
    return "0x" + "".join(
        char.upper() if char.isalpha() and int(digest[i], 16) >= 8 else char
        for i, char in enumerate(bare)
    )


def personal_hash(message: bytes) -> bytes:
    """EIP-191 version 0x45: what `personal_sign` signs."""
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(message)).encode() + message)


def recover(message_hash: bytes, signature: bytes) -> str:
    """The address that signed `message_hash` (65 bytes: r, s, v)."""
    if len(signature) != 65:
        raise ValueError(f"a signature is 65 bytes, not {len(signature)}")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    v = signature[64]
    recid = v - 27 if v >= 27 else v
    if not (0 < r < _N and 0 < s < _N) or recid not in (0, 1, 2, 3):
        raise ValueError("not a valid signature")
    x = r + (_N if recid >= 2 else 0)
    if x >= _P:
        raise ValueError("not a valid signature")
    y_squared = (pow(x, 3, _P) + 7) % _P
    y = pow(y_squared, (_P + 1) // 4, _P)
    if y * y % _P != y_squared:
        raise ValueError("not a valid signature")
    if y % 2 != recid % 2:
        y = _P - y
    z = int.from_bytes(message_hash, "big")
    r_inverse = pow(r, -1, _N)
    point = _add(_mul(s * r_inverse % _N, (x, y)), _mul((-z * r_inverse) % _N, _G))
    if point is None:
        raise ValueError("not a valid signature")
    return checksum(_address(point))


def recover_personal(text: str, signature_hex: str) -> str:
    return recover(
        personal_hash(text.encode("utf-8")), bytes.fromhex(signature_hex.removeprefix("0x"))
    )


def sign(message_hash: bytes, private_key: int, nonce: int) -> bytes:
    """For the tests only: a signature by `private_key` with nonce `nonce`."""
    z = int.from_bytes(message_hash, "big")
    point = _mul(nonce, _G)
    assert point is not None
    r = point[0] % _N
    s = pow(nonce, -1, _N) * (z + r * private_key) % _N
    recid = (point[1] & 1) | (2 if point[0] >= _N else 0)
    if s > _N // 2:
        s, recid = _N - s, recid ^ 1
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + recid])
