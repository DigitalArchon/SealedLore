"""Settings → Images, a server of the author's own (ComfyUI, AUTOMATIC1111 /
Forge): its workflows, how far a picture may change, the settings put into
each request, and a check that the server answers and what it has.

Shown under the picture endpoint's Service while one of LOCAL_IMAGE_APIS is
chosen. The fields are ordinary ones (a workflow is a text box to paste
ComfyUI's Export (API) into, or fill from its file), so Settings' Close
notices a change as it does any other.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.file_dialogs import open_file_name
from sealedlore.gui.sizing import GrowingText
from sealedlore.models.config import (
    LOCAL_IMAGE_ADDRESSES,
    LOCAL_IMAGE_APIS,
    MediaEndpoint,
)
from sealedlore.providers.local_images import (
    SERVICE_NAMES,
    WORKFLOW_MAX_CHARS,
    WorkflowProblem,
    pictures_taken,
    placeholders,
    read_workflow,
)
from sealedlore.storage.atomic import outside_json

BUILT_IN = "Blank: the built-in workflow, for any ordinary checkpoint."
WORKFLOW_HINT = (
    "Paste a workflow from ComfyUI (Workflow → Export (API)), or Load a file…. "
    "Its {{prompt}}, {{width}}, {{height}}, {{seed}}, {{model}}, {{image}} … are "
    "filled in each time."
)
EXTRA_TIPS = {
    "a1111": (
        "Fields put into every request as they are, in JSON: for example "
        '{"steps": 30, "cfg_scale": 5, "sampler_name": "DPM++ 2M", "scheduler": "Karras", '
        '"negative_prompt": "blurry"}. A FLUX checkpoint on Forge wants '
        '{"cfg_scale": 1, "distilled_cfg_scale": 3.5}.'
    ),
    "comfyui": (
        "What the built-in workflows sample with, in JSON: steps, cfg, sampler_name, "
        "scheduler and negative_prompt (ComfyUI's own names), for example "
        '{"steps": 30, "cfg": 5, "sampler_name": "euler", "scheduler": "normal"}. '
        "A workflow of your own gets them at its {{steps}}, {{cfg}}… placeholders."
    ),
}
NOTES = {
    "a1111": (
        "Drawn by AUTOMATIC1111 or Forge on your own computer or network: nothing "
        "charged, and nothing leaves it. Start it with --api (and --listen to reach it "
        "from another machine). Blank address: {address}. If it runs with --api-auth, "
        "put user:password in the API key field. Its checkpoints are the models."
    ),
    "comfyui": (
        "Drawn by ComfyUI on your own computer or network: nothing charged, and "
        "nothing leaves it. Blank address: {address}. Its checkpoints are the models; "
        "for FLUX, an edit model or anything the built-in workflow can't drive, give "
        "it a workflow of your own. A picture sent with the prompt is uploaded to its "
        "input folder."
    ),
}


class OwnImageServer(QWidget):
    """The rows for a server of the author's own, in a form of their own so
    the page can show and hide them with the service. `check` runs the
    dialog's check of the server as typed (its address and key are the
    page's fields, and whether a key may be sent the dialog's to ask)."""

    def __init__(self, endpoint: MediaEndpoint, check: Callable[[], None]) -> None:
        super().__init__()
        self.setObjectName("ownImageServer")
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.note = QLabel()
        self.note.setObjectName("hintLabel")
        self.note.setWordWrap(True)
        form.addRow(self.note)

        self.workflow, self.workflow_note, words = self._workflow_row(endpoint.workflow)
        self.workflow_edit, self.workflow_edit_note, edit = self._workflow_row(
            endpoint.workflow_edit
        )
        self._workflow_rows = (words, edit)
        form.addRow("Workflow, from words", words)
        form.addRow("Workflow, from a picture", edit)

        self.strength = QDoubleSpinBox()
        self.strength.setRange(0.05, 1.0)
        self.strength.setSingleStep(0.05)
        self.strength.setDecimals(2)
        self.strength.setValue(endpoint.strength)
        self.strength.setToolTip(
            "How far a picture sent with the prompt may change (img2img's denoising "
            "strength): low keeps more of it, high follows the prompt more. A new "
            "expression or clothes: about 0.5-0.65."
        )
        form.addRow("A picture may change", self.strength)

        self.extra = QLineEdit("" if not endpoint.extra_body else _compact(endpoint.extra_body))
        self.extra.setPlaceholderText('{"steps": 30}')
        form.addRow("Extra settings", self.extra)

        self.check_button = QPushButton("Check the server")
        self.check_button.clicked.connect(check)
        self.check_note = QLabel()
        self.check_note.setObjectName("hintLabel")
        self.check_note.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.check_button)
        row.addWidget(self.check_note, 1)
        form.addRow(row)
        self._form = form
        self.api = ""

    def _workflow_row(self, text: str) -> tuple[GrowingText, QLabel, QWidget]:
        box = GrowingText(text, lines=2, most=8)
        box.setPlaceholderText(BUILT_IN)
        box.setToolTip(WORKFLOW_HINT)
        note = QLabel()
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        load = QPushButton("Load a file…")
        load.clicked.connect(lambda: self._load(box))
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(box)
        line = QHBoxLayout()
        line.addWidget(note, 1)
        line.addWidget(load)
        column.addLayout(line)
        box.textChanged.connect(lambda: self._describe(box, note))
        self._describe(box, note)
        return box, note, holder

    def _load(self, box: GrowingText) -> None:
        path, _filter = open_file_name(
            self, "Choose a ComfyUI workflow (API format)", "", "Workflows (*.json);;All (*)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(WORKFLOW_MAX_CHARS + 1)
        except (OSError, UnicodeDecodeError) as exc:
            self.check_note.setText(f"Couldn't read {path}: {exc}")
            return
        box.setPlainText(text)

    def _describe(self, box: GrowingText, note: QLabel) -> None:
        note.setText(describe_workflow(box.toPlainText()))

    def set_service(self, api: str, address: str = "") -> None:
        """Shown for a server of one's own, its rows for its service."""
        self.api = api
        own = api in LOCAL_IMAGE_APIS
        self.setVisible(own)
        if not own:
            return
        self.note.setText(NOTES[api].format(address=LOCAL_IMAGE_ADDRESSES[api]))
        comfy = api == "comfyui"
        for holder in self._workflow_rows:
            self._form.setRowVisible(holder, comfy)
        self.extra.setToolTip(EXTRA_TIPS[api])

    def show_check(self, words: str) -> None:
        self.check_note.setText(words)

    def values(self, *, checked: bool = True) -> dict[str, Any]:
        """The endpoint's fields for a server of one's own. `checked`: raise
        ValueError, in words, for a workflow ComfyUI couldn't run or
        settings that aren't a JSON object (Save), else keep what can be."""
        workflow = self.workflow.toPlainText().strip()
        workflow_edit = self.workflow_edit.toPlainText().strip()
        # What a service not in use holds is kept, unchecked, for going back.
        checked = checked and self.api in LOCAL_IMAGE_APIS
        if checked and self.api == "comfyui":
            for name, text in (("from words", workflow), ("from a picture", workflow_edit)):
                if text:
                    try:
                        read_workflow(text)
                    except WorkflowProblem as exc:
                        raise ValueError(f"the workflow {name}: {exc}") from None
        extra = self.extra.text().strip()
        body: dict[str, Any] = {}
        if extra:
            try:
                parsed = outside_json(extra)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
            elif checked:
                raise ValueError(
                    'Extra settings must be JSON between braces, such as {"steps": 30}.'
                )
        return {
            "workflow": workflow,
            "workflow_edit": workflow_edit,
            "strength": round(self.strength.value(), 2),
            "extra_body": body,
        }


