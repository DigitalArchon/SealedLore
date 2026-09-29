"""Removing everything a picture file carries besides the picture.

Every picture that comes in (added from disk, drawn by an image model, in an
imported scenario or backup) and every one that goes out (to an image model,
in a scenario file) passes through `strip_metadata`. EXIF goes (GPS, camera
and its serial number, dates, and the thumbnail, which can show a photo as it
was before it was cropped), and so do XMP, IPTC, comments, C2PA manifests and
anything appended after the image (a phone's motion-photo video). The pixels,
the colour profile and the orientation stay: orientation as a minimal EXIF
block of one tag.

Nothing is re-encoded. The file's own blocks are copied or left out, so the
picture is bit for bit the same and the quality is too. What is kept is an
allowlist per format; anything else goes. A file that isn't a JPEG, PNG or
WebP, or whose structure can't be followed to the end of its image, raises
`MetadataError`: nobody can say what it carries, so it is never sent.
"""

from __future__ import annotations

import zlib

JPEG_SIGNATURE = b"\xff\xd8"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_EXIF_PREFIX = b"Exif\x00\x00"
_ORIENTATION_TAG = 0x0112
_TIFF_SHORT = 3


class MetadataError(ValueError):
    """A picture whose hidden data can't be removed, said in plain words."""


def picture_kind(data: bytes) -> str | None:
    """The file's extension by its content: ".jpg", ".png", ".webp", or None."""
    if data.startswith(JPEG_SIGNATURE):
        return ".jpg"
    if data.startswith(PNG_SIGNATURE):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def strip_metadata(data: bytes) -> bytes:
    """The same picture with nothing but its pixels, colour profile and
    orientation. Stripping a stripped file changes nothing."""
    kind = picture_kind(data)
    if kind == ".jpg":
        return _strip_jpeg(data)
    if kind == ".png":
        return _strip_png(data)
    if kind == ".webp":
        return _strip_webp(data)
    raise MetadataError("it isn't a JPEG, PNG or WebP picture")


def stripped_or_same(data: bytes) -> bytes:
    """Stripped when it can be, else as it was: for files coming in, which
    are kept either way. Anything that can't be stripped is refused on its
    way out. A video is stripped too (`storage.video_meta`)."""
    from sealedlore.storage.video_meta import is_mp4, strip_video_metadata

    try:
        return strip_video_metadata(data) if is_mp4(data) else strip_metadata(data)
    except MetadataError:
        return data


# --- orientation -----------------------------------------------------------------


def _orientation(tiff: bytes) -> int | None:
    """The Orientation tag of an EXIF block's first image, if it has one
    other than "as stored". A block that can't be read has none."""
    if len(tiff) < 8:
        return None
    order = {b"II": "little", b"MM": "big"}.get(tiff[:2])
    if order is None or int.from_bytes(tiff[2:4], order) != 42:
        return None
    ifd = int.from_bytes(tiff[4:8], order)
    if ifd + 2 > len(tiff):
        return None
    for index in range(int.from_bytes(tiff[ifd : ifd + 2], order)):
        entry = ifd + 2 + 12 * index
        if entry + 12 > len(tiff):
            return None
        if int.from_bytes(tiff[entry : entry + 2], order) != _ORIENTATION_TAG:
            continue
        kind = int.from_bytes(tiff[entry + 2 : entry + 4], order)
        count = int.from_bytes(tiff[entry + 4 : entry + 8], order)
        value = int.from_bytes(tiff[entry + 8 : entry + 10], order)
        return value if kind == _TIFF_SHORT and count == 1 and 2 <= value <= 8 else None
    return None


def _orientation_tiff(orientation: int) -> bytes:
    """A TIFF block holding the Orientation tag and nothing else."""
    return (
        b"MM\x00\x2a\x00\x00\x00\x08"  # big-endian, first directory at 8
        + b"\x00\x01"  # one entry
        + _ORIENTATION_TAG.to_bytes(2, "big")
        + _TIFF_SHORT.to_bytes(2, "big")
        + (1).to_bytes(4, "big")
        + orientation.to_bytes(2, "big")
        + b"\x00\x00"
        + b"\x00\x00\x00\x00"  # no next directory: no thumbnail
    )


