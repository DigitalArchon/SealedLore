"""File → Phone devices…: the phones, the stories they may see, and pairing.

Non-modal and one at a time: it follows phone access as it starts, and the
author can keep it open while a phone pairs. Everything it changes goes
through the window (gui/window_phone.py), which it holds weakly: a dialog
holding its window strongly is the kind of cycle that crashed PySide on
collection.
"""

from __future__ import annotations

import weakref
from datetime import date

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.sizing import size_to_screen
from sealedlore.gui.winprivacy import copy_text
from sealedlore.phone import qr
from sealedlore.phone.pairing import CODE_SECONDS
from sealedlore.storage.library import story_listings

QUIET_ZONE = 4  # modules of white round a QR code, as the standard asks
PHONE_KEEPS = (
    "Whatever a phone shows is on that phone: Tor Browser and Android may keep it (the open "
    "tab, screenshots and the recent-apps preview, what the keyboard learns, backups). That "
    "goes for private scenes, memory-only chats and private models too: SealedLore can't "
    "promise for a phone what it promises for this computer."
)
HOW_TO_PAIR = (
    "On the phone: open Tor Browser and let it connect. Scan this code with the phone's camera, "
    "tap Copy, and paste it into Tor Browser's address bar. (The camera opens links in the "
    "phone's usual browser, which can't reach an onion address.) Then compare the four digits "
    "the phone shows with the ones SealedLore asks you to allow."
)
NO_MAP_YET = "A visual story: phones play text stories and chats only, for now."


