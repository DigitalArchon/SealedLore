"""The window's side of the Lore page's Lore fires by choice and SillyTavern
settings… (gui/lore_panel.py, engine/session_tavern.py): the story's own
choice, saved; the prompt shown again, since what stands cached with the
world changes with it."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog

from sealedlore.gui.tavern_settings_dialog import TavernSettingsDialog

SAID = {
    "sillytavern": (
        "The lore now fires by SillyTavern's rules, with each entry's own settings. "
        "The next turn writes the cache afresh."
    ),
    "sealedlore": "The lore fires by SealedLore's rules again (Settings → Lore).",
}


class TavernFiringWindow:
    def _lore_firing(self) -> str:
        """How the open story's lore fires, for what an import says."""
        return self.session.story.defaults.lore_firing if self.session else "sealedlore"

    def _on_firing_changed(self, mode: str) -> None:
        if self.session is None or self._busy or self.session.story.chat:
            # Locked while a job runs; a chat has no lore.
            current = self.session.story.defaults.lore_firing if self.session else "sealedlore"
            self.lore_panel.set_firing(current)
            return
        # An earlier review's Undo would put the rules back as they were.
        self.session.forget_review_undo()
        self.session.story.defaults.lore_firing = mode
        self.session.save()
        self.statusBar().showMessage(SAID[mode], 8000)
        self._reassemble_inspector()
        # Another budget, so other entries may go condensed.
        self._lore_vectors_later()

    def _open_tavern_settings(self) -> None:
        if self.session is None or self._busy:
            return
        dialog = TavernSettingsDialog(self.session.story.defaults.tavern_world_info, self)
        try:
            if dialog.exec() == QDialog.Accepted:
                self.session.forget_review_undo()
                self.session.story.defaults.tavern_world_info = dialog.settings()
                self.session.save()
                self._reassemble_inspector()
                self._lore_vectors_later()
        finally:
            dialog.deleteLater()
