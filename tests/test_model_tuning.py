"""Models SealedLore adjusts for (engine/model_tuning.py): which models, what
the prompt leaves out for them, and the UI that says so."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sealedlore.engine.model_tuning import TUNINGS, bare, left_out, summary, tunings_for
from sealedlore.engine.prompt import SECTION_REMINDER, SECTION_ROUND
from tests.test_reactions import CLAUSE, make_session, play, turn

OPUS = "Anthropic::claude-opus-5-5"


@pytest.mark.parametrize(
    "ref",
    ["Anthropic::claude-opus-5-5", "anthropic/claude-opus-5.5", "claude-opus-5-5:thinking"],
)
def test_opus_5_5_is_recognised_however_it_is_named(ref: str) -> None:
    assert bare(ref) == "claude-opus-5-5"
    assert left_out(ref) == {"turn.round.remember"}


@pytest.mark.parametrize(
    "ref",
    ["claude-opus-5", "Anthropic::claude-sonnet-4-6", "z-ai/glm-5.3", "xiaomi/mimo-v2.6-pro", ""],
)
def test_no_other_model_loses_a_prompt_text(ref: str) -> None:
    assert left_out(ref) == frozenset()


def test_anthropic_only_shaping_is_left_off_another_endpoint() -> None:
    keys = lambda **kw: [t.key for t in tunings_for("anthropic/claude-opus-5.5", **kw)]  # noqa: E731
    assert "anthropic-thinking" in keys(anthropic_api=True)
    assert "anthropic-thinking" not in keys(anthropic_api=False)
    assert keys(anthropic_api=False)[0] == "opus-5-5-round-dialogue"  # prompt ones first


def test_private_and_tee_models_are_described() -> None:
    assert {t.key for t in tunings_for("private/glm-5-3")} == {
        "private-mode-families",
        "private-lowest-reasoning",
    }
    assert [t.key for t in tunings_for("TEE/deepseek-v3.2")] == ["private-lowest-reasoning"]
    assert summary("z-ai/glm-5.3") == ""
    assert "left out" in summary(OPUS)


def test_opus_5_5_gets_the_answers_without_the_dialogue_sentence(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "I pick the lock.", "It turns."])
    session.story.defaults.main_model = OPUS
    play(session, turn())
    play(session, turn("I hold the lamp."))
    prompt = session.last_prompt
    assert "[Maela Orr] I pick the lock." in prompt.section(SECTION_ROUND).text
    assert CLAUSE not in prompt.section("tail.directives").text
    assert CLAUSE not in prompt.section(SECTION_REMINDER).text


def test_another_model_keeps_it(tmp_path: Path) -> None:
    session = make_session(tmp_path, ["The door gives.", "I pick the lock.", "It turns."])
    session.story.defaults.main_model = "Anthropic::claude-sonnet-4-6"
    play(session, turn())
    play(session, turn("I hold the lamp."))
    assert CLAUSE in session.last_prompt.section(SECTION_REMINDER).text


# --- the UI ----------------------------------------------------------------------


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_model_field_says_when_a_model_is_tuned(app) -> None:
    from sealedlore.gui.model_picker import TUNED_LABEL, ModelField

    field = ModelField("z-ai/glm-5.3")
    assert not field.tuned.isVisibleTo(field)
    field.setText(OPUS)
    assert field.tuned.isVisibleTo(field) and field.tuned.text() == TUNED_LABEL
    assert "turn.round.remember" in field.tuned.toolTip()
    field.setText("")
    assert not field.tuned.isVisibleTo(field)


def test_help_lists_every_adjustment(app, tmp_path: Path, monkeypatch) -> None:
    from sealedlore.gui import model_tuning_dialog
    from sealedlore.gui.main_window import MainWindow

    window = MainWindow(root=tmp_path, use_mock=True)
    labels = [a.text().replace("&", "") for a in window.menuBar().actions()]
    help_menu = window.menuBar().actions()[labels.index("Help")].menu()
    action = next(a for a in help_menu.actions() if "Models SealedLore adjusts" in a.text())
    seen = []
    monkeypatch.setattr(
        model_tuning_dialog.ModelTuningDialog, "exec", lambda self: seen.append(self) or 0
    )
    action.trigger()
    assert len(seen[0].entries) == len(TUNINGS)
    assert "Why:" in seen[0].entries[0].text()
    window.close()
