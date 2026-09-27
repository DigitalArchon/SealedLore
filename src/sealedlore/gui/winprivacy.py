"""What Windows records of the screen and the clipboard, kept from the private
parts of the app. Every function here does nothing off Windows.

- **Screen capture.** Recall (Copilot+ PCs) snapshots the screen every few
  seconds and indexes the text in it; screenshots, screen recorders and
  remote viewers read the same pixels. `SetWindowDisplayAffinity` with
  `WDA_EXCLUDEFROMCAPTURE` leaves a window out of all of them: they show
  what is behind it, as if it weren't there (checked on Server 2022; black
  is the older `WDA_MONITOR`). Set while the private look is on (a private
  scene, a TEE or end-to-end encrypted chat), cleared when it ends.
- **The clipboard.** Win+V keeps what was copied, and "Sync across devices"
  uploads it to Microsoft. Text the app copies (a prompt, a review) carries
  the formats Windows reads to leave it out of both, and out of clipboard
  monitors.

Text the author selects and copies with Ctrl+C goes through Qt's own path
and is not covered; the manual says so.
"""

from __future__ import annotations

import struct
import sys

from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication, QWidget

WDA_NONE = 0x00
WDA_MONITOR = 0x01  # older: black in captures (the only choice before Windows 10 2004); unused
WDA_EXCLUDEFROMCAPTURE = 0x11

# Qt puts a mime type of this shape on the Windows clipboard under the format
# name inside the quotes.
_NATIVE = 'application/x-qt-windows-mime;value="{}"'
# Present at all: clipboard history, cloud clipboard and monitors skip it.
EXCLUDE_FROM_MONITORS = "ExcludeClipboardContentFromMonitorProcessing"
# A DWORD 0: not kept in history, not uploaded.
NOT_IN_HISTORY = "CanIncludeInClipboardHistory"
NOT_TO_CLOUD = "CanUploadToCloudClipboard"


def exclude_from_capture(widget: QWidget, exclude: bool) -> bool:
    """Leave `widget`'s window out of screen capture (Recall, screenshots),
    or put it back. True if Windows took it; always False elsewhere."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        hwnd = int(widget.window().winId())
        affinity = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
        return bool(user32.SetWindowDisplayAffinity(hwnd, affinity))
    except (OSError, AttributeError, ValueError):
        return False


def private_mime(text: str) -> QMimeData:
    """`text` for the clipboard; on Windows, marked to stay out of clipboard
    history, the cloud clipboard and clipboard monitors."""
    data = QMimeData()
    data.setText(text)
    if sys.platform == "win32":
        zero = struct.pack("<I", 0)
        data.setData(_NATIVE.format(EXCLUDE_FROM_MONITORS), zero)
        data.setData(_NATIVE.format(NOT_IN_HISTORY), zero)
        data.setData(_NATIVE.format(NOT_TO_CLOUD), zero)
    return data


def copy_text(text: str) -> None:
    """Put `text` on the clipboard, kept out of Windows' history and cloud."""
    QApplication.clipboard().setMimeData(private_mime(text))