def describe_workflow(text: str) -> str:
    """A line under a workflow box: built in, what it holds, or what is wrong."""
    if not text.strip():
        return "Built in."
    try:
        workflow = read_workflow(text)
    except WorkflowProblem as exc:
        return f"⚠ {str(exc)[:1].upper()}{str(exc)[1:]}."
    found = placeholders(workflow)
    nodes = f"{len(workflow)} node{'s' if len(workflow) != 1 else ''}"
    if not found:
        return f"Yours, {nodes}, with no placeholders: every picture is drawn the same."
    pictures = pictures_taken(workflow)
    takes = f"; takes {pictures} picture{'s' if pictures != 1 else ''}" if pictures else ""
    unknown = [name for name in found if not _known(name)]
    words = ", ".join("{{" + name + "}}" for name in found if _known(name))
    said = f"Yours, {nodes}: {words}{takes}."
    if "prompt" not in found:
        said += " ⚠ No {{prompt}}: the prompt won't reach it."
    if unknown:
        said += " ⚠ Not filled in: " + ", ".join("{{" + name + "}}" for name in unknown) + "."
    return said


def _known(name: str) -> bool:
    from sealedlore.providers.local_images import PLACEHOLDERS

    return name in PLACEHOLDERS


def _compact(body: dict[str, Any]) -> str:
    import json

    return json.dumps(body, ensure_ascii=False)


def typed_words(api: str, typed: Exception) -> str:
    """What Check the server says of a workflow whose model is free text
    (`local_images.ModelAsTyped`): it answered, and has no list to offer."""
    return f"✓ {SERVICE_NAMES.get(api, api)} answered. {typed}"


def found_words(api: str, models: list[Any]) -> str:
    """What Check the server says it found."""
    service = SERVICE_NAMES.get(api, api)
    if not models:
        return f"{service} answered, but has no models."
    names = [model.id for model in models]
    shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    return f"✓ {service} answered: {shown}."
