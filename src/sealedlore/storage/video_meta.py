"""Hidden data out of MP4 videos, without re-encoding or moving a byte.

What a video can carry besides its pictures and sound: metadata boxes (the
encoder's name in `udta/meta/ilst`, XMP and C2PA in `uuid` boxes, a
top-level `meta`), creation and modification times in the movie, track and
media headers, and the encoder's settings as text inside the video stream
(x264 writes them into an SEI message of the first frame). Seedance's
videos from NanoGPT carried the first and the last (Sept 2026). And
whatever a writer leaves in padding (`free`, `skip`), a handler's name, a
video track's compressor name (ffmpeg's "Lavc60.31.102 libx264"), the name
ffmpeg's own AAC encoder writes into the first frame of sound (the same
"Lavc60.31.102", as fill data a decoder skips) and any top-level box a
player has no use for.

Nothing is removed from the file, only blanked where it stands: a metadata
box, padding, or any box that isn't one a player needs where it stands (at
the top of the file, or inside the movie header, a track, its sample table,
a fragment) becomes a `free` box of the same size with its contents zeroed,
the times, names and vendor become zero, and the encoder's text becomes
spaces. So every offset
in the file (the sample tables point into `mdat`) stays right, and a player
reads it as before. Blanking a blanked file changes nothing.

A file that isn't an MP4 this can walk is `MetadataError`, and so is one
whose movie header is compressed (QuickTime's `cmov`).
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator

from sealedlore.storage.image_meta import PICTURE_BRANDS, MetadataError, bmff_brands

# The boxes whose children are boxes, walked into, and for each the children
# a player has a use for (ISO/IEC 14496-12's, and QuickTime's `gmhd`, `tapt`,
# `load` and `stps`). Any other child is blanked where it stands, as an
# unknown box at the top of the file is: listing only what to blank kept
# whatever a box nobody had listed held, wherever it sat below the top (a
# marker in the movie header, a track, a sample table). Metadata (`udta`,
# `meta`, `uuid`, XMP, ID3) and padding (`free`, `skip`, `wide`) are in no
# list, so they go with the rest.
_CHILDREN: dict[bytes, frozenset[bytes]] = {
    b"moov": frozenset({b"mvhd", b"trak", b"mvex", b"iods"}),
    b"trak": frozenset({b"tkhd", b"mdia", b"edts", b"tref", b"tapt", b"load"}),
    b"edts": frozenset({b"elst"}),
    b"mdia": frozenset({b"mdhd", b"hdlr", b"minf", b"elng"}),
    b"minf": frozenset(
        {b"vmhd", b"smhd", b"hmhd", b"nmhd", b"sthd", b"gmhd", b"dinf", b"stbl", b"hdlr"}
    ),
    b"dinf": frozenset({b"dref"}),
    b"stbl": frozenset(
        {
            b"stsd",
            b"stts",
            b"ctts",
            b"cslg",
            b"stsc",
            b"stsz",
            b"stz2",
            b"stco",
            b"co64",
            b"stss",
            b"stsh",
            b"stps",
            b"padb",
            b"stdp",
            b"sdtp",
            b"sbgp",
            b"sgpd",
            b"subs",
            b"saiz",
            b"saio",
        }  # fmt: skip
    ),
    b"mvex": frozenset({b"mehd", b"trex", b"leva", b"trep"}),
    b"moof": frozenset({b"mfhd", b"traf"}),
    b"traf": frozenset(
        {
            b"tfhd",
            b"tfdt",
            b"trun",
            b"sbgp",
            b"sgpd",
            b"subs",
            b"saiz",
            b"saio",
            b"senc",
        }  # fmt: skip
    ),
    b"mfra": frozenset({b"tfra", b"mfro"}),
}
# The top-level boxes a player needs, a fragmented MP4's among them. Any
# other at the top is blanked as metadata is: listing only what to blank
# let a marker through in every box nobody had listed.
_STRUCTURAL = {b"ftyp", b"moov", b"mdat", b"moof", b"mfra", b"sidx", b"ssix", b"styp"}
# QuickTime's compressed movie header: the header itself, zlib-compressed,
# in place of its boxes. Nothing in it can be blanked where it stands.
_COMPRESSED = b"cmov"
# Headers with a creation and a modification time.
_TIMED = {b"mvhd", b"tkhd", b"mdhd"}
# A handler's (`hdlr`) name begins after its type and reserved fields;
# its type, which a player reads, stays.
_HANDLER_NAME = 24
# In a video track's sample entry (`stsd`): the vendor a QuickTime one
# names (zero in an MP4's), and the compressor's name, where ffmpeg writes
# its encoder's ("Lavc60.31.102 libx264").
_VENDOR = (12, 16)
_COMPRESSOR = (42, 74)
# Video sample entries by their own type, for a track that names no
# handler (a player then can't tell either; a sound's entry, laid out
# otherwise, is never touched).
_VIDEO_ENTRIES = {
    b"avc1", b"avc2", b"avc3", b"avc4", b"hvc1", b"hev1", b"vvc1", b"vvi1", b"mp4v", b"vp08",
    b"vp09", b"av01", b"encv", b"dvh1", b"dvhe", b"dvav", b"dva1", b"dav1", b"s263", b"mjp2",
}  # fmt: skip
# Encoder settings written as text into the video stream. Long markers only:
# a short one could turn up by chance in compressed data.
_ENCODER_TEXT = (b"x264 - core ", b"x265 (build ")
# The name libavcodec writes into a stream it encodes: "Lavc" and the whole
# dotted version, all of it text and with no end mark (ffmpeg's AAC encoder
# puts it, byte-aligned, in a fill element of the first frame of sound).
# Four letters, then numbers with two dots between them: compressed data
# doesn't spell that by chance.
_LIBAVCODEC = re.compile(rb"Lavc\d+\.\d+\.\d+")


def is_mp4(data: bytes) -> bool:
    """An MP4 (or another ISO BMFF video): an `ftyp` box first, and no brand
    in it that makes it a picture. An iPhone's HEIC and an AVIF begin the
    same way; taken for videos, they kept their EXIF (GPS and all)."""
    brands = bmff_brands(data)
    return bool(brands) and not PICTURE_BRANDS.intersection(brands)


def _box_at(data: bytes | bytearray, offset: int, end: int) -> tuple[bytes, int, int]:
    """(type, payload start, box end) of the box at `offset`, in a parent
    that ends at `end`."""
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
    return kind, offset + header, offset + size


def _boxes(data: bytes | bytearray, start: int, end: int) -> Iterator[tuple[bytes, int, int, int]]:
    """(type, box start, payload start, box end) for each box in [start, end),
    one at a time. Never made a list of: a box can be eight bytes, and its
    place in a list thirty times that (a 144 KB card file of them took
    1.1 GB to strip)."""
    offset = start
    while offset < end:
        kind, payload, stop = _box_at(data, offset, end)
        yield kind, offset, payload, stop
        offset = stop


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


def _blank_names(out: bytearray, payload: int, stop: int, handler: bytes) -> None:
    """A video track's sample entries with their vendor and compressor's
    name blanked; an entry too short to have them, or entries that don't
    read as boxes, left as they are (as they were before these were
    blanked: never a reason to refuse the file)."""
    if handler not in (b"vide", b""):
        return
    try:
        for _entry in _boxes(out, payload + 8, stop):  # after version, flags and count
            pass
    except MetadataError:
        return
    for kind, _box, entry, entry_stop in _boxes(out, payload + 8, stop):
        video = handler == b"vide" or kind in _VIDEO_ENTRIES
        if video and entry_stop - entry >= _COMPRESSOR[1]:
            for first, last in (_VENDOR, _COMPRESSOR):
                out[entry + first : entry + last] = bytes(last - first)
        if video:
            # ffmpeg's MPEG-4 Part 2 encoder writes its name again into the
            # stream's setup (user data in the `esds` inside an `mp4v` entry).
            _blank_libavcodec(out, entry, entry_stop)


def _handler(out: bytearray, start: int, end: int, handler: bytes) -> bytes:
    """The handler type an `mdia` box in [start, end) names in its `hdlr`
    (its last, when it has several); `handler`, the one outside it, when it
    names none."""
    for kind, _box, payload, stop in _boxes(out, start, end):
        if kind == b"hdlr" and stop - payload >= 12:
            handler = bytes(out[payload + 8 : payload + 12])
    return handler


def _walk(out: bytearray, start: int, end: int, parent: bytes = b"") -> None:
    """Every box inside [start, end) (a `parent` box's), containers walked
    into, in the order they stand in the file. Neither by recursing (a 24 KB
    file of 3,000 nested `trak` boxes recursed too far: RecursionError,
    which no reader of a backup or a scenario expects) nor by listing a
    box's children first (see `_boxes`): only the boxes this one is inside
    are kept, each with where it ends. Each box has its track's handler
    type, read from `mdia`'s `hdlr`, for its sample entries: a video's are
    laid out as a picture's, a sound's otherwise. A box its parent has no
    use for (`_CHILDREN`) is blanked whole, whatever is inside it."""
    handler = _handler(out, start, end, b"") if parent == b"mdia" else b""
    known = _CHILDREN[parent]
    # The boxes this one is inside, innermost last: where each ends, and the
    # handler and the children known outside it.
    inside: list[tuple[int, bytes, frozenset[bytes]]] = []
    offset = start
    while True:
        while offset == end:
            if not inside:
                return
            end, handler, known = inside.pop()
        kind, payload, stop = _box_at(out, offset, end)
        if kind == _COMPRESSED:
            raise MetadataError("its movie header is compressed, so nothing in it can be blanked")
        if kind not in known:
            _blank_box(out, offset, payload, stop)
        elif kind in _TIMED:
            _zero_times(out, payload, stop)
        elif kind == b"hdlr":
            name = min(payload + _HANDLER_NAME, stop)
            out[name:stop] = bytes(stop - name)
        elif kind == b"stsd":
            _blank_names(out, payload, stop, handler)
        elif kind in _CHILDREN:
            inside.append((end, handler, known))
            if kind == b"mdia":
                handler = _handler(out, payload, stop, handler)
            offset, end, known = payload, stop, _CHILDREN[kind]
            continue
        offset = stop


def _blank_encoder_text(out: bytearray, payload: int, stop: int) -> None:
    """The encoder's settings, where it wrote them into the stream as text:
    each run from a known marker to its terminating NUL becomes spaces
    (never zeros, which could read as a start code). A run that isn't all
    printable text is left alone: it isn't the encoder's. And libavcodec's
    name with its version, wherever it stands whole (a decoder skips the
    fill it is written in), as spaces too; the bytes around it untouched."""
    for marker in _ENCODER_TEXT:
        at = out.find(marker, payload, stop)
        while at != -1:
            nul = out.find(b"\x00", at, min(stop, at + 4096))
            if nul == -1:
                break
            if all(32 <= byte < 127 for byte in out[at:nul]):
                out[at:nul] = b" " * (nul - at)
            at = out.find(marker, nul, stop)
    _blank_libavcodec(out, payload, stop)


def _blank_libavcodec(out: bytearray, payload: int, stop: int) -> None:
    """libavcodec's name with its version, wherever it stands whole in
    [payload, stop), as spaces (never zeros: they could read as a start
    code); the bytes around it untouched."""
    at = out.find(b"Lavc", payload, stop)
    while at != -1:
        name = _LIBAVCODEC.match(out, at, stop)
        end = name.end() if name else at + 4
        if name:
            out[at:end] = b" " * (end - at)
        at = out.find(b"Lavc", end, stop)


def strip_video_metadata(data: bytes) -> bytes:
    """The same MP4 with its metadata blanked (see the module docstring)."""
    if not is_mp4(data):
        raise MetadataError("it isn't an MP4 video")
    out = bytearray(data)
    if not any(kind == b"moov" for kind, *_ in _boxes(out, 0, len(out))):
        raise MetadataError("the video has no movie header")
    for kind, box, payload, stop in _boxes(out, 0, len(out)):
        if kind == b"mdat":
            _blank_encoder_text(out, payload, stop)
        elif kind in _STRUCTURAL and kind in _CHILDREN:
            _walk(out, payload, stop, kind)
        elif kind not in _STRUCTURAL:
            # Metadata, padding, and anything else no player needs.
            _blank_box(out, box, payload, stop)
    return bytes(out)
