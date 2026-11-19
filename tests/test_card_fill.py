"""Expand and Generate character profile in Your characters
(engine/card_fill.py, gui/card_fill.py): the authoring model writes, the
author takes what they like field by field, and nothing changes before."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sealedlore.engine.card_fill import (
    EXPAND,
    GENERATE,
    CardFill,
    apply_changes,
    build_card_fill_messages,
    parse_card_fill,
    proposed_changes,
)
from sealedlore.models.character import Character
from sealedlore.models.tavern import TavernFields

REPLY = json.dumps(
    {
        "name": "Halden Vey",
        "aliases": ["the Marsh Warden", "Old Vey"],
        "canon": "the Saltmarsh Chronicles",
        "summary": "A former dyke warden who walks the drowned coast alone.",
        "description": "Haunted by the family he lost, he survives by instinct.",
        "personality": "Laconic, wary, stubbornly decent.",
        "voice": "Few words, flat and dry.",
        "competence": "A superb boatman and tracker; no talker.",
        "scenario": "A lamp-oil store under siege.",
        "greeting": "*{{char}} lowers the boat hook a little.* You lost, {{user}}?",
        "examples": "<START>\n{{user}}: Who are you?\n{{char}}: Nobody.",
        "tags": ["saltmarsh", "loner"],
        "notes": "Kept to the books.",
    }
)


def vey_card() -> Character:
    return Character(name="Halden Vey", summary="A lone boatman.")


def test_the_request_has_the_card_what_to_do_the_world_and_the_wishes() -> None:
    card = vey_card()
    card.tavern = TavernFields(first_mes="Hello.")
    system, user = build_card_fill_messages(
        card, mode=EXPAND, wishes="make him tougher", world="the Saltmarsh Chronicles"
    )
    text = user.parts[0].text
    assert '"summary": "A lone boatman."' in text and '"greeting": "Hello."' in text
    assert "Expand this card" in text and "the Saltmarsh Chronicles" in text
    assert "make him tougher" in text
    assert '"greeting"' in system.parts[0].text  # the shape asked for
    # Without wishes or a world, neither line goes.
    plain = build_card_fill_messages(card, mode=GENERATE)[1].parts[0].text
    assert "which come first" not in plain and "They come from" not in plain
    with pytest.raises(ValueError):
        build_card_fill_messages(Character(name=""), mode=GENERATE)


def test_the_reply_becomes_changes_to_take_and_the_card_takes_them() -> None:
    card = vey_card()
    fill = parse_card_fill(REPLY)
    assert fill.notes == "Kept to the books."
    changes = proposed_changes(card, fill, EXPAND)
    keys = [c.key for c in changes]
    assert "name" not in keys, "the same name is no change"
    assert keys[:3] == ["aliases", "canon", "summary"]
    summary = next(c for c in changes if c.key == "summary")
    assert summary.old == "A lone boatman."
    apply_changes(card, changes)
    assert card.aliases == ["the Marsh Warden", "Old Vey"]
    assert card.canon == "the Saltmarsh Chronicles"
    assert card.voice_notes == "Few words, flat and dry."
    assert card.competence.notes.startswith("A superb boatman")
    assert card.tavern.first_mes.startswith("*{{char}}")
    assert card.tavern.tags == ["saltmarsh", "loner"]
    assert proposed_changes(card, fill, EXPAND) == []


def test_a_list_of_example_messages_reads_as_lines() -> None:
    fill = parse_card_fill(
        json.dumps({"examples": ["<START>\n{{char}}: One.", "<START>\n{{char}}: Two."]})
    )
    assert fill.fields["examples"] == "<START>\n{{char}}: One.\n<START>\n{{char}}: Two."


def test_a_blank_answer_never_empties_a_field() -> None:
    card = vey_card()
    fill = CardFill(fields={"summary": "", "personality": "Grim."})
    assert [c.key for c in proposed_changes(card, fill, EXPAND)] == ["personality"]


def test_a_reply_with_no_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_card_fill("Sorry, I can't.")
    with pytest.raises(ValueError):
        parse_card_fill('{"notes": "nothing"}')


def test_the_session_writes_on_the_authoring_model_and_logs_it(tmp_path: Path, estimator) -> None:
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config, ProviderConfig
    from sealedlore.models.story import Story
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import StoryBundle, read_api_log

    config = Config(
        providers=[ProviderConfig(name="NanoGPT", api_key="k", model="story-model")],
        active_provider_name="NanoGPT",
        authoring_model="authoring-model",
    )
    provider = MockChatProvider([REPLY])
    card = vey_card()
    session = StorySession(
        StoryBundle(story=Story(title="Characters"), cast=[card]),
        config,
        provider,
        root=tmp_path,
        estimator=estimator,
        learn_corrections=False,
    )
    fill = session.fill_card(card, mode=GENERATE, wishes="a criminal version")
    assert fill.fields["name"] == "Halden Vey"
    assert provider.last_request.model == "authoring-model"
    assert "a criminal version" in provider.last_request.messages[-1].parts[0].text
    assert card.summary == "A lone boatman.", "nothing on the card changes here"
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["card_fill_request", "card_fill_response"]


# --- the window -----------------------------------------------------------------


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_write_profile_asks_writes_and_returns_what_is_taken(app, monkeypatch) -> None:
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui import card_fill

    class Session:
        authoring_model = "authoring-model"
        provider = None

        def fill_card(self, character, *, mode, wishes, world):
            seen.update(mode=mode, wishes=wishes, world=world)
            return parse_card_fill(REPLY)

    seen: dict = {}

    def ask(dialog):
        dialog.wishes.setPlainText("tougher")
        dialog.from_world.setChecked(True)
        dialog.world.setText("the Saltmarsh Chronicles")
        return QDialog.Accepted

    def review(dialog):
        rows = dialog._rows
        for tick, text, change in rows:
            if change.key == "greeting":
                tick.setChecked(False)
            if change.key == "summary":
                text.setPlainText("A lone boatman, harder now.")
        return QDialog.Accepted

    monkeypatch.setattr(card_fill.CardFillDialog, "exec", ask)
    monkeypatch.setattr(card_fill.CardChangesDialog, "exec", review)

    def run(parent, session, words, job, result):
        for _ in job():
            pass

    monkeypatch.setattr(card_fill, "_run", run)
    taken = card_fill.write_profile(None, Session(), vey_card(), EXPAND)
    assert seen == {"mode": EXPAND, "wishes": "tougher", "world": "the Saltmarsh Chronicles"}
    keys = {c.key: c.new for c in taken}
    assert "greeting" not in keys
    assert keys["summary"] == "A lone boatman, harder now."


def test_your_characters_offers_both(app, tmp_path: Path) -> None:
    from sealedlore.gui.card_fill import EXPAND_LABEL, GENERATE_LABEL
    from sealedlore.gui.collection_page import CollectionPage

    page = CollectionPage(lambda: None)
    actions = [a.text() for a in page.write_button.menu().actions()]
    assert actions == [EXPAND_LABEL, GENERATE_LABEL]
    page.deleteLater()