# --- JPEG ------------------------------------------------------------------------

# Frames, Huffman and arithmetic tables, quantisation, restart interval,
# scans, line count, hierarchical: the image itself.
_JPEG_STRUCTURE = {*range(0xC0, 0xD0), 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF}
_JPEG_EOI = 0xD9


def _jpeg_segment(code: int, body: bytes) -> bytes:
    return bytes((0xFF, code)) + (len(body) + 2).to_bytes(2, "big") + body


def _kept_jpeg_segment(code: int, body: bytes) -> bytes | None:
    if code in _JPEG_STRUCTURE:
        return _jpeg_segment(code, body)
    if code == 0xE0 and body.startswith(b"JFIF\x00") and len(body) >= 12:
        # Version, units and density; the thumbnail it may carry goes.
        return _jpeg_segment(code, body[:12] + b"\x00\x00")
    if code == 0xE1 and body.startswith(_EXIF_PREFIX):
        orientation = _orientation(body[len(_EXIF_PREFIX) :])
        if orientation is None:
            return None
        return _jpeg_segment(code, _EXIF_PREFIX + _orientation_tiff(orientation))
    if code == 0xE2 and body.startswith(b"ICC_PROFILE\x00"):
        return _jpeg_segment(code, body)
    if code == 0xEE and body.startswith(b"Adobe") and len(body) >= 12:
        # The colour transform, without which a CMYK JPEG decodes wrongly.
        return _jpeg_segment(code, body[:12])
    if 0xE0 <= code <= 0xEF or code == 0xFE:
        return None  # every other application block, and comments
    raise MetadataError(f"it has a JPEG block this app doesn't know (0xFF{code:02X})")


