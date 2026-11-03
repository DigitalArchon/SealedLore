"""A story's picture style taken from a picture (the author, Nov 3 2026:
"base the style off the import"): Setup → About → Picture style → From a
picture…, and the same in the portraits dialog.

The picture is chosen (cropped first, as every picture from disk is), the
author is asked before it is sent, and a model that sees pictures (the
prompt writer's) describes its style in words (`describe_picture_style`).
The words become the story's own style in the field, to read and change
before anything is saved, and the picture is kept with them once saved
(`Story.style_picture`): sent with each new picture in that style as a
style reference (With its picture), which held a sketch's sepia where the
words alone added colour.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt, QThread
from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog, QWidget

from sealedlore.gui.crop_dialog import cropped_picture
from sealedlore.gui.ref_images import PICTURE_FILTER
from sealedlore.gui.worker import GenerationWorker
from sealedlore.storage.image_meta import picture_kind

FROM_PICTURE_LABEL = "From a picture…"
TITLE = "The style of a picture"
# Models that can see pictures, named when the prompt writer can't.
SEEING = "Claude Sonnet 4.6, Kimi K3 and GLM 5.3 Flash can, for instance"
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def describe_style(
    parent: QWidget | None, session, models: Mapping[str, object] | None = None
) -> tuple[str, bytes, str] | None:
    """The words for a picture's style, with the picture (its bytes, hidden
    data removed, and extension), or None (cancelled, refused or failed,
    said). `models`: the endpoint's listing, to know whether the prompt
    writer sees pictures before anything is sent."""
    if session is None:
        return None
    chosen, _ = QFileDialog.getOpenFileName(
        parent, "A picture whose style to take", str(Path.home()), PICTURE_FILTER
    )
    if not chosen:
        return None
    try:
        kept = cropped_picture(parent, Path(chosen))
    except ValueError as exc:
        QMessageBox.warning(parent, TITLE, str(exc))
        return None
    if kept is None:
        return None
    data, suffix = kept
    model = session.image_prompt_model
    info = (models or {}).get(model)
    if info is not None and getattr(info, "vision", None) is False:
        QMessageBox.information(
            parent,
            TITLE,
            f"{model} can't see pictures. Choose an image prompt writer that can, under "
            f"Settings → Models → Image prompt writer ({SEEING}).",
        )
        return None
    answer = QMessageBox.question(
        parent,
        TITLE,
        f"Send this picture to {model} to describe its style in words? It is sent as it is, "
        "its hidden data removed. The words become this story's picture style, for you to "
        "read and change before you save; the picture is kept with them, to go with each "
        "new picture as a style reference (With its picture).",
        QMessageBox.Yes | QMessageBox.Cancel,
        QMessageBox.Yes,
    )
    if answer != QMessageBox.Yes:
        return None
    mime = _MIME.get(suffix.lower()) or _MIME.get(picture_kind(data) or "", "image/png")
    result: dict[str, str] = {}

    def job():
        result["words"] = session.describe_picture_style(data, mime, model=model)
        return
        yield  # a generator, so GenerationWorker can run it

    worker = GenerationWorker(job, provider=session.provider)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.failed.connect(lambda message: result.__setitem__("error", message))
    worker.finished.connect(thread.quit)
    loop = QEventLoop()
    # Cleanup on the thread, never the worker's own finished (CONTRIBUTING.md).
    thread.finished.connect(loop.quit)
    progress = QProgressDialog(f"{model} is describing the picture's style…", "Stop", 0, 0, parent)
    progress.setWindowTitle(TITLE)
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.canceled.connect(worker.stop)
    progress.show()
    thread.start()
    loop.exec()
    progress.close()
    progress.deleteLater()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()
    if "error" in result:
        QMessageBox.warning(parent, TITLE, f"The style couldn't be described: {result['error']}")
        return None
    words = result.get("words")
    return (words, data, suffix) if words else None
