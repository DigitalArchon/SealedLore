"""What the review of 3.5.0b1 found in files from strangers, the window's
side (Nov 22 2026): each test fails on 0f289da. Every file is made here."""

from __future__ import annotations

import json
import os
import random
import re
import time
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.plot_md import parse_plot_markdown  # noqa: E402
from sealedlore.gui import markdown_text, window_collection  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.transcript import TextBody  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.archive import archive_data  # noqa: E402
from sealedlore.storage.scenario import bundle_from_scenario  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    yield window
    window.close()


def boxes(monkeypatch) -> list[str]:
    """Every message box answered Ok, its words kept."""
    said: list[str] = []

    def exec_(box) -> int:
        said.append(f"{box.windowTitle()}: {box.text()} {box.informativeText()}")
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return said


# --- F2: a full disk while a backup of characters is put back -----------------------


def test_a_log_that_cant_be_written_is_said_and_the_characters_are_back(
    app, window, tmp_path: Path, monkeypatch
):
    marcus = collection.kept("Marcus")
    path = tmp_path / "theirs.sealedlore-characters.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json", json.dumps({"format": "sealedlore-characters", "version": 1})
        )
        archive.writestr("characters.json", json.dumps([marcus.model_dump(mode="json")]))
        archive.writestr("api_log.jsonl", json.dumps({"id": "e1", "kind": "image_response"}) + "\n")

    def full(*args, **kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(window_collection, "append_api_log", full)
    said = boxes(monkeypatch)
    window.restore_collection(path)
    assert [c.name for c in window._collection_session().cast] == ["Marcus"]
    assert len(said) == 1 and "No space left" in said[0] and "put back" in said[0]


# --- F3: a story opens and shows its messages whatever numbers its backup holds -----

SAMPLE = Path(__file__).parent.parent / "SampleStories" / "Doomsville.md"


def _story_backup(tmp_path: Path, change) -> Path:
    """Doomsville three exchanges in, as a story backup, with `change` made
    to what the file holds."""
    parsed = parse_plot_markdown(SAMPLE.read_text(encoding="utf-8"), fallback_title="Doomsville")
    bundle = bundle_from_scenario(parsed.scenario)
    held = bundle.story.setup.suggested_character_id or bundle.cast[0].id
    bundle.nodes = make_exchange(3, speaker_id=held)
    bundle.story.active_leaf_id = bundle.nodes[-1].id
    bundle.story.held_character_id = held
    data = json.loads(json.dumps(archive_data(bundle, [], {})))
    change(data)
    path = tmp_path / "theirs.sealedlore-archive.json"
    # As Python writes them, NaN and Infinity are bare words: what a
    # stranger's file may hold, and the reader takes.
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _usage(**usage):
    def change(data: dict) -> None:
        data["api_log"] = [{"id": "l1", "kind": "response", "usage": usage}]

    return change


def _event_window(data: dict) -> None:
    data["story"]["plot"]["events"][0]["window"] = [30, 10]


def _budget(data: dict) -> None:
    data["story"]["defaults"]["context_token_budget"] = 10**400


@pytest.mark.parametrize(
    "change",
    [
        _usage(prompt_tokens=float("nan")),
        _usage(prompt_tokens=float("inf")),
        _usage(prompt_tokens=5, cost=10**400),
        _usage(prompt_tokens=10**400, completion_tokens=3),
        _usage(prompt_tokens=5, cost=float("nan")),
        _event_window,
        _budget,
    ],
    ids=[
        "nan tokens",
        "infinite tokens",
        "huge cost",
        "huge tokens",
        "nan cost",
        "window",
        "budget",
    ],
)
def test_a_story_opens_whatever_numbers_its_backup_holds(
    app, window, tmp_path: Path, monkeypatch, change
):
    said = boxes(monkeypatch)
    window.resize(1600, 900)
    window.show()
    window._import_archive(_story_backup(tmp_path, change))
    app.processEvents()
    assert window.session is not None and not said
    assert len(window.transcript.searchable_texts) == 6
    assert "nan" not in window.status_strip.cost_label.text().lower()
    # And again from its folder, as at every later start.
    story_id = window.session.story.id
    window._close_story()
    window.open_story(story_id)
    assert len(window.transcript.searchable_texts) == 6 and not said


def test_one_part_that_cant_be_shown_is_said_and_the_rest_is_shown(
    app, window, tmp_path: Path, monkeypatch
):
    said = boxes(monkeypatch)
    window.resize(1600, 900)
    window.show()

    def broken() -> None:
        raise OverflowError("int too large to convert to float")

    monkeypatch.setattr(window, "refresh_summaries", broken)
    window._import_archive(_story_backup(tmp_path, lambda data: None))
    app.processEvents()
    assert len(window.transcript.searchable_texts) == 6, "the story is there to read"
    assert window.composer.isEnabled()
    assert len(said) == 1 and "int too large" in said[0] and "Story so far" in said[0]


def test_a_lore_entrys_priority_past_the_box_still_shows_the_entry(
    app, window, tmp_path: Path, monkeypatch
):
    def priority(data: dict) -> None:
        data["lore"][0]["priority"] = 10**18

    said = boxes(monkeypatch)
    window._import_archive(_story_backup(tmp_path, priority))
    panel = window.lore_panel
    panel.list.setCurrentRow(0)
    app.processEvents()
    assert panel.title.text() and panel.priority.value() == panel.priority.maximum()
    assert not panel._loading and not said
    # The next entry's own priority is shown, not left from this one.
    panel.list.setCurrentRow(1)
    assert panel.priority.value() == 0


def test_a_spin_box_takes_a_number_past_what_qt_holds(app):
    from PySide6.QtWidgets import QSpinBox

    from sealedlore.gui.fields import set_spin_value

    box = QSpinBox()
    box.setRange(1, 100_000)
    set_spin_value(box, 10**18)
    assert box.value() == 100_000
    set_spin_value(box, -(10**400))
    assert box.value() == 1
    set_spin_value(box, 7)
    assert box.value() == 7


# --- F4: a message Markdown can't draw in good time is shown plain ------------------

_TABLE = "|" + "a|" * 100 + "\n|" + "-|" * 100 + "\n" + ("|" + "b|" * 100 + "\n") * 100
HEAVY = {
    "a table of ten thousand cells": _TABLE,
    "a table of twenty thousand rows": "**x**\n\na|b\n-|-\n" + "c|d\n" * 20_000,
    "lists nested three thousand deep": "- " * 3_000 + "a",
    "quoted lists nested deep": "> 1. " * 2_000 + "a",
    "twelve thousand list items": "- a\n" * 12_000,
    "sixty thousand brackets": "[" * 60_000 + " http://example.invalid",
    "pictures that never close": "**x** " + "![](" * 12_000,
    "pictures with titles that never close": "**x** " + '![](a "' * 9_000,
    "links that never close": "**x** " + "[a](b" * 12_000,
    "a megabyte of marks": "**b** *i* `c`\n" * 80_000,
}


@pytest.mark.parametrize("text", HEAVY.values(), ids=HEAVY.keys())
def test_a_message_markdown_would_take_long_over_is_drawn_at_once(app, text):
    markdown_text.forget()
    began = time.perf_counter()
    label = TextBody()
    label.setWordWrap(True)
    label.set_markdown(text)
    label.heightForWidth(900)
    shown = markdown_text.shown_text(text)
    assert time.perf_counter() - began < 1.0
    assert label.source == text
    if label.textFormat() == Qt.PlainText:
        assert shown == text, "Find reads what the label holds"


def test_past_its_limits_a_message_is_shown_as_written(app):
    for text in (_TABLE, "- " * 3_000 + "a", "- a\n" * 12_000, "**b**\n" * 60_000):
        assert markdown_text.has_markdown(text) and markdown_text.markdown_html(text) is None


def test_what_a_model_really_writes_keeps_its_markdown(app):
    table = "| Name | Role | Where |\n|---|---|---|\n" + "| Elena | medic | Hellsville |\n" * 60
    nested = "\n".join("  " * (i % 6) + f"- point {i}" for i in range(400))
    numbered = "\n".join(f"{i}. step {i} of the **plan**" for i in range(1, 500))
    long = (
        "A paragraph of a long reply, with **bold** and a [link](https://example.invalid).\n\n"
    ) * 900
    quoted = "> " * 12 + "a quote of a quote, twelve deep"
    for text in (table, nested, numbered, long, quoted):
        assert markdown_text.markdown_html(text, keep=False) is not None, text[:40]
    assert "<table" in markdown_text.markdown_html(table, keep=False)


_OLD_LINK = re.compile(r"\[[^\]\n]+\]\([^)\n]+\)")
_OLD_BARE = re.compile(r"!\[\s*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _old_pictures_named(line: str) -> str:
    if "![" not in line:
        return line
    segments = line.split("`")
    for i in range(0, len(segments), 2):
        segments[i] = _OLD_BARE.sub(lambda m: f"![({m.group(1)})]({m.group(1)})", segments[i])
    return "`".join(segments)


def test_links_and_bare_pictures_are_found_as_the_patterns_before_found_them():
    rng = random.Random(11)
    pieces = ["[", "]", "(", ")", "](", "![", "![](", "a", "b c", " ", "\n", '"', ' "t"', "!", "`"]
    links = named = 0
    for _ in range(60_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        old = _OLD_LINK.search(text) is not None
        assert markdown_text._has_link(text) == old, repr(text)
        links += old
        for line in text.split("\n"):
            old_line = _old_pictures_named(line)
            assert markdown_text._pictures_named(line) == old_line, repr(line)
            named += old_line != line
    assert links > 500 and named > 500
