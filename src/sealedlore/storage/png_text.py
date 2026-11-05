"""Text kept inside a PNG: read a picture's text chunks, write some into one.

A SillyTavern character card is a PNG with its card in a text chunk
(storage/tavern_cards.py). Pure bytes; nothing here decodes the pixels.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable

from sealedlore.storage.image_meta import PNG_SIGNATURE

# A compressed chunk's text may not inflate past this: a card is a few
# hundred kilobytes, and a chunk that claims more is not one.
MAX_INFLATED = 16 * 1024 * 1024
# Nor may all the text read from one picture come to more than this, however
# it was compressed: a card's PNG may be no larger (tavern_cards.MAX_BYTES),
# and a few small chunks can inflate to gigabytes.
MAX_TEXT = 64 * 1024 * 1024


class PngTextError(ValueError):
    """Not a PNG whose chunks can be followed, said in plain words."""


def _chunks(data: bytes) -> list[tuple[bytes, bytes, int, int]]:
    """(kind, body, start, stop) for every chunk, in order, up to IEND."""
    if not data.startswith(PNG_SIGNATURE):
        raise PngTextError("it isn't a PNG picture")
    found: list[tuple[bytes, bytes, int, int]] = []
    pos, end = len(PNG_SIGNATURE), len(data)
    while pos + 12 <= end:
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        stop = pos + 12 + length
        if stop > end:
            raise PngTextError("a PNG chunk runs past the end of the file")
        found.append((kind, data[pos + 8 : pos + 8 + length], pos, stop))
        if kind == b"IEND":
            return found
        pos = stop
    raise PngTextError("the PNG has no end: it may be cut off")


def _inflated(body: bytes) -> bytes:
    inflater = zlib.decompressobj()
    # A byte past the limit, so text that runs on past it is told from text
    # that ends at it.
    text = inflater.decompress(body, MAX_INFLATED + 1)
    if inflater.unconsumed_tail or len(text) > MAX_INFLATED:
        raise PngTextError("a compressed text chunk is too large")
    return text


def text_chunks(data: bytes, wanted: Callable[[str], bool] | None = None) -> dict[str, str]:
    """The picture's text, by keyword: tEXt, zTXt and iTXt alike, or only
    the keywords `wanted` takes (the rest are never inflated). A chunk that
    can't be read is passed over; the first of a keyword wins. PngTextError
    when what is read comes to more than MAX_TEXT."""
    found: dict[str, str] = {}
    total = 0
    for kind, body, _start, _stop in _chunks(data):
        if kind not in (b"tEXt", b"zTXt", b"iTXt") or b"\0" not in body:
            continue
        raw_keyword, rest = body.split(b"\0", 1)
        keyword = raw_keyword.decode("latin-1")
        if keyword in found or (wanted is not None and not wanted(keyword)):
            continue
        try:
            if kind == b"tEXt":
                text = rest.decode("latin-1")
            elif kind == b"zTXt":
                text = _inflated(rest[1:]).decode("latin-1")
            else:
                compressed, _method = rest[0], rest[1]
                _language, _translated, value = rest[2:].split(b"\0", 2)
                text = (_inflated(value) if compressed else value).decode("utf-8")
        except (ValueError, IndexError, zlib.error):
            continue
        total += len(text)
        if total > MAX_TEXT:
            raise PngTextError(
                f"its text comes to more than {MAX_TEXT // (1024 * 1024)} MB, far past any card's"
            )
        found[keyword] = text
    return found


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")


def with_text(data: bytes, texts: dict[str, str]) -> bytes:
    """The picture with `texts` as tEXt chunks just before its end, in place
    of any text chunks already under those keywords. The text must be
    Latin-1 (a card's is base64)."""
    keywords = {keyword.encode("latin-1") for keyword in texts}
    out = bytearray(PNG_SIGNATURE)
    for kind, body, start, stop in _chunks(data):
        if kind in (b"tEXt", b"zTXt", b"iTXt") and body.split(b"\0", 1)[0] in keywords:
            continue
        if kind == b"IEND":
            for keyword, text in texts.items():
                out += _chunk(b"tEXt", keyword.encode("latin-1") + b"\0" + text.encode("latin-1"))
        out += data[start:stop]
    return bytes(out)
