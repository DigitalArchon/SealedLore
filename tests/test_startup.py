"""What a launch loads and what an open story holds.

Measured on the author's stories: numpy was ~45 ms and ~13 MB of every launch
for one function, the tokenizer's warm-up took ~0.2s from the window's first
paint, and the cost in the status bar kept every prompt of the story's log in
memory (~190 MB for a 54 MB log).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from sealedlore.engine.retrieval import cosine_scores
from sealedlore.engine.usage import usage_fields, usage_report
from sealedlore.storage.atomic import append_jsonl, read_jsonl
from sealedlore.storage.repository import (
    StoryBundle,
    append_api_log,
    read_api_log,
    read_api_log_since,
    save_story_bundle,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sealedlore"
# Loaded by the one function that needs them, never by a module as it is imported.
ON_FIRST_USE = ("numpy", "tiktoken", "tinfoil", "keyring")
# Modules of a package, likewise: QtMultimedia loads FFmpeg (~20 MB of
# libraries) and is needed only once a video is shown or played.
MODULES_ON_FIRST_USE = ("PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets")


def imported_at_the_top(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_heavy_packages_are_imported_on_first_use():
    offenders = {
        str(path.relative_to(SOURCE_ROOT)): bad
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if (
            bad := {
                name
                for name in imported_at_the_top(path)
                if name.split(".")[0] in ON_FIRST_USE or name in MODULES_ON_FIRST_USE
            }
        )
    }
    assert offenders == {}


def test_similarity_still_scores():
    scores = cosine_scores([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0], [0.0, 0.0]])
    assert scores == pytest.approx([1.0, 0.0, 0.0])


# --- the log, as the cost in the status bar keeps it ------------------------------------

PROMPT = "Nobody but the storyteller is sent this."

LOG = [
    {
        "id": "r1",
        "kind": "request",
        "payload": {"model": "m", "messages": [{"role": "user", "content": PROMPT}]},
    },
    {"id": "x1", "kind": "response", "request_log_ref": "r1", "usage": {"cost": 0.25}},
    {"id": "r2", "kind": "request", "aside": PROMPT, "payload": {"model": "m"}},
    {
        "id": "x2",
        "kind": "response",
        "aside": PROMPT,
        "request_log_ref": "r2",
        "usage": {"prompt_tokens": 40, "completion_tokens": 9},
        "content": PROMPT,
    },
    {"id": "p1", "kind": "private_request", "payload": {"model": "TEE/m"}},
    {"kind": "private_response", "request_log_ref": "p1", "usage": {"cost": 0.5}},
    "not an entry at all",
]


def test_the_cost_is_the_same_from_what_is_kept():
    kept = [usage_fields(entry) for entry in LOG]
    assert usage_report(kept) == usage_report([e for e in LOG if isinstance(e, dict)])
    assert usage_report(kept).total.calls == 3
    assert [line.label for line in usage_report(kept).lines] == [
        "Story turns",
        "Questions (asides)",
        "Private scenes",
    ]


def test_nothing_of_a_prompt_is_kept():
    assert PROMPT not in repr([usage_fields(entry) for entry in LOG])


def test_the_log_is_cut_down_as_it_is_read(tmp_path: Path, story):
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    for entry in LOG:
        append_api_log(story.id, entry, root=tmp_path)
    entries, _start, end = read_api_log_since(story.id, 0, root=tmp_path, keep=usage_fields)
    assert entries == [usage_fields(entry) for entry in LOG]
    assert PROMPT not in repr(entries)
    log = tmp_path / "stories" / story.id / "api_log.jsonl"
    assert end == log.stat().st_size
    # Asked for nothing, it is every entry as written.
    assert read_api_log_since(story.id, 0, root=tmp_path)[0] == LOG


def test_a_record_ends_at_its_newline_and_nowhere_else(tmp_path: Path, story):
    """JSON leaves U+2028, U+2029 and U+0085 as they are inside a string, and
    `str.splitlines` breaks at each: the record was cut in pieces, none of
    which parsed, and its cost was lost without a word."""
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    text = "one two three\u0085four"
    entry = {"id": "x1", "kind": "response", "content": text, "usage": {"cost": 0.25}}
    append_api_log(story.id, entry, root=tmp_path)
    append_api_log(story.id, {"id": "x2"}, root=tmp_path)
    assert read_api_log(story.id, root=tmp_path) == [entry, {"id": "x2"}]
    assert read_api_log_since(story.id, 0, root=tmp_path)[0] == [entry, {"id": "x2"}]

    other = tmp_path / "other.jsonl"
    append_jsonl(other, entry)
    other.write_bytes(other.read_bytes() + b"\n{broken\n")  # a blank and a bad line
    assert read_jsonl(other) == [entry]


# --- the tokenizer, once the window is up -----------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_the_tokenizer_is_warmed_once_the_window_is_up(tmp_path: Path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui import main_window
    from sealedlore.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    started: list[str] = []
    monkeypatch.setattr(MainWindow, "_warm_tokenizer", lambda self: started.append("warm"))
    monkeypatch.setattr(main_window, "WARM_UP_DELAY_MS", 0)
    window = MainWindow(root=tmp_path, use_mock=True)
    try:
        app.processEvents()
        assert started == []  # building the window starts nothing
        window.show()
        QTest.qWait(50)
        assert started == ["warm"]
        window.hide()
        window.show()
        QTest.qWait(50)
        assert started == ["warm"]  # shown again, not warmed again
    finally:
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
