"""Which models are end-to-end encrypted, and which only TEE, shown where a
model is chosen (the author, Sept 2026: "show which models offer full
Private Mode instead of just TEE")."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.gui.model_picker import (  # noqa: E402
    COLUMNS,
    ModelCatalog,
    ModelField,
    ModelPickerDialog,
)
from sealedlore.providers.private_catalog import encrypted_counterpart  # noqa: E402

PRIVACY = COLUMNS.index("Privacy")


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def picker() -> ModelPickerDialog:
    catalog = ModelCatalog()
    catalog.models = {
        model_id: ModelInfo(id=model_id, name=model_id)
        for model_id in ("TEE/glm-5.3", "private/glm-5-3", "TEE/qwen3.6-27b", "anthropic/x")
    }
    # Fresh for this endpoint, so opening the dialog fetches nothing.
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    return ModelPickerDialog(catalog, lambda: (None, "e", False))


def rows(dialog: ModelPickerDialog) -> dict:
    table = dialog.table
    return {
        table.topLevelItem(i).text(0): table.topLevelItem(i)
        for i in range(table.topLevelItemCount())
    }


def test_the_picker_says_which_models_are_end_to_end_encrypted(app):
    dialog = picker()
    found = rows(dialog)
    assert found["private/glm-5-3"].text(PRIVACY) == "🔐 Encrypted"
    assert found["TEE/glm-5.3"].text(PRIVACY) == "TEE · 🔐 version"
    assert "private/glm-5-3" in found["TEE/glm-5.3"].toolTip(PRIVACY)
    assert found["TEE/qwen3.6-27b"].text(PRIVACY) == "TEE"
    assert found["anthropic/x"].text(PRIVACY) == ""
    assert "gateway" in found["TEE/qwen3.6-27b"].toolTip(PRIVACY)

    # Choosing one says what it keeps private, under the list.
    dialog.table.setCurrentItem(found["private/glm-5-3"])
    assert not dialog.privacy_note.isHidden()
    assert "End-to-end encrypted" in dialog.privacy_note.text()
    dialog.table.setCurrentItem(found["TEE/glm-5.3"])
    assert "in the clear" in dialog.privacy_note.text()
    assert "private/glm-5-3" in dialog.privacy_note.text()
    dialog.table.setCurrentItem(found["anthropic/x"])
    assert dialog.privacy_note.isHidden()
    dialog.deleteLater()


def test_a_model_field_labels_its_privacy_as_it_is_typed(app):
    field = ModelField("private/kimi-k3")
    assert field.privacy.text() == "🔐 E2EE" and "sealed" in field.privacy.toolTip()
    field.setText("TEE/glm-5.3")
    assert field.privacy.text() == "TEE" and "in the clear" in field.privacy.toolTip()
    field.setText("anthropic/claude-sonnet-4.6")
    assert field.privacy.isHidden()
    field.deleteLater()


def test_a_tee_model_knows_its_encrypted_twin():
    ids = ["private/deepseek-v4-1-flash", "private/gemma4-31b:thinking", "TEE/x"]
    assert encrypted_counterpart("TEE/deepseek-v4.1-flash", ids) == "private/deepseek-v4-1-flash"
    assert encrypted_counterpart("TEE/gemma4-31b:thinking", ids) == "private/gemma4-31b:thinking"
    assert encrypted_counterpart("TEE/gemma4-31b", ids) is None
    assert encrypted_counterpart("private/deepseek-v4-1-flash", ids) is None


def ids_shown(dialog: ModelPickerDialog) -> list[str]:
    return sorted(
        dialog.table.topLevelItem(i).text(0)
        for i in range(dialog.table.topLevelItemCount())
        if not dialog.table.topLevelItem(i).isHidden()
    )


def test_searching_tee_or_private_finds_both_versions_of_a_model(app):
    dialog = picker()
    dialog.search.setText("tee glm")
    assert ids_shown(dialog) == ["TEE/glm-5.3", "private/glm-5-3"]
    dialog.search.setText("encrypted")
    assert ids_shown(dialog) == ["TEE/glm-5.3", "private/glm-5-3"]
    dialog.search.setText("tee")
    assert "anthropic/x" not in ids_shown(dialog)
    dialog.deleteLater()


def test_a_tee_model_offers_its_encrypted_twin_in_one_click(app):
    dialog = picker()
    found = rows(dialog)
    dialog.table.setCurrentItem(found["TEE/qwen3.6-27b"])
    assert dialog.twin_button.isHidden(), "no twin, no button"
    dialog.search.setText("tee/glm")  # hides the private row
    dialog.table.setCurrentItem(rows(dialog)["TEE/glm-5.3"])
    assert not dialog.twin_button.isHidden()
    assert "private/glm-5-3" in dialog.twin_button.text()
    dialog.twin_button.click()
    assert dialog.result() == QDialog.Accepted
    assert dialog.selected_model() == "private/glm-5-3"
    dialog.deleteLater()


def test_a_limited_picker_lists_only_what_it_may_choose(app):
    from sealedlore.providers.tee import move_refusal

    catalog = ModelCatalog()
    catalog.models = {
        m: ModelInfo(id=m, name=m)
        for m in ("TEE/glm-5.3", "private/glm-5-3", "TEE/qwen3.6-27b", "anthropic/x")
    }
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    tee = ModelPickerDialog(
        catalog,
        lambda: (None, "e", False),
        allowed=lambda m: move_refusal("TEE/glm-5.3", m) is None,
        allowed_note="kept to TEE",
    )
    assert ids_shown(tee) == ["TEE/glm-5.3", "TEE/qwen3.6-27b", "private/glm-5-3"]
    assert "kept to TEE" in tee.status.text()
    encrypted = ModelPickerDialog(
        catalog,
        lambda: (None, "e", False),
        current="private/glm-5-3",
        allowed=lambda m: move_refusal("private/glm-5-3", m) is None,
    )
    assert ids_shown(encrypted) == ["private/glm-5-3"]
    tee.deleteLater()
    encrypted.deleteLater()
