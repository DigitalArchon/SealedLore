"""The main window's side of how it looks: View → Text size, Theme and
Fonts…. A mixin for `MainWindow`; the scaling is `gui/text_size.py`, the
colours and fonts `gui/theme.py`.

Asked for by the author for people with poor eyesight: every piece of text
in the window, the story, the panels, the menus and the map, at one size the
reader picks, kept in the config. The theme and fonts restyle the same way,
through `text_size.apply`.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QApplication, QDialog

from sealedlore.gui import text_size, theme
from sealedlore.gui.fonts_dialog import FontsDialog
from sealedlore.storage.repository import save_config


class TextSizeWindow:
    def _text_size_actions(self, view_menu) -> None:
        """Called from `_build_actions`: View → Text size."""
        menu = view_menu.addMenu("&Text size")
        larger = QAction("&Larger", self)
        larger.setShortcuts([QKeySequence(QKeySequence.ZoomIn), QKeySequence("Ctrl+=")])
        larger.setStatusTip("Make all the text in the window larger")
        larger.triggered.connect(lambda: self.set_text_scale(text_size.step(self._text_scale(), 1)))
        smaller = QAction("&Smaller", self)
        smaller.setShortcut(QKeySequence(QKeySequence.ZoomOut))
        smaller.setStatusTip("Make all the text in the window smaller")
        smaller.triggered.connect(
            lambda: self.set_text_scale(text_size.step(self._text_scale(), -1))
        )
        normal = QAction("&Normal size", self)
        normal.setShortcut(QKeySequence("Ctrl+0"))
        normal.setStatusTip("All the text in the window at the size it was designed for")
        normal.triggered.connect(lambda: self.set_text_scale(1.0))
        menu.addActions([larger, smaller, normal])
        menu.addSeparator()
        group = QActionGroup(self)
        group.setExclusive(True)
        self.text_size_choices: dict[float, QAction] = {}
        for value in text_size.SCALES:
            action = QAction(f"{round(value * 100)}%", self)
            action.setCheckable(True)
            action.triggered.connect(lambda _on=False, v=value: self.set_text_scale(v))
            group.addAction(action)
            menu.addAction(action)
            self.text_size_choices[value] = action
        self._check_text_size()
        self._appearance_actions(view_menu)
        # The config's look, before the window is shown. The default look
        # (100%, Dark, the app's fonts) is what the app started in already.
        theme.set_theme(self.config.theme)
        theme.set_fonts(self.config.ui_font, self.config.story_font, self.config.mono_font)
        self._check_theme()
        defaults = theme.Fonts() == theme.fonts() and theme.current().key == theme.DEFAULT_THEME
        if self._text_scale() != 1.0 or not defaults:
            text_size.apply(QApplication.instance(), self._text_scale())
        # A window moved to another screen may have more room, or less.
        QTimer.singleShot(0, self._watch_screen)

    def _appearance_actions(self, view_menu) -> None:
        """View → Theme and View → Fonts…."""
        menu = view_menu.addMenu("T&heme")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.theme_choices: dict[str, QAction] = {}
        tips = {
            "dark": "The app's own look",
            "high_contrast": "Black and white, with bright borders and marks: for poor eyesight",
        }
        for key, choice in theme.THEMES.items():
            action = QAction(choice.label, self)
            action.setCheckable(True)
            action.setStatusTip(tips.get(key, choice.label))
            action.triggered.connect(lambda _on=False, k=key: self.set_theme(k))
            group.addAction(action)
            menu.addAction(action)
            self.theme_choices[key] = action
        fonts = QAction("&Fonts…", self)
        fonts.setStatusTip("Choose the fonts for the window, the story and the Prompt tab")
        fonts.triggered.connect(self.choose_fonts)
        view_menu.addAction(fonts)

    def _check_theme(self) -> None:
        for key, action in self.theme_choices.items():
            action.setChecked(key == theme.current().key)

    def set_theme(self, key: str) -> None:
        key = theme.known(key)
        changed = key != self.config.theme
        self.config.theme = key
        theme.set_theme(key)
        self._check_theme()
        if changed:
            save_config(self.config, root=self.root)
        self._restyle(f"Theme: {theme.current().label}")

    def choose_fonts(self) -> None:
        dialog = FontsDialog(theme.fonts(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = dialog.chosen()
        if chosen == theme.fonts():
            return
        self.config.ui_font, self.config.story_font, self.config.mono_font = (
            chosen.ui,
            chosen.story,
            chosen.mono,
        )
        save_config(self.config, root=self.root)
        theme.set_fonts(chosen.ui, chosen.story, chosen.mono)
        self._restyle("Fonts changed")

    def _watch_screen(self) -> None:
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(lambda _screen: self._fit_centre_width())

    def _text_scale(self) -> float:
        return text_size.clamp(self.config.text_scale)

    def _check_text_size(self) -> None:
        for value, action in self.text_size_choices.items():
            action.setChecked(abs(value - self._text_scale()) < 1e-6)

    def set_text_scale(self, value: float) -> None:
        value = text_size.clamp(value)
        changed = value != self.config.text_scale
        self.config.text_scale = value
        self._check_text_size()
        if changed:
            save_config(self.config, root=self.root)
        if not changed and value == text_size.scale():
            return
        self._restyle(f"Text size {round(value * 100)}%", scale=value)

    def _restyle(self, told: str, *, scale: float | None = None) -> None:
        """Every widget in the current text size, theme and fonts, and what
        is drawn in code redrawn in them; `told` goes to the status bar."""
        place = self.transcript.place_of()
        text_size.apply(QApplication.instance(), self._text_scale() if scale is None else scale)
        self.transcript.restyle_background()
        # Word-wrapped messages are measured once per width: a rebuild
        # measures them again at the new size, where the reader was.
        if self.session is not None and not self._busy:
            self.reload_transcript()
            self.transcript.keep_place(place)
            self.refresh_panels()
        elif self.session is None:
            self.reload_transcript()  # the welcome's links are in the theme's colour
        if self.map_shown():
            self.refresh_map(keep_view=True)
        self._fit_centre_width()
        screen = self.screen()
        if screen is not None:
            room = screen.availableGeometry()
            need = self.minimumSizeHint()
            if need.height() > room.height() or need.width() > room.width():
                told += (
                    f": the window needs {need.width():,}×{need.height():,}px and this "
                    f"screen has {room.width():,}×{room.height():,}. A smaller size will fit."
                )
        self.statusBar().showMessage(told, 10000)

    def _fit_centre_width(self) -> None:
        """The centre keeps the width its rows want on one line while the
        screen has room for it, and gives way (the rows wrap) only past that.

        The composer's rows and the one above the transcript wrap
        (`WrapRow`), so the centre could be far narrower; but the docks take
        whatever the centre doesn't insist on, and at 100% on 1366px the
        composer then wrapped to four lines where it had always had two.
        """
        centre = self.centralWidget()
        if centre is None:
            return
        wanted = max(self.composer.sizeHint().width(), self.top_row.sizeHint().width())
        screen = self.screen()
        if screen is not None:
            centre.setMinimumWidth(0)
            others = self.minimumSizeHint().width() - centre.minimumSizeHint().width()
            wanted = min(wanted, screen.availableGeometry().width() - others)
        centre.setMinimumWidth(max(0, wanted))
