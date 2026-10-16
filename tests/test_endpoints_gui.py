"""Several endpoints in the window: Settings → Endpoints (a list, Add,
Remove, Make default, the selected one's fields), the picker's Provider
column and the reference it stores, the endpoint's name beside a field and
on the model button. Offscreen, no network."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.gui.model_picker import (  # noqa: E402
    COLUMNS,
    ModelCatalog,
    ModelField,
    ModelPickerDialog,
)
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import RECOMMENDED_MODELS, Config, ProviderConfig  # noqa: E402
from sealedlore.models.route import ModelRoute  # noqa: E402
from sealedlore.providers.endpoints import EndpointsProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
ANTHROPIC = "https://api.anthropic.com"
PROVIDER = COLUMNS.index("Provider")


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def two_endpoints(**changes) -> Config:
    return Config(
        providers=[
            ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="z-ai/glm-5.3"),
            ProviderConfig(name="Anthropic", base_url=ANTHROPIC, api_key="ant-key"),
        ],
        active_provider_name="NanoGPT",
        **changes,
    )


def catalog_of(*refs: str) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog.models = {
        ref: ModelInfo(
            id=ref,
            name=ref.split("::")[-1],
            endpoint=ref.split("::")[0] if "::" in ref else "NanoGPT",
            hosts=("a", "b") if "glm" in ref else (),
        )
        for ref in refs
    }
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    return catalog


def rows(dialog: ModelPickerDialog) -> dict:
    table = dialog.table
    return {
        table.topLevelItem(i).text(0): table.topLevelItem(i)
        for i in range(table.topLevelItemCount())
    }


# --- the picker -----------------------------------------------------------------------


def test_the_picker_shows_each_models_provider_and_stores_its_reference(app):
    catalog = catalog_of("anthropic/claude-opus-5.5", "Anthropic::claude-opus-5-5", "z-ai/glm-5.3")
    dialog = ModelPickerDialog(
        catalog, lambda: (None, "e", False), current="Anthropic::claude-opus-5-5"
    )
    found = rows(dialog)
    # The Model column is the id as the endpoint knows it; Provider says which.
    assert found["claude-opus-5-5"].text(PROVIDER) == "Anthropic"
    assert found["anthropic/claude-opus-5.5"].text(PROVIDER) == "NanoGPT"
    assert "Settings → Endpoints" in found["claude-opus-5-5"].toolTip(PROVIDER)
    # Opened on the current reference, and what is chosen is the reference.
    assert dialog.selected_model() == "Anthropic::claude-opus-5-5"
    dialog.table.setCurrentItem(found["anthropic/claude-opus-5.5"])
    assert dialog.selected_model() == "anthropic/claude-opus-5.5"
    # The search finds an endpoint by name.
    dialog.search.setText("anthropic opus")
    shown = [key for key, row in found.items() if not row.isHidden()]
    assert sorted(shown) == ["anthropic/claude-opus-5.5", "claude-opus-5-5"]
    dialog.search.setText("Anthropic::")
    assert [key for key, row in found.items() if not row.isHidden()] == ["claude-opus-5-5"]
    assert "3 models" in dialog.status.text() or "of 3 models" in dialog.status.text()
    dialog.deleteLater()


def test_the_picker_says_which_endpoints_listing_couldnt_be_fetched(app):
    catalog = catalog_of("z-ai/glm-5.3")
    catalog.notes = {"Anthropic": "HTTP 401"}
    dialog = ModelPickerDialog(catalog, lambda: (None, "e", False))
    assert not dialog.listing_note.isHidden()
    assert "Anthropic (HTTP 401)" in dialog.listing_note.text()
    dialog.deleteLater()


def test_a_route_is_offered_only_on_the_models_own_nanogpt_endpoint(app):
    catalog = catalog_of("z-ai/glm-5.3", "Anthropic::z-ai/glm-5.3")
    urls = {"z-ai/glm-5.3": NANO, "Anthropic::z-ai/glm-5.3": ANTHROPIC}
    dialog = ModelPickerDialog(
        catalog, lambda: (None, "e", False), route_endpoint=lambda model: urls.get(model)
    )
    found = {
        dialog.table.topLevelItem(i).data(0, 0x0100 + 1): dialog.table.topLevelItem(i)
        for i in range(dialog.table.topLevelItemCount())
    }
    dialog.table.setCurrentItem(found["z-ai/glm-5.3"])
    assert dialog.route_button.isEnabled()
    dialog.table.setCurrentItem(found["Anthropic::z-ai/glm-5.3"])
    assert not dialog.route_button.isEnabled(), "Anthropic has no hosts to choose"
    dialog.deleteLater()


def test_the_attestation_settings_are_the_chosen_models_endpoint_with_its_bare_id(app):
    cfg = two_endpoints()
    catalog = catalog_of("private/glm-5-3", "Anthropic::private/x")
    provider = EndpointsProvider(cfg, make=lambda s: None)  # never used to send
    dialog = ModelPickerDialog(
        catalog, lambda: (provider, "e", False), current="Anthropic::private/x"
    )
    settings = dialog._attest_settings()
    assert settings is not None
    assert settings.name == "Anthropic" and settings.model == "private/x"
    assert settings.api_key == "ant-key"
    dialog.deleteLater()


def test_a_field_names_the_endpoint_beside_a_model_off_the_default_one(app):
    field = ModelField("Anthropic::claude-opus-5-5")
    assert not field.endpoint_label.isHidden() and field.endpoint_label.text() == "· Anthropic"
    field.setText("anthropic/claude-sonnet-4.6")
    assert field.endpoint_label.isHidden()
    field.setText("NanoGPT::private/glm-5-3")
    assert field.endpoint_label.text() == "· NanoGPT" and field.privacy.text() == "🔐 E2EE"
    field.deleteLater()


def test_a_fields_route_follows_the_endpoint_its_model_names(app):
    urls = {None: NANO, "Anthropic": ANTHROPIC}
    field = ModelField(
        "z-ai/glm-5.3",
        route_endpoint=lambda model: urls.get(model.split("::")[0] if "::" in model else None),
    )
    assert not field.route_button.isHidden()
    field.setText("Anthropic::claude-opus-5-5")
    assert field.route_button.isHidden(), "another endpoint has no routes"
    field.deleteLater()


# --- Settings → Endpoints -------------------------------------------------------------


def test_settings_lists_the_endpoints_and_edits_the_selected_one(app):
    cfg = two_endpoints(scene_model="Anthropic::claude-sonnet-4-6")
    dialog = SettingsDialog(cfg, None)
    assert [dialog.endpoint_list.item(i).text() for i in range(2)] == [
        "NanoGPT — nano-gpt.com  (default)",
        "Anthropic — api.anthropic.com",
    ]
    assert dialog.endpoint_list.currentRow() == 0, "opens on the default"
    assert dialog.provider_name.text() == "NanoGPT" and dialog.api_key.text() == "nano-key"
    assert not dialog.make_default.isEnabled()
    dialog.endpoint_list.setCurrentRow(1)
    assert dialog.provider_name.text() == "Anthropic" and dialog.base_url.text() == ANTHROPIC
    assert dialog.api_key.text() == "ant-key" and "x-api-key" in dialog.api_note.text()
    assert dialog.make_default.isEnabled()
    # The scene field names Anthropic, so its route is gone; the story's is NanoGPT's.
    assert dialog.scene_model.endpoint_label.text() == "· Anthropic"
    assert dialog.scene_model.route_button.isHidden()
    assert not dialog.model.route_button.isHidden()
    # Edits land on the selected endpoint; Browse… lists all of them as typed.
    dialog.api_key.setText("ant-key-2")
    dialog.base_url.setText("https://proxy.example/v1")
    assert dialog.endpoint_list.item(1).text() == "Anthropic — proxy.example"
    provider, key, owned = dialog._model_source()
    assert isinstance(provider, EndpointsProvider) and owned
    assert "Anthropic=https://proxy.example/v1" in key
    assert provider.endpoint_for("Anthropic::x").api_key == "ant-key-2"
    assert provider.endpoint_for("z-ai/glm-5.3").api_key == "nano-key"
    dialog._save()
    assert cfg.endpoint_for("Anthropic::x").api_key == "ant-key-2"
    assert cfg.endpoint_for("Anthropic::x").base_url == "https://proxy.example/v1"
    assert cfg.active_provider_name == "NanoGPT"
    assert cfg.scene_model == "Anthropic::claude-sonnet-4-6"
    dialog.deleteLater()


def test_add_make_default_and_remove_round_trip_with_the_keys_kept(app):
    cfg = Config(
        providers=[
            ProviderConfig(name="default", base_url=NANO, api_key="nano-key", model="z-ai/glm-5.3")
        ],
        active_provider_name="default",
        scene_model="z-ai/glm-5.3",
        model_routes={"story": ModelRoute(priority="latency")},
    )
    dialog = SettingsDialog(cfg, None)
    assert not dialog.remove_endpoint.isEnabled(), "the only endpoint stays"
    dialog._add_endpoint()
    assert dialog.endpoint_list.count() == 2 and dialog.endpoint_list.currentRow() == 1
    assert dialog.provider_name.text() == "New endpoint" and dialog.base_url.text() == ""
    dialog.provider_name.setText("Anthropic")
    dialog.base_url.setText(ANTHROPIC)
    dialog.api_key.setText("ant-key")
    assert dialog.endpoint_list.item(1).text() == "Anthropic — api.anthropic.com"

    # Make default renames the model fields so they keep their endpoints.
    dialog._make_default()
    assert dialog.endpoint_list.item(1).text().endswith("(default)")
    assert dialog.model.text() == "default::z-ai/glm-5.3"
    assert dialog.scene_model.text() == "default::z-ai/glm-5.3"
    assert dialog.model.endpoint_label.text() == "· default"
    dialog._save()
    assert [p.name for p in cfg.providers] == ["default", "Anthropic"]
    assert cfg.active_provider_name == "Anthropic"
    assert cfg.endpoint_for("Anthropic::x").api_key == "ant-key"
    assert cfg.active_provider().model == "default::z-ai/glm-5.3"
    assert cfg.endpoint_for("default::z-ai/glm-5.3").api_key == "nano-key"
    assert cfg.scene_model == "default::z-ai/glm-5.3"
    assert cfg.model_routes == {"story": ModelRoute(priority="latency")}, "still NanoGPT's"

    # Remove: the fields that name it are left alone, and the note says so.
    again = SettingsDialog(cfg, None)
    again.endpoint_list.setCurrentRow(0)
    assert again.provider_name.text() == "default"
    again._remove_endpoint()
    assert "Story model" in again.endpoints_note.text()
    assert "Scene model" in again.endpoints_note.text()
    assert again.endpoint_list.count() == 1 and again.provider_name.text() == "Anthropic"
    again._save()
    assert [p.name for p in cfg.providers] == ["Anthropic"]
    assert cfg.scene_model == "default::z-ai/glm-5.3", "left as it was"
    assert cfg.endpoint_for(cfg.scene_model) is None
    dialog.deleteLater()
    again.deleteLater()


def test_a_blank_or_repeated_name_or_a_plain_http_address_is_refused(app):
    cfg = two_endpoints()
    dialog = SettingsDialog(cfg, None)
    dialog.endpoint_list.setCurrentRow(1)
    dialog.provider_name.setText("NanoGPT")
    dialog._save()
    assert "Two endpoints are called 'NanoGPT'" in dialog.error.text()
    dialog.provider_name.setText("")
    dialog._save()
    assert "needs a name" in dialog.error.text()
    dialog.provider_name.setText("Proxy")
    dialog.base_url.setText("http://proxy.example/v1")
    dialog._save()
    assert dialog.error.text().startswith("Proxy: endpoint must use https")
    assert [p.name for p in cfg.providers] == ["NanoGPT", "Anthropic"], "nothing saved"
    dialog.deleteLater()


def test_the_recommended_ids_name_the_nanogpt_endpoint_when_it_isnt_the_default(app):
    cfg = two_endpoints()
    cfg.active_provider_name = "Anthropic"
    dialog = SettingsDialog(cfg, None)
    dialog._fill_recommended()
    assert dialog.scene_model.text() == f"NanoGPT::{RECOMMENDED_MODELS['scene_model']}"
    assert "NanoGPT::" in dialog.recommended_note.text()
    premium = next(
        a for a in dialog.model.recommended_button.menu().actions() if "(premium)" in a.text()
    )
    premium.trigger()
    assert dialog.model.text() == "NanoGPT::anthropic/claude-opus-5.5"
    # With NanoGPT the default, bare as before.
    cfg.active_provider_name = "NanoGPT"
    plain = SettingsDialog(cfg, None)
    plain._fill_recommended()
    assert plain.scene_model.text() == RECOMMENDED_MODELS["scene_model"]
    dialog.deleteLater()
    plain.deleteLater()


def test_the_speed_test_puts_each_role_on_its_own_endpoint(app):
    cfg = two_endpoints(scene_model="Anthropic::claude-sonnet-4-6")
    dialog = SettingsDialog(cfg, None)
    targets = {t.label: t for t in dialog.speed_targets()}
    assert targets["z-ai/glm-5.3"].settings.name == "NanoGPT"
    scene = targets["claude-sonnet-4-6 · Anthropic"]
    assert scene.settings.name == "Anthropic" and scene.settings.model == "claude-sonnet-4-6"
    assert scene.settings.api_key == "ant-key"
    # The plot and map roles fall back to the scene model, and with it its
    # endpoint; the lore model has a default of its own.
    assert set(scene.roles) >= {"Scene", "Plot", "Map", "Map art"}
    dialog.deleteLater()


# --- the window -----------------------------------------------------------------------


def test_the_model_button_and_the_session_line_name_the_endpoint(app, tmp_path: Path, story, cast):
    from sealedlore.gui.main_window import MainWindow

    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.providers = two_endpoints().providers
    window.config.active_provider_name = "NanoGPT"
    window.open_story(story.id)
    window.session.story.defaults.main_model = "Anthropic::claude-opus-5-5"
    window._refresh_model_button()
    assert window.model_button.text() == "claude-opus-5-5 · Anthropic ▾"
    assert "claude-opus-5-5 · Anthropic" in window.model_button.toolTip()
    line = window.session_line.label.text()
    assert "claude-opus-5-5 · Anthropic" in line and "via api.anthropic.com" in line
    assert window.route_button.isHidden(), "no NanoGPT route for Anthropic's model"
    window.session.story.defaults.main_model = "z-ai/glm-5.3"
    window._refresh_model_button()
    assert window.model_button.text() == "glm-5.3 ▾"
    assert "via nano-gpt.com" in window.session_line.label.text()
    window.close()
