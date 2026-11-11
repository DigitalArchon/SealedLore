"""Hidden data out of MP4 videos, without re-encoding or moving a byte.

What a video can carry besides its pictures and sound: metadata boxes (the
encoder's name in `udta/meta/ilst`, XMP and C2PA in `uuid` boxes, a
top-level `meta`), creation and modification times in the movie, track and
media headers, and the encoder's settings as text inside the video stream
(x264 writes them into an SEI message of the first frame). Seedance's
videos from NanoGPT carried the first and the last (Sept 2026). And
whatever a writer leaves in padding (`free`, `skip`), a handler's name, a
video track's compressor name (ffmpeg's "Lavc60.31.102 libx264") and any
top-level box a player has no use for.

Nothing is removed from the file, only blanked where it stands: a metadata
box, padding, or a top-level box that isn't one a player needs becomes a
`free` box of the same size with its contents zeroed, the times, names and
vendor become zero, and the encoder's text becomes spaces. So every offset
in the file (the sample tables point into `mdat`) stays right, and a player
reads it as before. Blanking a blanked file changes nothing.

A file that isn't an MP4 this can walk is `MetadataError`.
"""

from __future__ import annotations

import struct

from sealedlore.storage.image_meta import PICTURE_BRANDS, MetadataError, bmff_brands

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
# Room left over, holding whatever its writer left in it (ffmpeg's encoder
# settings, a marker): blanked wherever it is.
_PADDING = {b"free", b"skip", b"wide"}
# The top-level boxes a player needs, a fragmented MP4's among them. Any
# other at the top is blanked as metadata is: listing only what to blank
# let a marker through in every box nobody had listed.
_STRUCTURAL = {b"ftyp", b"moov", b"mdat", b"moof", b"mfra", b"sidx", b"ssix", b"styp"}
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


def is_mp4(data: bytes) -> bool:
    """An MP4 (or another ISO BMFF video): an `ftyp` box first, and no brand
    in it that makes it a picture. An iPhone's HEIC and an AVIF begin the
    same way; taken for videos, they kept their EXIF (GPS and all)."""
    brands = bmff_brands(data)
    return bool(brands) and not PICTURE_BRANDS.intersection(brands)


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


def _blank_names(out: bytearray, payload: int, stop: int, handler: bytes) -> None:
    """A video track's sample entries with their vendor and compressor's
    name blanked; an entry too short to have them, or entries that don't
    read as boxes, left as they are (as they were before these were
    blanked: never a reason to refuse the file)."""
    if handler not in (b"vide", b""):
        return
    try:
        entries = list(_boxes(out, payload + 8, stop))  # after version, flags and count
    except MetadataError:
        return
    for kind, _box, entry, entry_stop in entries:
        video = handler == b"vide" or kind in _VIDEO_ENTRIES
        if video and entry_stop - entry >= _COMPRESSOR[1]:
            for first, last in (_VENDOR, _COMPRESSOR):
                out[entry + first : entry + last] = bytes(last - first)


def _walk(out: bytearray, start: int, end: int, parent: bytes = b"") -> None:
    """Every box inside [start, end) (a `parent` box's), containers walked
    into. With a list of those still to walk, never by recursing: a 24 KB
    file of 3,000 nested `trak` boxes recursed too far (RecursionError,
    which no reader of a backup or a scenario expects). Each carries its
    track's handler type, read from `mdia`'s `hdlr`, for its sample
    entries: a video's are laid out as a picture's, a sound's otherwise."""
    pending = [(start, end, parent, b"")]
    while pending:
        start, end, parent, handler = pending.pop()
        children = list(_boxes(out, start, end))
        if parent == b"mdia":
            for kind, _box, payload, stop in children:
                if kind == b"hdlr" and stop - payload >= 12:
                    handler = bytes(out[payload + 8 : payload + 12])
        for kind, box, payload, stop in children:
            if kind in _METADATA or kind in _PADDING:
                _blank_box(out, box, payload, stop)
            elif kind in _TIMED:
                _zero_times(out, payload, stop)
            elif kind == b"hdlr":
                name = min(payload + _HANDLER_NAME, stop)
                out[name:stop] = bytes(stop - name)
            elif kind == b"stsd":
                _blank_names(out, payload, stop, handler)
            elif kind in _CONTAINERS:
                pending.append((payload, stop, kind, handler))


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
        if kind == b"mdat":
            _blank_encoder_text(out, payload, stop)
        elif kind in _CONTAINERS:
            _walk(out, payload, stop, kind)
        elif kind not in _STRUCTURAL:
            # Metadata, padding, and anything else no player needs.
            _blank_box(out, box, payload, stop)
    return bytes(out)
