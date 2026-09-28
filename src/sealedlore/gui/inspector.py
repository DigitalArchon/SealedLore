"""The Context Inspector.

Shows the exact assembled request: each section with its token count and
boundary, where the cache breakpoints landed, and the raw JSON payload.
Without this, debugging cache behaviour and budget overruns is guesswork.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from html import escape

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.prompt import AssembledPrompt
from sealedlore.gui.winprivacy import copy_text
from sealedlore.messages import PromptMessage
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import ChatRequest
from sealedlore.providers.wire import build_chat_payload

# The prompt's sections in the author's words; the id (what the code and the
# API log call it) is the row's tooltip. An id not listed shows as itself.
SECTION_LABELS = {
    "system.engine_rules": "Rules",
    "system.chat_prompt": "System prompt",
    "tail.chat_tail": "Tail",
    "system.private_rules": "Private scene rules",
    "system.private_notes": "Private scene notes",
    "system.style": "Style",
    "system.perspective": "Perspective",
    "system.world_activity": "World activity",
    "system.world_bible": "World",
    "system.lore": "Lore, every turn",
    "system.cast": "Cast",
    "summaries": "Chapters",
    "history": "Recent story",
    "tail.lore": "Lore for this turn",
    "tail.supporting": "Supporting characters",
    "tail.scene_log": "Recent scenes",
    "tail.story_time": "Story time",
    "tail.scene": "This scene",
    "tail.direction": "Plot direction",
    "tail.directives": "This turn's instructions",
    "tail.author_turn": "Your turn",
    "tail.question": "Your question",
    "tail.reminder": "Final reminder",
}


class ContextInspector(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.summary = QLabel("Nothing assembled yet.")
        self.summary.setObjectName("inspectorSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        # One click to take the prompt away for analysis: selecting the whole
        # payload by hand was the only way before.
        self.copy_text = QPushButton("Copy prompt")
        self.copy_text.setToolTip(
            "Copy every message the storyteller is sent, as readable text, headed by its role"
        )
        self.copy_text.clicked.connect(self._copy_text)
        self.copy_json = QPushButton("Copy request JSON")
        self.copy_json.setToolTip(
            "Copy the exact request body sent to the endpoint, cache markers included"
        )
        self.copy_json.clicked.connect(self._copy_json)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.copy_text)
        buttons.addWidget(self.copy_json)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        splitter = QSplitter(Qt.Vertical)

        self.sections = QTreeWidget()
        self.sections.setColumnCount(3)
        self.sections.setHeaderLabels(["Section", "Tokens", "Cache"])
        self.sections.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.sections.setRootIsDecorated(False)
        self.sections.setAlternatingRowColors(True)
        splitter.addWidget(self.sections)

        self.payload = QPlainTextEdit()
        self.payload.setObjectName("inspectorPayload")
        self.payload.setReadOnly(True)
        self.payload.setLineWrapMode(QPlainTextEdit.NoWrap)
        splitter.addWidget(self.payload)

        splitter.setSizes([320, 260])
        layout.addWidget(splitter, 1)

        self.sections.currentItemChanged.connect(self._show_section_text)
        self._section_text: dict[str, str] = {}
        self._payload_json = ""
        self._messages: tuple[PromptMessage, ...] = ()
        self._set_copyable(False)

    def clear(self) -> None:
        """Back to the no-story state, after the open story is closed or deleted."""
        self.summary.setText("Nothing assembled yet.")
        self.sections.clear()
        self.payload.clear()
        self._section_text = {}
        self._payload_json = ""
        self._messages = ()
        self._set_copyable(False)

    def show_prompt(
        self,
        prompt: AssembledPrompt,
        *,
        model: str,
        params: GenerationParams,
        use_cache_control: bool,
        approximate: bool,
        warnings: Sequence[str] = (),
        extra_body: dict | None = None,
    ) -> None:
        """`warnings`: things wrong with what is sent that the author should
        fix, one line each (style notes that say who plays whom).
        `extra_body`: the story model's route, as it is sent."""
        budget = prompt.budget
        tilde = "~" if approximate else ""
        lines = [
            f"<b>{model or 'no model set'}</b> — "
            f"{tilde}{budget.total:,} / {budget.budget:,} tokens "
            f"({budget.utilization:.0%})",
            f"cache_control: {'on' if use_cache_control else 'off'} · "
            f"{len(prompt.breakpoints)} breakpoint(s)"
            f"{'' if use_cache_control else ' (not sent)'} · "
            f"{len(prompt.messages)} messages",
        ]
        if budget.over_budget:
            lines.append("<span style='color:#f7768e'>over budget</span>")
        elif budget.warning:
            lines.append("<span style='color:#e0af68'>at 90% of budget</span>")
        if prompt.needs_archival:
            lines.append(
                f"<span style='color:#e0af68'>{len(prompt.excluded_node_ids)} oldest turns "
                "excluded to fit</span>"
            )
        lines += [f"<span style='color:#e0af68'>{escape(text)}</span>" for text in warnings]
        self.summary.setText("<br>".join(lines))

        breakpoint_by_section = {
            marker.section_name: f"break · {marker.segment_tokens:,}"
            for marker in prompt.breakpoints
        }
        self.sections.clear()
        self._section_text.clear()
        for section in prompt.sections:
            marker = breakpoint_by_section.get(section.name, "")
            label = SECTION_LABELS.get(section.name, section.name)
            item = QTreeWidgetItem([label, f"{tilde}{section.tokens:,}", marker])
            item.setData(0, Qt.UserRole, section.name)
            item.setToolTip(0, section.name)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.sections.addTopLevelItem(item)
            self._section_text[section.name] = section.text

        payload = build_chat_payload(
            ChatRequest(
                model=model,
                messages=prompt.messages,
                params=params,
                use_cache_control=use_cache_control,
                extra_body=dict(extra_body or {}),
            )
        )
        self._payload_json = json.dumps(payload, indent=2, ensure_ascii=False)
        self._messages = prompt.messages
        self.payload.setPlainText(self._payload_json)
        self._set_copyable(True)

    def prompt_text(self) -> str:
        """The prompt as it reads: each message under its role."""
        return "\n\n".join(
            f"===== {message.role.upper()} =====\n\n{message.text}" for message in self._messages
        )

    def _set_copyable(self, on: bool) -> None:
        self.copy_text.setEnabled(on)
        self.copy_json.setEnabled(on)

    def _copy_text(self) -> None:
        self._copy(self.copy_text, "Copy prompt", self.prompt_text())

    def _copy_json(self) -> None:
        self._copy(self.copy_json, "Copy request JSON", self._payload_json)

    def _copy(self, button: QPushButton, label: str, text: str) -> None:
        copy_text(text)
        button.setText("Copied")
        QTimer.singleShot(1500, lambda: button.setText(label))

    def _show_section_text(self, current: QTreeWidgetItem | None) -> None:
        if current is None:
            self.payload.setPlainText(self._payload_json)
            return
        text = self._section_text.get(current.data(0, Qt.UserRole))
        self.payload.setPlainText(text if text else self._payload_json)
