"""Lore from the full entry goes to every storyteller but Claude
(engine/model_tuning.py, engine/session_lore_condense.py): measured blind,
GLM 5.3 wrote clearly better with the parts of a condensed always-on entry a
turn is about, Claude Sonnet 4.6 and Opus 5.5 no better, at about a third
more a turn on Sonnet. The storyteller's model in force decides: the story's
own, a private scene's, a Question's. The mock provider: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine import lore_whole_back
from sealedlore.engine.model_tuning import (
    TUNINGS,
    described,
    is_tuned,
    sends_lore_from_full,
    sent_differently,
    summary,
    tunings_for,
)
from sealedlore.engine.retrieval import GAP
from sealedlore.providers.mock import MockChatProvider
from tests.test_lore_whole_back import (
    GLM,
    GUILD,
    HESK_TURN,
    TITLE,
    guild,
    harbour,
    keep,
    sent,
    session_with,
    system_block,
    tail,
    turn,
    with_embeddings,
)

CLAUDES = [
    "Anthropic::claude-sonnet-4-6",
    "Anthropic::claude-opus-5-5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5.5",
    "NanoGPT::anthropic/claude-sonnet-4.5",
    "OpenRouter::anthropic/claude-3.5-haiku",
    "claude-sonnet-4-6:thinking",
    "Claude-Haiku-5-5",
]
OTHERS = [GLM, "z-ai/glm-5.3:thinking", "deepseek/deepseek-v4.1-flash", "acme/storyteller-9"]
LINE = "Lore from the full entry is not sent"


@pytest.mark.parametrize("model", CLAUDES)
def test_no_claude_storyteller_is_sent_lore_from_the_full_entry(model: str) -> None:
    assert not sends_lore_from_full(model)
    assert not sends_lore_from_full(model, "storyteller")
    # Playing a character it is another use, which this doesn't touch.
    assert sends_lore_from_full(model, "character")


@pytest.mark.parametrize("model", [*OTHERS, "private/glm-5-3", "TEE/deepseek-v3.2", ""])
def test_every_other_storyteller_is(model: str) -> None:
    assert sends_lore_from_full(model)


def test_it_is_a_lore_adjustment_of_the_storyteller_with_its_measurement() -> None:
    (tuning,) = [t for t in TUNINGS if t.lore_from_full_left_out]
    assert tuning.kind == "lore" and tuning.role == "storyteller"
    assert tuning.what.startswith(LINE)
    for words in ("blind", "Sonnet 4.6", "Opus 5.5", "a third more a turn", "GLM 5.3"):
        assert words in tuning.why
    # Listed with the prompt ones first, then lore, then how requests are shaped.
    kinds = [t.kind for t in tunings_for("anthropic/claude-opus-5.5", anthropic_api=True)]
    assert kinds == sorted(kinds, key=["prompt", "lore", "request"].index)
    assert "lore" in kinds
    # Not a wording of the prompt ("· tuned prompts"), but what it is sent differs.
    assert not is_tuned("Anthropic::claude-sonnet-4-6", "storyteller")
    assert sent_differently("Anthropic::claude-sonnet-4-6", "storyteller")
    assert not sent_differently("Anthropic::claude-sonnet-4-6", "scene_model")
    assert not sent_differently(GLM, "storyteller")
    # A field's note says it for the storyteller, not for a side call's model.
    assert LINE in summary("Anthropic::claude-sonnet-4-6", role="storyteller")
    assert LINE not in summary("Anthropic::claude-sonnet-4-6", role="scene_model")
    assert LINE not in summary(GLM, role="storyteller")


def test_help_lists_it_under_lore(app) -> None:
    from sealedlore.gui.model_tuning_dialog import INTRO, KINDS, ModelTuningDialog

    rows = [row for row in described() if row[1] == "lore"]
    assert rows == [
        (
            "Claude models, on any endpoint, as the storyteller",
            "lore",
            rows[0][2],
            rows[0][3],
        )
    ]
    assert rows[0][2].startswith(LINE)
    assert KINDS["lore"] == "Lore" and "less of the lore" in INTRO
    dialog = ModelTuningDialog()
    texts = [label.text() for label in dialog.entries]
    assert any(text.startswith(LINE) and "Why: Measured" in text for text in texts)
    dialog.deleteLater()


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_story_model_field_says_it(app) -> None:
    from sealedlore.gui.model_picker import TUNED_LABEL, ModelField

    field = ModelField("Anthropic::claude-sonnet-4-6", role="storyteller")
    assert field.tuned.isVisibleTo(field) and field.tuned.text() == TUNED_LABEL
    assert LINE in field.tuned.toolTip()
    side = ModelField("Anthropic::claude-sonnet-4-6", role="scene_model")
    assert LINE not in side.tuned.toolTip()
    field.setText(GLM)
    assert LINE not in field.tuned.toolTip()


# --- in play ---------------------------------------------------------------------------


def lore():
    return [harbour(), keep(), guild()]


@pytest.mark.parametrize("model", CLAUDES[:4] + CLAUDES[6:7])
def test_a_claude_storyteller_gets_no_parts_and_the_rest_as_before(
    tmp_path, story, cast, model
) -> None:
    glm = session_with(tmp_path / "glm", story.model_copy(deep=True), cast, lore())
    list(glm.send(turn(HESK_TURN)))
    assert glm.last_retrieval.in_full
    claude = session_with(tmp_path / "claude", story.model_copy(deep=True), cast, lore())
    claude.story.defaults.main_model = model
    list(claude.send(turn(HESK_TURN)))
    report = claude.last_retrieval
    assert report.in_full == ()
    assert "(from the full entry)" not in sent(claude)
    # The rest as before: the same lore chosen, the same condensed versions
    # in the same system block.
    assert [i.entry.title for i in report.injected] == [
        i.entry.title for i in glm.last_retrieval.injected if i.reason != "in_full"
    ]

    def condensed(session):
        titles = {entry.id: entry.title for entry in session.bundle.lore}
        return [titles[entry_id] for entry_id in session.last_retrieval.condensed]

    assert condensed(claude) == condensed(glm) == ["The Harbour", "The Guild"]
    assert system_block(claude) == system_block(glm)
    assert "The Guild, in short." in system_block(claude)


def test_an_untested_storyteller_gets_them(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore(), model="acme/storyteller-9")
    list(session.send(turn(HESK_TURN)))
    assert [i.name for i in session.last_retrieval.in_full] == ["The Guild"]
    assert TITLE in tail(session)


def test_regenerate_reproduces_it_and_a_new_model_changes_it_from_the_next_turn(
    tmp_path, story, cast
) -> None:
    session = session_with(tmp_path, story, cast, lore())
    with_embeddings(session)
    list(session.send(turn(HESK_TURN)))
    first = [(i.entry.id, i.matches) for i in session.last_retrieval.in_full]
    assert first
    list(session.regenerate())
    assert [(i.entry.id, i.matches) for i in session.last_retrieval.in_full] == first
    # The story's own model changed to Claude: none from the next turn on, a
    # retake of it included.
    session.story.defaults.main_model = "Anthropic::claude-sonnet-4-6"
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == ()
    list(session.regenerate())
    assert session.last_retrieval.in_full == ()
    assert "(from the full entry)" not in sent(session)
    # And back.
    session.story.defaults.main_model = GLM
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full


def test_a_private_scene_on_a_claude_model_gets_none(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore())  # the story's own: GLM 5.3
    private = MockChatProvider(["In private."] * 3)
    session.enter_private(keep="memory", provider=private, model="Anthropic::claude-sonnet-4-6")
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == ()
    text = "\n".join(part.text for m in private.requests[-1].messages for part in m.parts)
    assert "(from the full entry)" not in text
    assert "The Guild, in short." in text


def test_a_private_scene_on_another_model_gets_them_under_a_claude_story(
    tmp_path, story, cast
) -> None:
    session = session_with(tmp_path, story, cast, lore(), model="anthropic/claude-sonnet-4.5")
    private = MockChatProvider(["In private."] * 3)
    session.enter_private(keep="memory", provider=private, model="local/model")
    list(session.send(turn(HESK_TURN)))
    (back,) = session.last_retrieval.in_full
    text = "\n".join(part.text for m in private.requests[-1].messages for part in m.parts)
    assert back.name == "The Guild" and TITLE + f"{GAP}\n\n{GUILD[1]}" in text


@pytest.mark.parametrize(("model", "given"), [("Anthropic::claude-sonnet-4-6", False), (GLM, True)])
def test_a_question_follows_the_storyteller(tmp_path, story, cast, model, given) -> None:
    session = session_with(
        tmp_path, story, cast, lore(), model=model, replies=["A passage.", "An answer."]
    )
    list(session.send(turn("I walk on.")))
    list(session.ask(HESK_TURN))
    asked = session.provider.requests[-1].messages[-1].text
    assert HESK_TURN in asked
    assert ("(from the full entry)" in asked) is given


def test_the_switch_off_still_turns_it_off_for_everyone(tmp_path, story, cast, monkeypatch):
    monkeypatch.setattr(lore_whole_back, "WHOLE_BACK_TOP", 0)
    session = session_with(tmp_path, story, cast, lore())
    assert not session.lore_from_full_on()
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == ()


def test_it_is_on_by_default() -> None:
    assert lore_whole_back.WHOLE_BACK_TOP == 3
