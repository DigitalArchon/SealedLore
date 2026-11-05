"""System prompts the author keeps for simple chats (`Config.chat_prompts`),
chosen where one is written: New simple chat, Chat with them… (Your
characters), and a chat's Setup. The author (Nov 4 2026): "For Simple Chats
we should also be able to save a list of favorite system prompts to quickly
select."
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QPlainTextEdit, QToolButton

from sealedlore.models.config import Config, SavedPrompt

SAVED_LABEL = "Saved prompts"
KEEP_PROMPT_LABEL = "Save this prompt…"


class SavedPrompts:
    """The config's list, and how it is kept."""

    def __init__(self, config: Config, save: Callable[[], None]) -> None:
        self._config = config
        self._save = save

    @property
    def prompts(self) -> list[SavedPrompt]:
        return list(self._config.chat_prompts)

    def keep(self, name: str, text: str) -> None:
        """Kept under `name`, in place of one of that name."""
        name = name.strip()
        prompts = [p for p in self._config.chat_prompts if p.name.casefold() != name.casefold()]
        prompts.append(SavedPrompt(name=name, text=text))
        self._config.chat_prompts = sorted(prompts, key=lambda p: p.name.casefold())
        self._save()

    def forget(self, name: str) -> None:
        self._config.chat_prompts = [p for p in self._config.chat_prompts if p.name != name]
        self._save()


def _suggested_name(text: str) -> str:
    words = text.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


class SavedPromptsButton(QToolButton):
    """Beside a system prompt: a saved one put in its place, this one kept,
    or one forgotten."""

    def __init__(
        self,
        saved: SavedPrompts,
        target: QPlainTextEdit,
        in_memory: Callable[[], bool] | None = None,
    ) -> None:
        """`in_memory`: whether the chat the prompt is for is kept in memory
        only, when keeping its prompt in the settings is asked first."""
        super().__init__()
        self._saved = saved
        self._target = target
        self._in_memory = in_memory or (lambda: False)
        self.setText(SAVED_LABEL)
        self.setToolTip("System prompts you keep for chats: put one here, or keep this one")
        self.setPopupMode(QToolButton.InstantPopup)
        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._fill)
        self.setMenu(self._menu)

    def _fill(self) -> None:
        menu = self._menu
        menu.clear()
        prompts = self._saved.prompts
        if not prompts:
            menu.addAction("None saved yet").setEnabled(False)
        for prompt in prompts:
            action = menu.addAction(prompt.name)
            action.setToolTip(prompt.text[:300])
            action.triggered.connect(lambda _checked=False, p=prompt: self.use(p))
        menu.addSeparator()
        keep = menu.addAction(KEEP_PROMPT_LABEL)
        keep.setEnabled(bool(self._target.toPlainText().strip()))
        keep.triggered.connect(self.keep_current)
        if prompts:
            forget = menu.addMenu("Forget one")
            for prompt in prompts:
                forget.addAction(prompt.name).triggered.connect(
                    lambda _checked=False, name=prompt.name: self._forget(name)
                )

    def use(self, prompt: SavedPrompt) -> None:
        """In place of what the box has, Ctrl+Z bringing it back."""
        cursor = self._target.textCursor()
        cursor.select(QTextCursor.Document)
        cursor.insertText(prompt.text)
        self._target.setTextCursor(cursor)

    def keep_current(self) -> None:
        text = self._target.toPlainText().strip()
        if not text:
            return
        name, ok = QInputDialog.getText(
            self, KEEP_PROMPT_LABEL, "A name for this system prompt:", text=_suggested_name(text)
        )
        if not ok or not name.strip():
            return
        if self._in_memory() and (
            QMessageBox.question(
                self,
                KEEP_PROMPT_LABEL,
                "This chat is kept in memory only, and nothing of it is written to disk. "
                "Saving its system prompt keeps it in SealedLore's settings on this computer. "
                "Save it anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        taken = any(p.name.casefold() == name.strip().casefold() for p in self._saved.prompts)
        if taken and (
            QMessageBox.question(
                self,
                KEEP_PROMPT_LABEL,
                f"Replace the one named “{name}”?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self._saved.keep(name, text)

    def _forget(self, name: str) -> None:
        answer = QMessageBox.question(
            self,
            "Forget a saved prompt",
            f"Forget “{name}”?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._saved.forget(name)
