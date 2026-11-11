"""Qt's image decoding and encoding, one at a time for the whole process.

Decoding a picture on two Python threads at once hangs or kills the app
(Oct 17 2026, found when the test suite stalled for good): with PySide6
6.10 offscreen, one thread in `QImage.fromData` beside the GUI thread in
`QImageReader.read` deadlocked every time within seconds, and two worker
threads decoding together aborted the process; under one lock, thousands of
decodes a second and no stall. The play view paints an area on a thread
(sprites and textures decoded there) while the GUI thread loads thumbnails,
portraits and the background, so the app itself could freeze. Encoding
(`QImage.save`) goes through the same plugins, so it takes the lock too.

Every decode and encode in the GUI goes through here: `image_from_data`,
`read_image` (a reader made by the caller: a file or a buffer, auto-transform
set), and `encoded`. Never call `QImage.fromData`, `QImageReader.read` or
`QImage.save` elsewhere (`tests/test_image_codec.py` looks).

A picture is decoded only as the kind of picture its own first bytes say it
is (`raster_format`), named to Qt, which then never guesses. Left to guess,
Qt drew an SVG saved as "jane.png" (beside a plot file, as a card's icon, in
a scenario) as a drawing, and its drawing pulled in any picture file on this
computer it named by path: the story then kept that file's pixels. The app's
own icons are drawn by the stylesheet, not here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader

from sealedlore.storage.image_meta import raster_kind

# One lock for every decode and encode in the process (re-entrant: a decode
# inside a cut-out inside an encode is fine on one thread).
CODEC_LOCK = threading.RLock()

# The pictures made of pixels the app takes, by their own bytes, as Qt names
# their readers. Nothing else (a drawing, a document) is ever decoded.
QT_FORMATS = {
    ".jpg": b"jpeg",
    ".png": b"png",
    ".webp": b"webp",
    ".gif": b"gif",
    ".bmp": b"bmp",
    ".tiff": b"tiff",
}
# Enough of a file's start to tell its kind.
HEAD_BYTES = 64


def raster_format(head: bytes | None) -> bytes | None:
    """Qt's name for the picture `head` (a file's first bytes, or all of
    them) begins, or None for anything that isn't one the app takes."""
    kind = raster_kind(head or b"")
    return QT_FORMATS.get(kind) if kind is not None else None


def image_from_data(data: bytes | None) -> QImage:
    """`QImage.fromData` under the lock, as the picture its bytes say it is;
    a null image for nothing, or for anything else."""
    fmt = raster_format(data)
    if not data or fmt is None:
        return QImage()
    with CODEC_LOCK:
        return QImage.fromData(data, fmt.decode("ascii"))


def read_image(make_reader: Callable[[], QImageReader], fmt: bytes | None) -> QImage:
    """`reader.read()` under the lock, with EXIF orientation applied, for a
    reader the caller makes (`QImageReader(path)` or one over a buffer), as
    the picture `fmt` (`raster_format`) names and nothing else: a null image
    for None."""
    if fmt is None:
        return QImage()
    with CODEC_LOCK:
        reader = make_reader()
        reader.setAutoDetectImageFormat(False)
        reader.setFormat(fmt)
        reader.setAutoTransform(True)
        return reader.read()


def encoded(image: QImage, fmt: str, quality: int = -1) -> bytes:
    """`image.save` to bytes in `fmt` ("PNG", "JPEG", "WEBP") under the lock."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    with CODEC_LOCK:
        image.save(buffer, fmt, quality)
    buffer.close()
    return bytes(data.data())
