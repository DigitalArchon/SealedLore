"""Models SealedLore recommends and adjusts for (engine/model_tuning.py): which models, what
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
    assert {t.key for t in tunings_for("private/glm-5-3", role="storyteller")} == {
        "private-mode-families",
        "private-lowest-reasoning",
    }
    assert [t.key for t in tunings_for("TEE/deepseek-v3.2")] == ["private-lowest-reasoning"]
    assert summary("z-ai/glm-5.3", role="scene_model") == ""
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
    from sealedlore.gui.model_picker import TUNED_LABEL, TUNED_PROMPTS_LABEL, ModelField

    field = ModelField("mistralai/mistral-medium-3.1")
    assert not field.tuned.isVisibleTo(field)
    field.setText(OPUS)
    assert field.tuned.isVisibleTo(field) and field.tuned.text() == TUNED_PROMPTS_LABEL
    # Chosen for another use, its prompt isn't SealedLore's to word: only how
    # its requests are shaped is said.
    side = ModelField(OPUS, role="scene_model")
    assert side.tuned.text() == TUNED_LABEL
    assert "turn.round.remember" not in side.tuned.toolTip()
    assert "turn.round.remember" in field.tuned.toolTip()
    field.setText("")
    assert not field.tuned.isVisibleTo(field)


def test_help_lists_every_adjustment(app, tmp_path: Path, monkeypatch) -> None:
    from sealedlore.gui import model_tuning_dialog
    from sealedlore.gui.main_window import MainWindow

    window = MainWindow(root=tmp_path, use_mock=True)
    labels = [a.text().replace("&", "") for a in window.menuBar().actions()]
    help_menu = window.menuBar().actions()[labels.index("Help")].menu()
    action = next(a for a in help_menu.actions() if "Models SealedLore recommends" in a.text())
    seen = []
    monkeypatch.setattr(
        model_tuning_dialog.ModelTuningDialog, "exec", lambda self: seen.append(self) or 0
    )
    action.trigger()
    assert len(seen[0].entries) == len(TUNINGS)
    assert "Why:" in seen[0].entries[0].text()
    window.close()


# --- a model's own wording ------------------------------------------------------


@pytest.fixture
def worded(monkeypatch):
    """A storyteller tuning that rewords one text, and a character one."""
    from sealedlore.engine import model_tuning
    from sealedlore.engine.model_tuning import ModelTuning

    extra = (
        ModelTuning(
            key="test-storyteller",
            models="Test model",
            kind="prompt",
            what="Its own REMEMBER voice line.",
            why="A test.",
            applies=lambda model: bare(model) == "story-model",
            role="storyteller",
            texts_replaced={"turn.remember.voice": "TUNED VOICE LINE."},
        ),
        ModelTuning(
            key="test-character",
            models="Test model",
            kind="prompt",
            what="Its own ask.",
            why="A test.",
            applies=lambda model: bare(model) == "maela-lora",
            role="character",
            texts_replaced={"characters.own.ask": "TUNED ASK, {name}."},
        ),
    )
    monkeypatch.setattr(model_tuning, "TUNINGS", (*model_tuning.TUNINGS, *extra))


def test_a_model_s_own_wording_goes_only_where_it_applies(worded) -> None:
    from sealedlore.engine.model_tuning import tuned_texts
    from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts

    told = tuned_texts(DEFAULT_TEXTS, "story-model", "storyteller")
    assert told["turn.remember.voice"] == "TUNED VOICE LINE."
    # Not in another use, nor for another model.
    assert tuned_texts(DEFAULT_TEXTS, "story-model", "character") is DEFAULT_TEXTS
    assert tuned_texts(DEFAULT_TEXTS, "z-ai/glm-5.3", "storyteller") is DEFAULT_TEXTS
    # The author's own edit of the text wins over the model's wording.
    mine = PromptTexts({"turn.remember.voice": "MY LINE."})
    assert tuned_texts(mine, "story-model", "storyteller")["turn.remember.voice"] == "MY LINE."


def test_the_storyteller_and_a_character_each_get_their_own(worded, tmp_path: Path) -> None:
    from tests.test_reactions import MAELA_MODEL, round_calls, sent

    session = make_session(tmp_path, ["The door gives.", "I pick the lock."])
    session.config.prose_reminders = True
    play(session, turn())
    assert "TUNED VOICE LINE." in session.last_prompt.section(SECTION_REMINDER).text
    [call] = round_calls(session)
    assert call.model == MAELA_MODEL
    asked = sent(call)
    assert "TUNED ASK, Maela Orr." in asked and "TUNED VOICE LINE." not in asked


def test_a_field_marks_a_model_recommended_for_its_use(app) -> None:
    from sealedlore.gui.model_picker import ModelField
    from sealedlore.models.config import RECOMMENDED_TEXT_MODELS

    choices = RECOMMENDED_TEXT_MODELS["story"]
    field = ModelField("", recommended=choices, role="storyteller")
    assert not field.recommended_mark.isVisibleTo(field)
    # However it is reached: NanoGPT's id or Anthropic's own.
    field.setText("Anthropic::claude-opus-5-5")
    assert field.recommended_mark.isVisibleTo(field)
    assert "premium" in field.recommended_mark.text()


def test_a_recommended_claude_goes_to_anthropic_s_own_api_when_there_is_one() -> None:
    from sealedlore.models.config import ProviderConfig, recommended_reference

    nano = ProviderConfig(name="NanoGPT", base_url="https://nano-gpt.com/api/v1")
    direct = ProviderConfig(name="Anthropic", base_url="https://api.anthropic.com")
    opus = "anthropic/claude-opus-5.5"
    assert recommended_reference(opus, [nano, direct], "NanoGPT") == "Anthropic::claude-opus-5-5"
    assert recommended_reference(opus, [nano], "NanoGPT") == opus
    # Anything else stays NanoGPT's.
    assert recommended_reference("z-ai/glm-5.3", [nano, direct], "Anthropic") == (
        "NanoGPT::z-ai/glm-5.3"
    )


@pytest.mark.parametrize(
    "ref", ["z-ai/glm-5.3", "TEE/glm-5.3", "private/glm-5-3", "glm-5.3:thinking"]
)
def test_glm_5_3_playing_a_character_is_told_to_go_by_the_story(ref: str) -> None:
    from sealedlore.engine.model_tuning import is_tuned, tuned_texts
    from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts

    told = tuned_texts(DEFAULT_TEXTS, ref, "character")
    for key in ("characters.own.ask", "characters.own.ask.after"):
        assert told[key].startswith(DEFAULT_TEXTS[key])  # the usual ask, then the sentence
        assert told[key].endswith(
            "if it doesn't show you saying or doing it, you didn't, and you say so."
        )
    assert "{player}" in told["characters.own.ask.after"]
    assert is_tuned(ref, "character") and not is_tuned(ref, "storyteller")
    # As the storyteller, and for any other model, the usual wording.
    assert tuned_texts(DEFAULT_TEXTS, ref, "storyteller") is DEFAULT_TEXTS
    for other in ("z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash", OPUS):
        assert tuned_texts(DEFAULT_TEXTS, other, "character") is DEFAULT_TEXTS
    # The author's own ask stands.
    mine = PromptTexts({"characters.own.ask": "MY ASK, {name}."})
    assert tuned_texts(mine, ref, "character")["characters.own.ask"] == "MY ASK, {name}."


def test_the_character_list_recommends_opus_on_anthropic_and_a_cheap_one() -> None:
    from sealedlore.models.config import RECOMMENDED_TEXT_MODELS

    first, *rest = RECOMMENDED_TEXT_MODELS["character"]
    assert bare(first.id) == "claude-opus-5-5" and first.tag == "recommended"
    assert any(choice.tag == "budget" for choice in rest)
