"""Settings → Images for a server of one's own (gui/own_image_server.py):
the service, its rows, saving what was typed, and Check the server.
Offscreen, no network: the check's listing is stubbed."""

from __future__ import annotations

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import settings_dialog as settings_module  # noqa: E402
from sealedlore.gui.own_image_server import describe_workflow  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, MediaEndpoint, ProviderConfig  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.local_images import LARGE_SIZES, _info  # noqa: E402
from tests.test_images_gui import window  # noqa: E402,F401  (the window fixture)

NANO = "https://nano-gpt.com/api/v1"
WORKFLOW = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "{{model}}"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
    "3": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "4": {"class_type": "X", "inputs": {"what": "{{mood}}"}},
}


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**endpoint) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    return Config(
        providers=[chat],
        active_provider_name="NanoGPT",
        image_endpoint=MediaEndpoint(**endpoint),
    )


def answered(app: QApplication, dialog: SettingsDialog, timeout: float = 10.0) -> None:
    """Until the server asked on its own thread has answered (`_ask_server`)."""
    deadline = time.monotonic() + timeout
    while dialog._asking:
        if time.monotonic() > deadline:
            raise AssertionError("the server asked never answered")
        app.processEvents()
        time.sleep(0.005)


def choose(dialog: SettingsDialog, api: str) -> None:
    dialog.image_api.setCurrentIndex(dialog.image_api.findData(api))


def test_pictures_may_be_drawn_on_ones_own_server_but_never_videos(app):
    dialog = SettingsDialog(config(), None)
    pictures = {dialog.image_api.itemData(i) for i in range(dialog.image_api.count())}
    videos = {dialog.video_api.itemData(i) for i in range(dialog.video_api.count())}
    assert {"comfyui", "a1111"} <= pictures
    assert not {"comfyui", "a1111"} & videos
    dialog.deleteLater()


def test_the_rows_follow_the_service(app):
    dialog = SettingsDialog(config(), None)
    server = dialog.own_server
    assert server.isHidden()  # NanoGPT's, from the chat's address
    choose(dialog, "comfyui")
    dialog._image_fold.set_open(True)
    assert not server.isHidden()
    assert not server.workflow.isHidden() and not server.workflow_edit.isHidden()
    assert dialog.image_url.placeholderText() == "http://127.0.0.1:8188"
    choose(dialog, "a1111")
    assert not server.isHidden()
    assert server.workflow.parentWidget().isHidden()  # no workflows for AUTOMATIC1111
    assert dialog.image_url.placeholderText() == "http://127.0.0.1:7860"
    assert "user:password" in dialog.image_key.placeholderText()
    choose(dialog, "openrouter")
    assert server.isHidden()
    dialog.deleteLater()


def test_a_saved_server_opens_with_its_rows_shown(app):
    dialog = SettingsDialog(config(api="comfyui"), None)
    assert dialog._image_fold.is_open() and not dialog.own_server.isHidden()
    dialog.deleteLater()


def test_save_keeps_the_workflows_strength_and_settings(app):
    saved = config()
    dialog = SettingsDialog(saved, None)
    choose(dialog, "comfyui")
    dialog.own_server.workflow.setPlainText(json.dumps(WORKFLOW))
    dialog.own_server.strength.setValue(0.45)
    dialog.own_server.extra.setText('{"steps": 30, "cfg": 4.5}')
    assert dialog._write()
    endpoint = saved.image_endpoint
    assert endpoint.api == "comfyui" and endpoint.base_url == ""
    assert json.loads(endpoint.workflow) == WORKFLOW
    assert endpoint.strength == 0.45 and endpoint.extra_body == {"steps": 30, "cfg": 4.5}
    assert saved.images().base_url == "http://127.0.0.1:8188"
    dialog.deleteLater()


