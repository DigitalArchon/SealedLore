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
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader

# One lock for every decode and encode in the process (re-entrant: a decode
# inside a cut-out inside an encode is fine on one thread).
CODEC_LOCK = threading.RLock()


def image_from_data(data: bytes | None) -> QImage:
    """`QImage.fromData` under the lock; a null image for nothing."""
    if not data:
        return QImage()
    with CODEC_LOCK:
        return QImage.fromData(data)


def read_image(make_reader: Callable[[], QImageReader]) -> QImage:
    """`reader.read()` under the lock, with EXIF orientation applied, for a
    reader the caller makes (`QImageReader(path)` or one over a buffer)."""
    with CODEC_LOCK:
        reader = make_reader()
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
