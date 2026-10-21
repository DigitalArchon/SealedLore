"""The two plot guides beside the samples, opened in the desktop's own viewer.

`PLOT_FORMAT.md` says what a plot file may hold; `WRITING_PLOTS.md` says how
to word one so it plays as its author meant. Both ship in the samples folder
(the AppImage copies every `.md` there), so they are found wherever the
samples are: Help in the main window and in the plot file editor.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from sealedlore.storage.paths import PLOT_FORMAT_GUIDE, PLOT_WRITING_GUIDE, samples_dir

# Menu labels, used by both windows (the manual names the main window's).
FORMAT_LABEL = "Plot &format guide"
WRITING_LABEL = "&Writing a plot that plays well"
WRITING_TIP = "How to word a plot's triggers, facts and events so the story plays as you meant"


def open_guide(parent: QWidget, name: str = PLOT_WRITING_GUIDE) -> bool:
    """Open one of the guides; say so, and return False, when this copy has none."""
    folder = samples_dir()
    path = folder / name if folder is not None else None
    if path is None or not path.is_file():
        box = QMessageBox(
            QMessageBox.Warning,
            "Guide not found",
            f"{name} isn't installed with this copy of SealedLore "
            "(it is in SampleStories/ in the source).",
            QMessageBox.Ok,
            parent,
        )
        box.setTextFormat(Qt.PlainText)
        box.exec()
        return False
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    return True


__all__ = [
    "FORMAT_LABEL",
    "PLOT_FORMAT_GUIDE",
    "PLOT_WRITING_GUIDE",
    "WRITING_LABEL",
    "WRITING_TIP",
    "open_guide",
]