@pytest.mark.parametrize(
    ("field", "text", "said"),
    [
        ("workflow", '{"nodes": [], "links": []}', "Export (API)"),
        ("workflow_edit", "{not json", "isn't JSON"),
        ("extra", "steps=30", "JSON between braces"),
    ],
)
def test_what_comfyui_couldnt_use_is_said_on_its_page(app, field, text, said):
    dialog = SettingsDialog(config(), None)
    choose(dialog, "comfyui")
    box = getattr(dialog.own_server, field)
    (box.setPlainText if hasattr(box, "setPlainText") else box.setText)(text)
    assert not dialog._write()
    assert dialog.error.text().startswith("Images:") and said in dialog.error.text()
    dialog.deleteLater()


def test_a_service_not_in_use_keeps_what_it_held_unchecked(app):
    saved = config()
    dialog = SettingsDialog(saved, None)
    choose(dialog, "comfyui")
    dialog.own_server.extra.setText("not json yet")
    choose(dialog, "nanogpt")
    assert dialog._write()  # not checked while NanoGPT draws
    dialog.deleteLater()


def test_check_the_server_lists_its_models_and_puts_one_in_fields_that_werent_its(app, monkeypatch):
    asked: list[dict] = []
    found = [_info("juggernautXL.safetensors", "juggernautXL.safetensors", 1, True)]

    def listing(url, **kwargs):
        asked.append({"url": url, **kwargs})
        return found

    monkeypatch.setattr(settings_module, "list_own_models", listing)
    dialog = SettingsDialog(config(), None)
    choose(dialog, "a1111")
    dialog.own_server.check_button.click()
    answered(app, dialog)
    (call,) = asked
    assert call["url"] == "http://127.0.0.1:7860" and call["api"] == "a1111"
    words = dialog.own_server.check_note.text()
    assert "✓ AUTOMATIC1111 / Forge answered: juggernautXL.safetensors." in words
    assert "Image model and Portrait model set to juggernautXL.safetensors" in words
    assert dialog.image_model.text() == "juggernautXL.safetensors"
    assert dialog.portrait_model.text() == "juggernautXL.safetensors"
    sizes = [dialog.image_size.itemText(i) for i in range(dialog.image_size.count())]
    assert sizes == list(LARGE_SIZES)
    dialog.deleteLater()


def test_a_server_that_doesnt_answer_is_said(app, monkeypatch):
    def listing(url, **kwargs):
        raise ProviderError("Nothing answered at http://127.0.0.1:8188 (refused)")

    monkeypatch.setattr(settings_module, "list_own_models", listing)
    dialog = SettingsDialog(config(), None)
    choose(dialog, "comfyui")
    dialog.own_server.check_button.click()
    answered(app, dialog)
    assert dialog.own_server.check_note.text().startswith("⚠ Nothing answered")
    assert dialog.own_server.check_button.isEnabled()
    dialog.deleteLater()


def test_a_workflow_is_described_under_its_box():
    assert describe_workflow("") == "Built in."
    said = describe_workflow(json.dumps(WORKFLOW))
    assert said.startswith("Yours, 4 nodes: {{prompt}}, {{model}}, {{image}}; takes 1 picture.")
    assert "Not filled in: {{mood}}" in said
    no_prompt = {"1": {"class_type": "X", "inputs": {"w": "{{width}}"}}}
    assert "No {{prompt}}" in describe_workflow(json.dumps(no_prompt))
    assert describe_workflow('{"nodes": []}').startswith("⚠ This is the workflow")


def test_the_picture_dialog_says_a_picture_on_ones_own_server_costs_nothing(app, window):  # noqa: F811
    from sealedlore.gui.image_dialog import ImageDialog
    from sealedlore.gui.image_jobs import listing_entry

    model = _info("juggernautXL.safetensors", "juggernautXL.safetensors", 1, True)
    window.config.image_endpoint = MediaEndpoint(api="comfyui")
    window.config.image_models = [listing_entry(model)]
    window.config.image_models_source = "http://127.0.0.1:8188"
    dialog = ImageDialog(window.session, parent=window)
    dialog.model.setText("juggernautXL.safetensors")
    dialog.size_choice.setCurrentText("1024x1024")
    dialog._sync()
    assert dialog.price.text() == "No charge: drawn on this computer, by your own server."
    assert "$" not in dialog.generate_button.text()
    dialog.deleteLater()
