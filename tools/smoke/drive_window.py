"""Drive the real SealedLore window on the display, against the fake servers.

usage: drive_window.py DATA_DIR STORY_ID OUT_DIR
Takes screenshots of the screen into OUT_DIR and prints what it checked.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sealedlore.gui.app import build_app
from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.private_dialogs import PrivateSummaryDialog

# Run from the repo root with the venv active, on the real display:
#   DISPLAY=:0 python tools/smoke/drive_window.py DATA_DIR STORY_ID OUT_DIR

ROOT, STORY, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
OUT.mkdir(parents=True, exist_ok=True)
MARKER = "PRIVATE-ZQX"
checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_idle(timeout: float = 90) -> None:
    end = time.monotonic() + timeout
    while window._busy:
        if time.monotonic() > end:
            raise AssertionError("the window stayed busy")
        app.processEvents()
        modal = QApplication.activeModalWidget()
        if isinstance(modal, QMessageBox):
            shot("unexpected-box")
            raise AssertionError(f"unexpected message box: {modal.windowTitle()}: {modal.text()}")
        time.sleep(0.01)
    pump(0.3)


def shot(name: str) -> None:
    pump(0.5)
    QApplication.primaryScreen().grabWindow(0).save(str(OUT / f"{name}.png"))
    print(f"shot {name}", flush=True)


def until_modal(kind, timeout: float = 60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        modal = QApplication.activeModalWidget()
        if isinstance(modal, kind):
            return modal
        if isinstance(modal, QMessageBox):
            shot("unexpected-box")
            raise AssertionError(
                "unexpected message box: " + modal.windowTitle() + ": " + modal.text()
            )
        time.sleep(0.01)
    raise AssertionError(f"no {kind.__name__} appeared")


def send(text: str) -> None:
    window.composer.input.setPlainText(text)
    window.send_turn()
    wait_idle()


app = build_app(["drive"])


folder = ROOT / "stories" / STORY


def on_disk() -> str:
    log = folder / "api_log.jsonl"
    return "".join(p.read_text() for p in folder.glob("*.json")) + (
        log.read_text() if log.exists() else ""
    )


window = MainWindow(root=ROOT)
window.show()
pump(1.0)
window.open_story(STORY)
wait_idle()
shot("01-opened")
check("story opened", window.session is not None and window.session.story.title == "Doomsville")

# Hold John (the cast's first), then a public turn.
held = next(c.id for c in window.session.cast if c.name == "John")
window.composer.held.setCurrentIndex(window.composer.held.findData(held))
pump(0.2)
send("I push through the saloon doors and look for the sheriff.")
shot("02-first-turn")
public_leaf = window.session.story.active_leaf_id
check("public turn got a reply", window.session.full_path()[-1].kind == "assistant")
check("scene panel enabled outside a scene", window.scene_panel.isEnabled())

# Into a memory-only private scene.
window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("memory"))
# An earlier run's approved summary is public and holds the marker by design,
# and a public turn's prompts quote it: the check is that the open scene adds
# none (a reused story used to fail it).
MARKERS_BEFORE = on_disk().count(MARKER)
window.config.private_intro_seen = True  # the note is a modal dialog
window.composer.private_toggle.click()
pump(0.5)
check("private scene open", window.session.in_private)
check("scene panel frozen", not window.scene_panel.isEnabled())
check("plot panel frozen", not window.plot_panel.isEnabled())
check("held combo frozen", not window.composer.held.isEnabled())
check("stories list frozen", not window.stories.isEnabled())
shot("03-private-open")
send("Quietly, I tell her what I saw at the mine.")
shot("04-private-turn")
leaf = window.session.full_path()[-1]
check("private reply came from the private server", MARKER in leaf.content)
check("private reply is marked", leaf.meta.private_span is not None)

# Leaving the story mid-scene asks first; Cancel keeps the scene.


def cancel_question() -> None:
    modal = QApplication.activeModalWidget()
    if isinstance(modal, QMessageBox):
        shot("05-leave-asks")
        modal.reject()
    else:
        QTimer.singleShot(50, cancel_question)


QTimer.singleShot(200, cancel_question)
window.new_story()
pump(0.5)
check("new story mid-scene asked and kept the scene", window.session.in_private)

# Nothing on disk yet holds the scene's marker (memory only).
check("marker not on disk while the scene is open", on_disk().count(MARKER) == MARKERS_BEFORE)

# End the scene: approve the summary. The dialog's exec() nests inside
# whatever processEvents() call opened it, so it is driven from a timer.
summary_seen: list[str] = []


def approve_summary() -> None:
    modal = QApplication.activeModalWidget()
    if isinstance(modal, PrivateSummaryDialog):
        shot("06-summary-dialog")
        summary_seen.append(modal.editor.toPlainText())
        modal.approve.click()
    elif isinstance(modal, QMessageBox):
        shot("unexpected-box")
        summary_seen.append("ERROR " + modal.text())
        modal.reject()
    else:
        QTimer.singleShot(50, approve_summary)


QTimer.singleShot(200, approve_summary)
window.config.private_intro_seen = True  # the note is a modal dialog
window.composer.private_toggle.click()
wait_idle()  # the summary job; the timer approves the dialog it opens
pump(0.5)
wait_idle()  # the close job the approval started
check("summary came from the private server", bool(summary_seen) and MARKER in summary_seen[0])
shot("07-after-scene")
check("scene closed", not window.session.in_private)
check("scene panel back", window.scene_panel.isEnabled())
leaf = window.session.full_path()[-1]
check("summary joined the story", leaf.meta.private_summary_of is not None)
disk = "".join(p.read_text() for p in folder.glob("*.json"))
check(
    "scene messages not on disk after approval",
    MARKER + ")" not in disk and "PRIVATE-ZQX)" not in disk,
)
log = [
    json.loads(line) for line in (folder / "api_log.jsonl").read_text().splitlines() if line.strip()
]
kinds = [e["kind"] for e in log]
check(
    "private calls logged content-free",
    "private_request" in kinds
    and all(
        "messages" not in json.dumps(e.get("payload", {}))
        for e in log
        if e["kind"] == "private_request"
    ),
)
check(
    "no private content in any log entry",
    not any("Quietly, I tell her" in json.dumps(e) for e in log),
)
saved = json.loads((folder / "story.json").read_text())
node_ids = {n["id"] for n in json.loads((folder / "nodes.json").read_text())}
check("saved leaf exists", saved["active_leaf_id"] in node_ids)

# One more public turn after the scene.
send("I nod to the barkeep and head for the door.")
shot("08-public-after")
check("public turn after the scene got a reply", window.session.full_path()[-1].kind == "assistant")

# About box.
QTimer.singleShot(600, lambda: (shot("09-about"), QApplication.activeModalWidget().accept()))
window._show_about()
pump(0.3)

window.close()
pump(0.5)
failed = [name for name, ok in checks if not ok]
print(
    f"\n{len(checks) - len(failed)}/{len(checks)} checks passed"
    + (f"; failed: {failed}" if failed else "")
)
sys.exit(1 if failed else 0)
