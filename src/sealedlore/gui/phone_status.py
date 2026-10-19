"""The status bar's sign that phone access is on (gui/window_phone.py).

Shown only while it is on, among the permanent widgets so a notice never
hides it: an onion-like mark drawn in the theme's colours and a few words
("Phone: connecting 45%", "Phone: ready", "Phone: 1 connected", "Phone: tor
stopped"). A click opens File → Phone devices….
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QToolButton, QWidget

from sealedlore.gui import theme

ICON_SIZE = 14


def phone_icon(colour_name: str) -> QIcon:
    """Three rings, the outer two open at the top: an onion's layers."""
    side = ICON_SIZE * 2  # drawn at twice the size, for high-DPI screens
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    colour = theme.colour(colour_name)
    for inset, width in ((2, 3), (7, 3)):
        painter.setPen(QPen(colour, width))
        rect = QRectF(inset, inset, side - 2 * inset, side - 2 * inset)
        painter.drawArc(rect, 110 * 16, 320 * 16)
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    painter.drawEllipse(QRectF(side / 2 - 4, side / 2 - 4, 8, 8))
    painter.end()
    return QIcon(pixmap)


def short_words(step: str, words: str, progress: int, connected: int) -> str:
    if step == "ready":
        if connected:
            return f"Phone: {connected} connected"
        return "Phone: ready"
    if step == "bootstrapping":
        return f"Phone: connecting {progress}%"
    if step in ("starting", "connecting"):
        return "Phone: starting"
    if step == "publishing":
        return "Phone: publishing"
    if step == "restarting":
        return "Phone: tor restarting"
    if step == "failed":
        return "Phone: tor stopped"
    return "Phone"


class PhoneStatusButton(QToolButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("phoneStatus")
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.hide()

    def show_state(
        self, step: str, words: str, *, progress: int = 0, connected: int = 0, shared: int = 0
    ) -> None:
        """`step` is tor's ("off" hides the button)."""
        if step == "off":
            self.hide()
            return
        colour = (
            "warning"
            if step in ("failed", "restarting")
            else ("accent" if step == "ready" else "text_dim")
        )
        self.setIcon(phone_icon(colour))
        self.setText(short_words(step, words, progress, connected))
        stories = f"{shared} stor{'y' if shared == 1 else 'ies'} shared"
        self.setToolTip(f"Phone access is on. {words} {stories}. Click for the phones and stories.")
        self.show()
