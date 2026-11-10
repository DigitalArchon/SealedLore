"""Prompt sets: the edited prompt texts in a file of their own, exported from
the prompt editor and imported into a story or into every story."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.prompt_edits import default_sha, set_text  # noqa: E402
from sealedlore.engine.prompt_texts import TEXTS  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.prompt_editor import PromptEditorDialog  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.prompt_edit import PromptEdit  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage.prompt_sets import (  # noqa: E402
    PROMPT_SET_SUFFIX,
    PromptSetError,
    read_prompt_set,
    write_prompt_set,
)
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle  # noqa: E402

RULES = "storyteller.rules"
QUESTION = "question.rules"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def scope(dialog: PromptEditorDialog, name: str) -> None:
    dialog.scope.setCurrentIndex(dialog.scope.findData(name))


# --- the file ---------------------------------------------------------------------


def test_a_set_round_trips_with_its_fingerprints(tmp_path: Path):
    edits: dict[str, PromptEdit] = {}
    set_text(edits, RULES, "Mine.")
    edits["old.text"] = PromptEdit(text="From a newer version.", default_sha="abc")
    path = tmp_path / f"mine{PROMPT_SET_SUFFIX}"

    write_prompt_set(path, edits)
    data = json.loads(path.read_text(encoding="utf-8"))
    loaded = read_prompt_set(path)

    assert data["format"] == "sealedlore-prompts" and data["version"] == 1
    assert loaded.edits == edits
    assert loaded.edits[RULES].default_sha == default_sha(RULES)


@pytest.mark.parametrize(
    ("content", "said"),
    [
        ("not json", "isn't valid JSON"),
        (json.dumps({"format": "sealedlore-scenario"}), "isn't a SealedLore prompt set"),
        (json.dumps({"format": "sealedlore-prompts", "version": 9}), "reads up to version 1"),
        (
            json.dumps({"format": "sealedlore-prompts", "version": 1, "edits": {"a": 3}}),
            "invalid content",
        ),
    ],
)
def test_a_file_that_isnt_a_set_is_said_plainly(tmp_path: Path, content: str, said: str):
    path = tmp_path / "x.json"
    path.write_text(content)
    with pytest.raises(PromptSetError, match=said):
        read_prompt_set(path)


# --- the editor -------------------------------------------------------------------


def test_export_saves_what_is_in_force_for_the_story(app, tmp_path: Path, monkeypatch):
    config, story = Config(), Story(title="Tale")
    set_text(config.prompt_edits, QUESTION, "Answer plainly.")
    set_text(story.prompt_edits, RULES, "Story rules.")
    dialog = PromptEditorDialog(config, story)
    target = tmp_path / f"out{PROMPT_SET_SUFFIX}"
    monkeypatch.setattr(
        "sealedlore.gui.prompt_editor.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(target), ""),
    )

    scope(dialog, "story")
    dialog.export_button.click()
    assert set(read_prompt_set(target).edits) == {RULES, QUESTION}

    scope(dialog, "app")
    dialog.export_button.click()
    assert set(read_prompt_set(target).edits) == {QUESTION}
    dialog.deleteLater()


def test_import_adds_to_or_replaces_the_edits_here_until_saved(app, tmp_path: Path):
    config, story = Config(), Story(title="Tale")
    set_text(story.prompt_edits, QUESTION, "Ours.")
    incoming: dict[str, PromptEdit] = {}
    set_text(incoming, RULES, "Theirs.")
    path = tmp_path / f"theirs{PROMPT_SET_SUFFIX}"
    write_prompt_set(path, incoming)

    dialog = PromptEditorDialog(config, story)
    scope(dialog, "story")
    assert dialog.import_set(path, how="add")
    assert set(dialog.edits["story"]) == {RULES, QUESTION}
    assert dialog.import_set(path, how="replace")
    assert set(dialog.edits["story"]) == {RULES}
    # Nothing is kept until Save.
    assert set(story.prompt_edits) == {QUESTION}
    dialog.apply_to(config, story)
    assert story.prompt_edits[RULES].text == "Theirs."
    dialog.deleteLater()


def test_a_set_from_another_version_says_what_it_has(app, tmp_path: Path):
    edits = {
        RULES: PromptEdit(text="Old rules.", default_sha="0000"),
        "a.text.gone": PromptEdit(text="Gone.", default_sha="1111"),
    }
    path = tmp_path / f"old{PROMPT_SET_SUFFIX}"
    write_prompt_set(path, edits)

    notes = " ".join(PromptEditorDialog.import_notes(path, edits))
    assert "edits 2 texts" in notes
    assert "1 of them are texts this version doesn't have" in notes
    assert "default has changed since 1 of them" in notes

    dialog = PromptEditorDialog(Config(), None)
    assert dialog.import_set(path, how="add")
    assert "a.text.gone" in dialog.counts.text()
    dialog.deleteLater()


def test_a_broken_file_is_refused_with_its_reason(app, tmp_path: Path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_text("{}")
    said: list[str] = []
    monkeypatch.setattr(
        "sealedlore.gui.prompt_editor.QMessageBox.warning",
        lambda parent, title, text: said.append(text),
    )
    dialog = PromptEditorDialog(Config(), None)
    assert not dialog.import_set(path, how="add")
    assert said and "isn't a SealedLore prompt set" in said[0]
    assert dialog.edits["app"] == {}
    dialog.deleteLater()


# --- File → Import… ----------------------------------------------------------------


def test_file_import_takes_a_set_into_the_editor(app, tmp_path: Path, story, monkeypatch):
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    incoming: dict[str, PromptEdit] = {}
    set_text(incoming, QUESTION, "Answer plainly.")
    path = tmp_path / f"theirs{PROMPT_SET_SUFFIX}"
    write_prompt_set(path, incoming)
    monkeypatch.setattr(
        "sealedlore.gui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(path), ""),
    )
    monkeypatch.setattr(PromptEditorDialog, "_ask_how", lambda self, path, edits: "add")
    monkeypatch.setattr(PromptEditorDialog, "exec", lambda self: PromptEditorDialog.Accepted)
    stories_before = len(list((tmp_path / "stories").iterdir()))

    window.import_file()

    assert load_config(root=tmp_path).prompt_edits[QUESTION].text == "Answer plainly."
    assert len(list((tmp_path / "stories").iterdir())) == stories_before
    assert TEXTS[QUESTION].default != "Answer plainly."
    window.close()