def _scan_end(data: bytes, pos: int) -> int:
    """Where a scan's coded data ends: at the next marker that isn't a
    stuffed zero or a restart. The file's end if it stops mid-scan."""
    while True:
        at = data.find(b"\xff", pos)
        if at < 0 or at + 1 >= len(data):
            return len(data)
        following = data[at + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7 or following == 0xFF:
            pos = at + 1
            continue
        return at


def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(JPEG_SIGNATURE)
    pos, end, scanned = 2, len(data), False
    while pos < end:
        if data[pos] != 0xFF:
            raise MetadataError(f"its JPEG structure breaks off at byte {pos}")
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        code = data[pos]
        pos += 1
        if code == _JPEG_EOI:
            if not scanned:
                break
            # Anything after the end of the image (a motion photo's video,
            # a second picture) goes with it.
            return bytes(out + bytes((0xFF, _JPEG_EOI)))
        if 0xD0 <= code <= 0xD7 or code == 0x01:
            out += bytes((0xFF, code))
            continue
        if pos + 2 > end:
            raise MetadataError("its JPEG structure is cut off")
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > end:
            raise MetadataError("a JPEG block runs past the end of the file")
        kept = _kept_jpeg_segment(code, data[pos + 2 : pos + length])
        pos += length
        if kept is not None:
            out += kept
        if code == 0xDA:
            scanned = True
            stop = _scan_end(data, pos)
            out += data[pos:stop]
            pos = stop
    if not scanned:
        raise MetadataError("it holds no JPEG image")
    # Cut off mid-scan: everything there was copied as image data.
    return bytes(out)


# --- PNG -------------------------------------------------------------------------

# The image, its transparency, animation and colour (the profile included),
# and its pixel aspect. Text (where XMP and generators' prompts go), times,
# C2PA and anything unknown go.
_PNG_KEPT = {
    b"IHDR",
    b"PLTE",
    b"IDAT",
    b"IEND",
    b"tRNS",
    b"gAMA",
    b"cHRM",
    b"sRGB",
    b"iCCP",
    b"sBIT",
    b"cICP",
    b"mDCV",
    b"mDCv",
    b"cLLI",
    b"cLLi",
    b"pHYs",
    b"acTL",
    b"fcTL",
    b"fdAT",
}


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")


def _strip_png(data: bytes) -> bytes:
    out = bytearray(PNG_SIGNATURE)
    pos, end = len(PNG_SIGNATURE), len(data)
    first = True
    while pos + 8 <= end:
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        stop = pos + 12 + length
        if stop > end:
            raise MetadataError("a PNG chunk runs past the end of the file")
        if first and kind != b"IHDR":
            raise MetadataError("its PNG header is missing")
        first = False
        if kind in _PNG_KEPT:
            out += data[pos:stop]
        elif kind == b"eXIf":
            orientation = _orientation(data[pos + 8 : pos + 8 + length])
            if orientation is not None:
                out += _png_chunk(b"eXIf", _orientation_tiff(orientation))
        pos = stop
        if kind == b"IEND":
            # Anything after the end of the image goes with it.
            return bytes(out)
    raise MetadataError("the PNG has no end: it may be cut off")


# --- WebP ------------------------------------------------------------------------

_WEBP_KEPT = {b"VP8X", b"ICCP", b"ANIM", b"ALPH", b"VP8 ", b"VP8L"}
_WEBP_FRAME_KEPT = {b"ALPH", b"VP8 ", b"VP8L"}
_WEBP_IMAGE = {b"VP8 ", b"VP8L", b"ANMF"}
_VP8X_EXIF = 0x08
_VP8X_XMP = 0x04


def _riff_chunks(data: bytes, pos: int, end: int) -> list[tuple[bytes, bytes]]:
    chunks: list[tuple[bytes, bytes]] = []
    while pos < end:
        if pos + 8 > end:
            raise MetadataError("a WebP chunk is cut off")
        kind = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if pos + 8 + size > end:
            raise MetadataError("a WebP chunk runs past the end of the file")
        chunks.append((kind, data[pos + 8 : pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return chunks


def _riff_chunk(kind: bytes, body: bytes) -> bytes:
    return kind + len(body).to_bytes(4, "little") + body + (b"\x00" if len(body) & 1 else b"")


def _strip_webp(data: bytes) -> bytes:
    end = 8 + int.from_bytes(data[4:8], "little")
    if end > len(data):
        raise MetadataError("the WebP file is cut off")
    chunks = _riff_chunks(data, 12, end)
    if not any(kind in _WEBP_IMAGE for kind, _ in chunks):
        raise MetadataError("it holds no WebP image")
    extended = bool(chunks) and chunks[0][0] == b"VP8X"
    kept: list[tuple[bytes, bytes]] = []
    exif = False
    for kind, body in chunks:
        if kind in _WEBP_KEPT:
            kept.append((kind, body))
        elif kind == b"ANMF":
            if len(body) < 16:
                raise MetadataError("a WebP frame is cut off")
            frame = b"".join(
                _riff_chunk(sub, sub_body)
                for sub, sub_body in _riff_chunks(body, 16, len(body))
                if sub in _WEBP_FRAME_KEPT
            )
            kept.append((kind, body[:16] + frame))
        elif kind == b"EXIF" and extended:
            tiff = body[len(_EXIF_PREFIX) :] if body.startswith(_EXIF_PREFIX) else body
            orientation = _orientation(tiff)
            if orientation is not None:
                kept.append((kind, _orientation_tiff(orientation)))
                exif = True
    if extended:
        header = kept[0][1]
        if len(header) < 1:
            raise MetadataError("its WebP header is cut off")
        flags = header[0] & ~(_VP8X_EXIF | _VP8X_XMP) | (_VP8X_EXIF if exif else 0)
        kept[0] = (b"VP8X", bytes((flags,)) + header[1:])
    body = b"WEBP" + b"".join(_riff_chunk(kind, chunk) for kind, chunk in kept)
    return b"RIFF" + len(body).to_bytes(4, "little") + body
