"""A QR code for the pairing link (ISO/IEC 18004), written here so that no
dependency is added for one picture.

Byte mode, error correction level M, versions 1 to 10. A pairing link is
about a hundred bytes (version 6), and level M still reads off a screen
photographed at an angle. The result is a list of rows, True for a dark
module, with no quiet zone: the window draws it with one.
"""

from __future__ import annotations

MAX_VERSION = 10

# Level M, by version: EC codewords per block, and the blocks as
# (how many, data codewords in each).
_BLOCKS_M: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {
    1: (10, ((1, 16),)),
    2: (16, ((1, 28),)),
    3: (26, ((1, 44),)),
    4: (18, ((2, 32),)),
    5: (24, ((2, 43),)),
    6: (16, ((4, 27),)),
    7: (18, ((4, 31),)),
    8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}
_ALIGNMENT: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}
_LEVEL_M = 0b00  # level M's two format bits
_FORMAT_MASK = 0x5412
_FORMAT_GENERATOR = 0x537
_VERSION_GENERATOR = 0x1F25

# The penalty weights the standard gives for choosing a mask.
_RUN, _BLOCK, _FINDER_LIKE, _BALANCE = 3, 3, 40, 10
_FINDER_LIKE_PATTERNS = ("10111010000", "00001011101")


class QrTooLong(ValueError):
    """The text needs a larger QR code than this encoder makes."""


Matrix = list[list[bool]]


def data_codewords(version: int) -> int:
    _, blocks = _BLOCKS_M[version]
    return sum(count * size for count, size in blocks)


def byte_capacity(version: int) -> int:
    """How many bytes fit at this version (mode and count bits taken out)."""
    return (data_codewords(version) * 8 - 4 - _count_bits(version)) // 8


def encode(text: str | bytes, *, mask: int | None = None) -> Matrix:
    """The QR code for `text` (UTF-8), the smallest version it fits.

    The mask is the one with the lowest penalty unless one is given (tests
    give one to compare with another encoder's output).
    """
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = next((v for v in range(1, MAX_VERSION + 1) if byte_capacity(v) >= len(data)), None)
    if version is None:
        raise QrTooLong(f"{len(data)} bytes; at most {byte_capacity(MAX_VERSION)} fit")
    codewords = _with_error_correction(_data_codewords(data, version), version)
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]
    _draw_function_patterns(modules, function, version)
    _place_data(modules, function, codewords)
    if mask is None:
        mask = min(range(8), key=lambda m: _penalty(_masked(modules, function, m)))
    if not 0 <= mask <= 7:
        raise ValueError(f"mask {mask}: must be 0 to 7")
    return _masked(modules, function, mask)


# --- codewords ---------------------------------------------------------------


def _count_bits(version: int) -> int:
    return 8 if version < 10 else 16


def _data_codewords(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> i) & 1 for i in range(width - 1, -1, -1))

    put(0b0100, 4)  # byte mode
    put(len(data), _count_bits(version))
    for byte in data:
        put(byte, 8)
    capacity = data_codewords(version) * 8
    bits.extend([0] * min(4, capacity - len(bits)))  # the terminator
    bits.extend([0] * (-len(bits) % 8))
    words = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(words) < data_codewords(version):
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    return words


def _with_error_correction(data: list[int], version: int) -> list[int]:
    ec_size, groups = _BLOCKS_M[version]
    blocks: list[list[int]] = []
    start = 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[start : start + size])
            start += size
    ec_blocks = [_reed_solomon(block, ec_size) for block in blocks]
    out: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        out.extend(block[i] for block in blocks if i < len(block))
    for i in range(ec_size):
        out.extend(block[i] for block in ec_blocks)
    return out