class QrWidget(QWidget):
    """A QR code, black on white whatever the theme, with its quiet zone."""

    def __init__(self, matrix: qr.Matrix, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.matrix = matrix
        side = (len(matrix) + 2 * QUIET_ZONE) * 6
        self.setMinimumSize(side, side)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        modules = len(self.matrix) + 2 * QUIET_ZONE
        scale = max(1, min(self.width(), self.height()) // modules)  # whole pixels
        side = scale * modules
        left = (self.width() - side) // 2
        top = (self.height() - side) // 2
        painter = QPainter(self)
        painter.fillRect(QRectF(left, top, side, side), QColor("white"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("black"))
        for row, line in enumerate(self.matrix):
            for col, dark in enumerate(line):
                if dark:
                    x = left + (col + QUIET_ZONE) * scale
                    y = top + (row + QUIET_ZONE) * scale
                    painter.drawRect(QRectF(x, y, scale, scale))
        painter.end()


class PairSheet(QDialog):
    """The pairing code, for CODE_SECONDS; closing it spends the code."""

    def __init__(self, window, link: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = weakref.ref(window)
        self.setWindowTitle("Pair a phone")
        self.link = link
        self._left = CODE_SECONDS
        layout = QVBoxLayout(self)
        how = QLabel(HOW_TO_PAIR)
        how.setWordWrap(True)
        layout.addWidget(how)
        self.code = QrWidget(qr.encode(link))
        layout.addWidget(self.code, 1)
        shown = QLabel(link)
        shown.setObjectName("monoLabel")
        shown.setWordWrap(True)
        shown.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(shown)
        row = QHBoxLayout()
        copy = QPushButton("Copy link")
        copy.clicked.connect(lambda: copy_text(self.link))
        row.addWidget(copy)
        self.countdown = QLabel()
        self.countdown.setObjectName("hintLabel")
        row.addWidget(self.countdown, 1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()
        size_to_screen(self, 0.3, 0.7)

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self.countdown.setText("This code has run out. Close and pair again.")
            self.code.setEnabled(False)
            self._timer.stop()
            return
        minutes, seconds = divmod(self._left, 60)
        self.countdown.setText(f"Good for {minutes}:{seconds:02d}, and once.")

    def done(self, result: int) -> None:
        self._timer.stop()
        window = self._window()
        if window is not None:
            window.phone_pairing_closed()
        super().done(result)


class PhoneDevicesDialog(QDialog):
    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = weakref.ref(window)
        self.setWindowTitle("Phone devices")
        self.pair_sheet: PairSheet | None = None
        layout = QVBoxLayout(self)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.on_button = QPushButton()
        self.on_button.clicked.connect(self._toggle)
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.on_button)
        layout.addLayout(top)

        self.address = QLabel()
        self.address.setObjectName("monoLabel")
        self.address.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.address.setWordWrap(True)
        layout.addWidget(self.address)

        self.pair_button = QPushButton("Pair a phone…")
        self.pair_button.clicked.connect(self.pair)
        layout.addWidget(self.pair_button, 0, Qt.AlignLeft)

        phones = QGroupBox("Paired phones")
        phones_layout = QVBoxLayout(phones)
        self.devices = QTableWidget(0, 4)
        self.devices.setHorizontalHeaderLabels(["Name", "Paired", "Last seen", ""])
        self.devices.verticalHeader().hide()
        self.devices.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.devices.setSelectionMode(QAbstractItemView.NoSelection)
        self.devices.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        phones_layout.addWidget(self.devices)
        layout.addWidget(phones, 1)

        stories = QGroupBox("Stories on your phones")
        stories_layout = QVBoxLayout(stories)
        hint = QLabel("A phone sees only the stories ticked here, and nothing of any other.")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        stories_layout.addWidget(hint)
        self.stories = QListWidget()
        self.stories.itemChanged.connect(self._story_ticked)
        stories_layout.addWidget(self.stories)
        layout.addWidget(stories, 1)

        keeps = QLabel(PHONE_KEEPS)
        keeps.setObjectName("hintLabel")
        keeps.setWordWrap(True)
        layout.addWidget(keeps)

        bottom = QHBoxLayout()
        self.new_address = QPushButton("New address…")
        self.new_address.setToolTip(
            "A new onion address: every paired phone is forgotten and pairs again"
        )
        self.new_address.clicked.connect(self._new_address)
        bottom.addWidget(self.new_address)
        self.details_button = QToolButton()
        self.details_button.setText("Details ▸")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._show_details)
        bottom.addWidget(self.details_button)
        bottom.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setObjectName("monoText")
        self.details.hide()
        layout.addWidget(self.details, 1)

        window.phone_access_changed.connect(self.refresh)
        size_to_screen(self, 0.4, 0.75)
        self.refresh()

    # --- showing --------------------------------------------------------------------

    def refresh(self) -> None:
        window = self._window()
        if window is None:
            return
        step = window.phone_step
        on = step != "off"
        if not on:
            self.status.setText(
                "Phone access is off: nothing listens for phones and tor isn't running."
            )
        else:
            self.status.setText(window.phone_words)
        self.on_button.setText("Turn off" if on else "Turn on")
        onion = window._phone.onion if window._phone is not None else ""
        self.address.setText(f"Address: {onion}" if onion else "")
        self.address.setVisible(bool(onion))
        self.pair_button.setEnabled(step == "ready")
        self.pair_button.setToolTip(
            "" if step == "ready" else "Once phone access is on and its address published"
        )
        self._fill_devices(window)
        self._fill_stories(window)
        if self.details.isVisible():
            self._fill_details(window)

    def _fill_devices(self, window) -> None:
        devices = window.config.phone.devices
        today = date.today().isoformat()
        # Built again only when the phones change: refreshed on every step of
        # tor's, the rows' Revoke buttons were replaced each time and the old
        # ones lingered over the names until Qt deleted them.
        shown = [(d.id, d.name, d.paired_at, d.last_seen_day == today) for d in devices]
        if shown == getattr(self, "_shown_devices", None):
            return
        self._shown_devices = shown
        self.devices.setRowCount(0)
        self.devices.setRowCount(len(devices))
        for row, device in enumerate(devices):
            self.devices.setItem(row, 0, QTableWidgetItem(device.name))
            self.devices.setItem(row, 1, QTableWidgetItem(device.paired_at[:10]))
            seen = "today" if device.last_seen_day == today else device.last_seen_day or "never"
            self.devices.setItem(row, 2, QTableWidgetItem(seen))
            revoke = QPushButton("Revoke")
            revoke.clicked.connect(lambda _=False, d=device.id, n=device.name: self._revoke(d, n))
            self.devices.setCellWidget(row, 3, revoke)
        self.devices.resizeColumnsToContents()

    def _fill_stories(self, window) -> None:
        self.stories.blockSignals(True)
        self.stories.clear()
        session = window.session
        if session is not None and session.memory_only:
            item = QListWidgetItem(f"{session.story.title} (this memory-only chat, for this run)")
            item.setData(Qt.UserRole, (session.story.id, True))
            self._tick_item(item, window.phone_shared(session.story.id), session.story.visual)
            self.stories.addItem(item)
        for listing in story_listings(window.root):
            if listing.error is not None:
                continue
            item = QListWidgetItem(listing.title)
            item.setData(Qt.UserRole, (listing.id, False))
            self._tick_item(item, window.phone_shared(listing.id), listing.keeps_map)
            self.stories.addItem(item)
        self.stories.blockSignals(False)

    def _tick_item(self, item: QListWidgetItem, shared: bool, keeps_map: bool) -> None:
        if keeps_map:
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsUserCheckable)
            item.setText(item.text() + " · a visual story")
            item.setToolTip(NO_MAP_YET)
            return
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if shared else Qt.Unchecked)

    def _fill_details(self, window) -> None:
        service = window._tor
        if service is None:
            self.details.setPlainText("Tor isn't running.")
            return
        lines = [f"Tor: {service.tor}", f"Data: {service.paths.data}", ""]
        lines += list(service.log)
        self.details.setPlainText("\n".join(lines))

    # --- the author's -------------------------------------------------------------------

    def _toggle(self) -> None:
        window = self._window()
        if window is not None:
            window.set_phone_access(window.phone_step == "off")

    def pair(self) -> None:
        window = self._window()
        if window is None:
            return
        link = window.phone_pairing_link()
        if link is None:
            return
        if self.pair_sheet is not None:
            self.pair_sheet.close()
        self.pair_sheet = PairSheet(window, link, self)
        self.pair_sheet.finished.connect(self._pair_sheet_closed)
        self.pair_sheet.show()

    def _pair_sheet_closed(self) -> None:
        sheet, self.pair_sheet = self.pair_sheet, None
        if sheet is not None:
            sheet.deleteLater()
        self.refresh()

    def _revoke(self, device_id: str, name: str) -> None:
        window = self._window()
        if window is None:
            return
        answer = QMessageBox.question(
            self,
            "Revoke a phone",
            f"Revoke “{name}”? It can't connect again until it is paired again.",
        )
        if answer == QMessageBox.Yes:
            window.phone_revoke(device_id)
            self.refresh()

    def _story_ticked(self, item: QListWidgetItem) -> None:
        window = self._window()
        if window is None:
            return
        story_id, memory = item.data(Qt.UserRole)
        shared = item.checkState() == Qt.Checked
        if shared and not window.phone_confirm_share(story_id, memory):
            self.stories.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.stories.blockSignals(False)
            return
        window.phone_share(story_id, shared, memory=memory)

    def _new_address(self) -> None:
        window = self._window()
        if window is None:
            return
        answer = QMessageBox.question(
            self,
            "New address",
            "Give phone access a new onion address? Every paired phone is forgotten, its "
            "bookmark stops working, and it has to be paired again.",
        )
        if answer == QMessageBox.Yes:
            reason = window.phone_new_address()
            if reason:
                QMessageBox.information(self, "Phone access", reason)
            self.refresh()

    def _show_details(self, shown: bool) -> None:
        self.details_button.setText("Details ▾" if shown else "Details ▸")
        self.details.setVisible(shown)
        window = self._window()
        if shown and window is not None:
            self._fill_details(window)
