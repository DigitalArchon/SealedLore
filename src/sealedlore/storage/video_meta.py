"""Hidden data out of MP4 videos, without re-encoding or moving a byte.

What a video can carry besides its pictures and sound: metadata boxes (the
encoder's name in `udta/meta/ilst`, XMP and C2PA in `uuid` boxes, a
top-level `meta`), creation and modification times in the movie, track and
media headers, and the encoder's settings as text inside the video stream
(x264 writes them into an SEI message of the first frame). Seedance's
videos from NanoGPT carried the first and the last (Sept 2026).

Nothing is removed from the file, only blanked where it stands: a metadata
box becomes a `free` box of the same size with its contents zeroed, the
times become zero, and the encoder's text becomes spaces. So every offset
in the file (the sample tables point into `mdat`) stays right, and a player
reads it as before. Blanking a blanked file changes nothing.

A file that isn't an MP4 this can walk is `MetadataError`.
"""

from __future__ import annotations

import struct

from sealedlore.storage.image_meta import MetadataError

# Boxes whose children are boxes, walked into.
_CONTAINERS = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"edts",
    b"dinf",
    b"mvex",
    b"moof",
    b"traf",
}
# Boxes that are metadata and nothing else: blanked wherever they are.
_METADATA = {b"udta", b"meta", b"uuid", b"XMP_", b"ID32"}
# Headers with a creation and a modification time.
_TIMED = {b"mvhd", b"tkhd", b"mdhd"}
# Encoder settings written as text into the video stream. Long markers only:
# a short one could turn up by chance in compressed data.
_ENCODER_TEXT = (b"x264 - core ", b"x265 (build ")


def is_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _boxes(data: bytes | bytearray, start: int, end: int):
    """(type, box start, payload start, box end) for each box in [start, end)."""
    offset = start
    while offset < end:
        if end - offset < 8:
            raise MetadataError("a box runs past the end of its parent")
        size, kind = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1:
            if end - offset < 16:
                raise MetadataError("a large box runs past the end of its parent")
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise MetadataError("a box's size doesn't fit the file")
        yield kind, offset, offset + header, offset + size
        offset += size


def _blank_box(out: bytearray, start: int, payload: int, stop: int) -> None:
    out[start + 4 : start + 8] = b"free"
    out[payload:stop] = bytes(stop - payload)


def _zero_times(out: bytearray, payload: int, stop: int) -> None:
    if stop - payload < 12:
        return
    version = out[payload]
    width = 8 if version == 1 else 4
    first = payload + 4
    end = first + 2 * width
    if end <= stop:
        out[first:end] = bytes(2 * width)


def _walk(out: bytearray, start: int, end: int) -> None:
    for kind, box, payload, stop in _boxes(out, start, end):
        if kind in _METADATA:
            _blank_box(out, box, payload, stop)
        elif kind in _TIMED:
            _zero_times(out, payload, stop)
        elif kind in _CONTAINERS:
            _walk(out, payload, stop)


def _blank_encoder_text(out: bytearray, payload: int, stop: int) -> None:
    """The encoder's settings, where it wrote them into the stream as text:
    each run from a known marker to its terminating NUL becomes spaces
    (never zeros, which could read as a start code). A run that isn't all
    printable text is left alone: it isn't the encoder's."""
    for marker in _ENCODER_TEXT:
        at = out.find(marker, payload, stop)
        while at != -1:
            nul = out.find(b"\x00", at, min(stop, at + 4096))
            if nul == -1:
                break
            if all(32 <= byte < 127 for byte in out[at:nul]):
                out[at:nul] = b" " * (nul - at)
            at = out.find(marker, nul, stop)


def strip_video_metadata(data: bytes) -> bytes:
    """The same MP4 with its metadata blanked (see the module docstring)."""
    if not is_mp4(data):
        raise MetadataError("it isn't an MP4 video")
    out = bytearray(data)
    top = list(_boxes(out, 0, len(out)))
    if not any(kind == b"moov" for kind, *_ in top):
        raise MetadataError("the video has no movie header")
    for kind, box, payload, stop in top:
        if kind in _METADATA:
            _blank_box(out, box, payload, stop)
        elif kind in _CONTAINERS:
            _walk(out, payload, stop)
        elif kind == b"mdat":
            _blank_encoder_text(out, payload, stop)
    return bytes(out)
