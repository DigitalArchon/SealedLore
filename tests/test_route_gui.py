"""Choosing a route in the window: the route dialog, the label beside a model
field, the picker's Choose a route…, and Settings → Models. Offscreen, no
network: the hosts come from a capture of NanoGPT's listing."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.gui.model_hosts import hosts_catalog  # noqa: E402
from sealedlore.gui.model_picker import ModelCatalog, ModelField, ModelPickerDialog  # noqa: E402
from sealedlore.gui.route_dialog import RouteDialog  # noqa: E402
from sealedlore.models.route import ModelRoute  # noqa: E402
from sealedlore.providers.model_hosts import parse_model_hosts  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
GLM = "z-ai/glm-5.3"
DATA = json.loads((Path(__file__).parent / "data" / "nanogpt_providers_glm53.json").read_text())


@pytest.fixture
def app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    # The hosts as NanoGPT listed them, already fetched: no network.
    hosts_catalog()._found[(NANO, GLM)] = (time.monotonic(), parse_model_hosts(GLM, DATA))
    return app


def test_the_route_dialog_shows_each_host_and_nanogpts_own_routing(app):
    dialog = RouteDialog(NANO, GLM, None)
    table = dialog.table
    names = [table.topLevelItem(i).text(0) for i in range(table.topLevelItemCount())]
    assert names[0] == "NanoGPT's choice", "first by first token"
    assert "Parasail" in names and "StreamLake" not in names, "unavailable hosts left out"
    assert "Crusoe" not in names, "FP4 hosts are hidden while FP8 or better is ticked"
    assert "below FP8" in dialog.status.text()
    assert dialog.radios["subscription"].isChecked() and dialog.route() is None
    # FP8 or better leaves FP4 hosts out of the host choice.
    hosts = [dialog.host.itemData(i) for i in range(dialog.host.count())]
    assert "parasail" in hosts and "crusoe" not in hosts
    dialog.fp8.setChecked(False)
    assert "crusoe" in [dialog.host.itemData(i) for i in range(dialog.host.count())]
    shown = [table.topLevelItem(i).text(0) for i in range(table.topLevelItemCount())]
    assert "Crusoe" in shown
    dialog.fp8.setChecked(True)

    dialog.radios["latency"].setChecked(True)
    assert dialog.route() == ModelRoute(priority="latency", fp8=True)
    # Clicking a host's row chooses it, for this model.
    row = next(
        table.topLevelItem(i)
        for i in range(table.topLevelItemCount())
        if table.topLevelItem(i).text(0) == "Parasail"
    )
    dialog._host_clicked(row, 0)
    assert dialog.route() == ModelRoute(priority="host", host="parasail", host_model=GLM)
    dialog.deleteLater()


def test_a_model_with_one_host_has_no_route_to_choose(app):
    one = {**DATA, "providers": DATA["providers"][:1]}
    hosts_catalog()._found[(NANO, "one/model")] = (time.monotonic(), parse_model_hosts("x", one))
    dialog = RouteDialog(NANO, "one/model", ModelRoute(priority="price"))
    assert dialog.radios["subscription"].isChecked()
    assert not dialog.radios["latency"].isEnabled()
    assert "only one host" in dialog.status.text()
    dialog.deleteLater()


def test_a_fields_route_shows_beside_it_and_follows_its_model(app):
    endpoint = [NANO]
    field = ModelField(GLM, route_endpoint=lambda: endpoint[0])
    assert not field.route_button.isHidden() and field.route_button.text() == "Route…"
    field.set_route(ModelRoute(priority="latency"))
    assert field.route_button.text() == "⚡ Fastest start"
    assert "paid" in field.route_button.toolTip()
    field.set_route(ModelRoute(priority="host", host="parasail", host_model=GLM))
    assert field.effective_route() is not None
    field.setText("moonshotai/kimi-k2.6")  # a host is one model's
    assert field.effective_route() is None and field.route_button.text() == "Route…"
    field.setText("TEE/glm-5.3")
    assert field.route_button.isHidden(), "an enclave has no route"
    endpoint[0] = "https://openrouter.ai/api/v1"
    field.setText(GLM)
    assert field.route_button.isHidden(), "NanoGPT only"
    field.deleteLater()


def test_the_picker_offers_a_route_for_a_model_with_hosts(app, monkeypatch):
    catalog = ModelCatalog()
    catalog.models = {
        GLM: ModelInfo(id=GLM, hosts=("zai", "parasail")),
        "anthropic/x": ModelInfo(id="anthropic/x"),
    }
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    dialog = ModelPickerDialog(catalog, lambda: (None, "e", False), route_endpoint=NANO)
    rows = {dialog.table.topLevelItem(i).text(0): dialog.table.topLevelItem(i) for i in range(2)}
    dialog.table.setCurrentItem(rows["anthropic/x"])
    assert not dialog.route_button.isEnabled()
    dialog.table.setCurrentItem(rows[GLM])
    assert dialog.route_button.isEnabled()

    class Chosen(RouteDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.radios["price"].setChecked(True)
            return QDialog.Accepted

    monkeypatch.setattr("sealedlore.gui.model_picker.RouteDialog", Chosen)
    dialog._choose_route()
    assert dialog.chosen_route == ModelRoute(priority="price")
    dialog.deleteLater()


def test_settings_keeps_each_roles_route(app, tmp_path, story, cast):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import ProviderConfig
    from sealedlore.models.story import Story

    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.providers = [ProviderConfig(name="nano", base_url=NANO, model=GLM)]
    window.config.active_provider_name = "nano"
    window.config.scene_model = GLM
    window.config.model_routes = {"plot": ModelRoute(priority="price")}
    dialog = SettingsDialog(window.config, None, window, catalog=window.catalog)
    assert dialog.plot_model.route == ModelRoute(priority="price"), "loaded"
    dialog.scene_model.set_route(ModelRoute(priority="latency"))
    dialog.plot_model.set_route(None)
    assert dialog.minimumSizeHint().width() <= 660, "the route labels widened Settings"
    # The speed test measures each role on its route: GLM twice.
    targets = {t.label: t.roles for t in dialog.speed_targets()}
    assert targets[GLM] == ("Story", "Summarisation", "Authoring", "Image prompt writer")
    assert targets[f"{GLM} · Fastest first word · FP8+ · paid"] == ("Scene", "Plot")
    dialog._save()
    assert window.config.model_routes == {"scene": ModelRoute(priority="latency")}

    # With a chat open, the Story field's route is the chat's too.
    chat = Story(title="Tutor", mode="chat")
    dialog = SettingsDialog(window.config, chat, window, catalog=window.catalog)
    dialog.model.set_route(ModelRoute(priority="speed"))
    dialog._save()
    assert chat.defaults.main_route == ModelRoute(priority="speed")
    assert window.config.model_routes["story"] == ModelRoute(priority="speed")
    window.close()


def test_only_a_model_with_a_choice_of_hosts_offers_a_route(app, tmp_path):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import ProviderConfig

    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.providers = [
        ProviderConfig(name="nano", base_url=NANO, model="anthropic/claude-sonnet-4.6")
    ]
    window.config.active_provider_name = "nano"
    window.config.scene_model = GLM
    window.config.model_routes = {"story": ModelRoute(priority="latency")}
    window.catalog.models = {
        GLM: ModelInfo(id=GLM, hosts=("zai", "parasail")),
        "anthropic/claude-sonnet-4.6": ModelInfo(id="anthropic/claude-sonnet-4.6"),
    }
    dialog = SettingsDialog(window.config, None, window, catalog=window.catalog)
    assert not dialog.scene_model.route_button.isHidden()
    assert dialog.model.route_button.isHidden(), "Claude has one host"
    assert dialog.plot_model.route_button.isHidden(), "a blank field takes its fallback's"
    dialog._save()
    assert "story" not in window.config.model_routes, "a route on a one-host model was kept"
    window.catalog.models = None
    window.close()


def test_the_status_bar_says_when_a_route_isnt_followed(app):
    """The author: a host preference that falls back should at least show,
    bottom left, for when someone wonders what's wrong."""
    from sealedlore.engine.routing import check_route
    from sealedlore.gui.status import StatusStrip
    from sealedlore.models.node import Usage

    strip = StatusStrip()
    strip.set_routes([])
    assert strip.route_label.isHidden()
    fast = {"sort": "latency", "min_quantization": "fp8"}
    billed = Usage(prompt_tokens=10, completion_tokens=10, cost=0.0003, cost_reported=True)
    ok = check_route(GLM, fast, billed)
    strip.set_routes([("2026-09-28T21:14:00+00:00", ok, ["scene"])])
    assert strip.route_label.text() == "⚡ routes" and not strip.route_label.isHidden()
    assert (
        "scene: z-ai/glm-5.3 · Fastest first word · FP8+: as asked" in strip.route_label.toolTip()
    )
    host = {"order": ["parasail"], "min_quantization": "fp8"}
    dropped = check_route(GLM, host, Usage(prompt_tokens=10, cost_reported=True))
    strip.set_routes(
        [
            ("2026-09-28T21:14:00+00:00", ok, ["scene"]),
            ("2026-09-28T21:15:00+00:00", dropped, ["plot"]),
        ]
    )
    assert strip.route_label.text() == "⚠ route not followed (1)"
    tip = strip.route_label.toolTip()
    assert "plot: z-ai/glm-5.3 · parasail · FP8+: ⚠ NOT followed" in tip
    assert "subscription routing" in tip
    strip.deleteLater()
