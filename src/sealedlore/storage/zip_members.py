"""Reading a member of a zip someone else made, within a limit.

`ZipFile.read` inflates a member whole and only then cuts it to the size the
archive claims for it: a member that says it is small and inflates to
gigabytes is held whole in memory first (a 199 KB card file took 428 MB).
Read a piece at a time, a member never holds more than it claims, and one
that inflates past it fails its checksum and reads as damaged.

Only stored and deflated members are read. zipfile inflates a bzip2 or LZMA
member a whole compressed piece at a time with no limit on what comes out
(bzip2 holds 200 MB of one byte in 178 bytes), and no card, expression pack
or backup made by the usual tools uses either.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator

PIECE = 64 * 1024


class MemberTooLarge(ValueError):
    """A member that says it is larger than the reader takes."""


def _openable(info: zipfile.ZipInfo) -> None:
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        # As zipfile says of a way of compressing it can't read: damaged, to
        # every caller.
        raise NotImplementedError(f"{info.filename} is compressed in a way SealedLore doesn't read")


def read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """`info`'s bytes, never more than `limit` (MemberTooLarge when it says it
    is larger); what zipfile raises for a damaged member otherwise, a member
    that inflates past the size it claims among them (BadZipFile)."""
    if info.file_size > limit:
        raise MemberTooLarge(f"{info.filename} is {info.file_size} bytes, past {limit}")
    _openable(info)
    pieces: list[bytes] = []
    with archive.open(info) as stream:
        # zipfile stops at the size the archive claims and checks the
        # checksum there, inflating no more than a piece at a time.
        while piece := stream.read(PIECE):
            pieces.append(piece)
    return b"".join(pieces)


def member_lines(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, line_limit: int
) -> Iterator[bytes]:
    """`info`'s lines that hold anything, without their newlines, read a
    piece at a time: a line longer than `line_limit` (at least a piece) is
    passed over, never held whole, and empty ones cost next to nothing (a
    log of nothing but newlines took ten times its size in memory, a list
    of every line). MemberTooLarge when it says it is larger than `limit`; a
    damaged member raises what zipfile raises, at the point it is found."""
    if info.file_size > limit:
        raise MemberTooLarge(f"{info.filename} is {info.file_size} bytes, past {limit}")
    _openable(info)
    line_limit = max(line_limit, PIECE)
    with archive.open(info) as stream:
        head = bytearray()  # a line the pieces so far haven't ended
        dropping = False  # a line too long to keep, until it ends
        while piece := stream.read(PIECE):
            *ended, rest = piece.split(b"\n")
            if ended:
                # The first ends the line carried over; the others lie
                # wholly in this piece, so within the limit.
                head += ended[0]
                ended[0] = b"" if dropping or len(head) > line_limit else bytes(head)
                head.clear()
                dropping = False
                yield from filter(None, ended)
            if not dropping:
                head += rest
                if len(head) > line_limit:
                    head.clear()
                    dropping = True
        if head and not dropping:
            yield bytes(head)