_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _multiply(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The generator polynomial, highest power first."""
    poly = [1]
    for i in range(degree):
        poly = [
            (poly[j] if j < len(poly) else 0) ^ (_multiply(poly[j - 1], _EXP[i]) if j else 0)
            for j in range(len(poly) + 1)
        ]
    return poly


def _reed_solomon(data: list[int], degree: int) -> list[int]:
    generator = _generator(degree)
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for i in range(degree):
            remainder[i] ^= _multiply(generator[i + 1], factor)
    return remainder


# --- the matrix --------------------------------------------------------------


def _set(modules: Matrix, function: Matrix, row: int, col: int, dark: bool) -> None:
    modules[row][col] = dark
    function[row][col] = True


def _draw_function_patterns(modules: Matrix, function: Matrix, version: int) -> None:
    size = len(modules)
    for i in range(size):
        _set(modules, function, 6, i, i % 2 == 0)
        _set(modules, function, i, 6, i % 2 == 0)
    for row, col in ((3, 3), (3, size - 4), (size - 4, 3)):
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size:
                    _set(modules, function, r, c, max(abs(dr), abs(dc)) not in (2, 4))
    places = _ALIGNMENT[version]
    corners = (
        {(places[0], places[0]), (places[0], places[-1]), (places[-1], places[0])}
        if places
        else set()
    )
    for row in places:
        for col in places:
            if (row, col) in corners:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    _set(modules, function, row + dr, col + dc, max(abs(dr), abs(dc)) != 1)
    _draw_format(modules, function, 0)  # reserved; drawn again with the mask
    if version >= 7:
        remainder = version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * _VERSION_GENERATOR)
        bits = version << 12 | remainder
        for i in range(18):
            dark = bool((bits >> i) & 1)
            a, b = size - 11 + i % 3, i // 3
            _set(modules, function, b, a, dark)
            _set(modules, function, a, b, dark)


def _format_bits(mask: int) -> int:
    data = _LEVEL_M << 3 | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * _FORMAT_GENERATOR)
    return (data << 10 | remainder) ^ _FORMAT_MASK


def _draw_format(modules: Matrix, function: Matrix, mask: int) -> None:
    size = len(modules)
    bits = _format_bits(mask)

    def bit(i: int) -> bool:
        return bool((bits >> i) & 1)

    for i in range(6):
        _set(modules, function, i, 8, bit(i))
    _set(modules, function, 7, 8, bit(6))
    _set(modules, function, 8, 8, bit(7))
    _set(modules, function, 8, 7, bit(8))
    for i in range(9, 15):
        _set(modules, function, 8, 14 - i, bit(i))
    for i in range(8):
        _set(modules, function, 8, size - 1 - i, bit(i))
    for i in range(8, 15):
        _set(modules, function, size - 15 + i, 8, bit(i))
    _set(modules, function, size - 8, 8, True)  # the dark module


def _place_data(modules: Matrix, function: Matrix, codewords: list[int]) -> None:
    size = len(modules)
    total = len(codewords) * 8
    i = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1  # the vertical timing pattern's column is skipped
        upward = (right + 1) & 2 == 0
        for step in range(size):
            row = size - 1 - step if upward else step
            for col in (right, right - 1):
                if not function[row][col] and i < total:
                    modules[row][col] = bool((codewords[i >> 3] >> (7 - (i & 7))) & 1)
                    i += 1
    # What is left over (the remainder bits) stays light.


def _mask_dark(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return row * col % 2 + row * col % 3 == 0
    if mask == 6:
        return (row * col % 2 + row * col % 3) % 2 == 0
    return ((row + col) % 2 + row * col % 3) % 2 == 0


def _masked(modules: Matrix, function: Matrix, mask: int) -> Matrix:
    out = [
        [dark ^ (not function[r][c] and _mask_dark(mask, r, c)) for c, dark in enumerate(row)]
        for r, row in enumerate(modules)
    ]
    _draw_format(out, [row[:] for row in function], mask)
    return out


def _penalty(modules: Matrix) -> int:
    size = len(modules)
    lines = modules + [[modules[r][c] for r in range(size)] for c in range(size)]
    score = 0
    for line in lines:
        run = 1
        for a, b in zip(line, line[1:], strict=False):
            if a == b:
                run += 1
                continue
            if run >= 5:
                score += _RUN + run - 5
            run = 1
        if run >= 5:
            score += _RUN + run - 5
        text = "0000" + "".join("1" if dark else "0" for dark in line) + "0000"
        for pattern in _FINDER_LIKE_PATTERNS:
            start = text.find(pattern)
            while start != -1:
                score += _FINDER_LIKE
                start = text.find(pattern, start + 1)
    for r in range(size - 1):
        for c in range(size - 1):
            if modules[r][c] == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                score += _BLOCK
    dark = sum(sum(row) for row in modules)
    total = size * size
    score += _BALANCE * ((abs(dark * 20 - total * 10) + total - 1) // total - 1)
    return score
