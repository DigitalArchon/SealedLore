"""The pairing link's QR code (phone/qr.py).

The golden matrices in tests/data/qr_golden.json were made once with segno
1.6.6 (byte mode, level M, the mask forced) and each was decoded by zxing-cpp
before it was kept. Only full symbols are compared: below a version's
capacity segno adds a zero byte the standard doesn't ask for, so the two
encoders differ there while both decode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.phone import qr

GOLDEN = Path(__file__).parent / "data" / "qr_golden.json"


def rows_of(matrix: qr.Matrix) -> list[str]:
    return ["".join("1" if dark else "0" for dark in row) for row in matrix]


@pytest.mark.parametrize(
    "case", json.loads(GOLDEN.read_text())["cases"], ids=lambda c: f"v{c['version']}"
)
def test_every_version_matches_the_reference_encoder(case):
    assert rows_of(qr.encode(case["text"], mask=case["mask"])) == case["rows"]


def test_reed_solomon_matches_the_worked_example():
    # "HELLO WORLD" at 1-M, the codewords as the usual worked example gives them.
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr._reed_solomon(data, 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


def test_byte_mode_codewords_end_with_the_terminator_then_alternate_pads():
    # 0100 | 00000101 | 'a'..'e' | 0000, then 0xEC 0x11 to the 16 codewords of 1-M.
    assert qr._data_codewords(b"abcde", 1) == [64, 86, 22, 38, 54, 70, 80] + [236, 17] * 4 + [236]


def test_format_bits_for_level_m():
    # The standard's table: level M, masks 0 to 7.
    assert [qr._format_bits(m) for m in range(8)] == [
        0b101010000010010,
        0b101000100100101,
        0b101111001111100,
        0b101101101001011,
        0b100010111111001,
        0b100000011001110,
        0b100111110010111,
        0b100101010100000,
    ]


def test_the_smallest_version_that_fits_is_used():
    assert len(qr.encode("x" * 14)) == 21
    assert len(qr.encode("x" * 15)) == 25
    assert len(qr.encode("x" * 213)) == 57


def test_a_pairing_link_fits_version_6():
    link = "http://" + "a" * 56 + ".onion/#pair=" + "A" * 26
    matrix = qr.encode(link)
    assert len(link.encode()) == 102
    assert len(matrix) == 41 and all(len(row) == 41 for row in matrix)


def test_text_too_long_is_refused():
    with pytest.raises(qr.QrTooLong):
        qr.encode("x" * 214)


def test_the_chosen_mask_is_the_one_with_the_lowest_penalty():
    text = "http://example.onion/#k=abc"
    scores = {}
    for mask in range(8):
        scores[mask] = qr._penalty(qr.encode(text, mask=mask))
    best = min(scores, key=lambda m: (scores[m], m))
    assert qr.encode(text) == qr.encode(text, mask=best)
