"""What the review of 3.5.0b1 found where the window asks a server something
(Nov 22 2026), one test a fault: each fails on 0f289da. Offscreen, and
every server is a stand-in made here."""

from __future__ import annotations

import os
import threading
import time

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import image_jobs, video_jobs  # noqa: E402
from sealedlore.gui import settings_dialog as settings_module  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    Config,
    MediaEndpoint,
    ProviderConfig,
    bind_unbound_keys,
)
from sealedlore.providers import images as images_module  # noqa: E402
from sealedlore.providers import videos as videos_module  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.context_probe import ContextSize  # noqa: E402
from sealedlore.providers.local_images import ModelAsTyped  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    made = Config(providers=[chat], active_provider_name="NanoGPT", **changes)
    bind_unbound_keys(made)  # as a config is when loaded
    return made


def answered(app: QApplication, dialog: SettingsDialog, timeout: float = 10.0) -> None:
    """Until the server asked on its own thread has answered (`_ask_server`)."""
    deadline = time.monotonic() + timeout
    while dialog._asking:
        if time.monotonic() > deadline:
            raise AssertionError("the server asked never answered")
        app.processEvents()
        time.sleep(0.005)


# --- N6: Detect comes back, whatever the server answered ---------------------------


def _private_dialog() -> SettingsDialog:
    private = ProviderConfig(
        name="private", base_url="http://127.0.0.1:11434/v1", api_key="", model="m"
    )
    dialog = SettingsDialog(config(private_provider=private), None)
    dialog.private_url.setText("http://127.0.0.1:11434/v1")
    dialog.private_model.setText("m")
    return dialog


def test_detect_gives_its_button_back_when_the_asking_raises(app, monkeypatch):
    dialog = _private_dialog()

    def raises(url, key, model, **_):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(settings_module, "detect_context", raises)
    dialog._detect_private_context()
    answered(app, dialog)
    assert dialog.detect_button.isEnabled(), "left off, Detect could never be pressed again"
    assert dialog.detect_note.text().startswith("The server didn't say.")
    dialog.deleteLater()


@pytest.mark.parametrize(
    "tokens",
    [float("inf"), float("nan"), "8192", True, -5, None, pytest.param(10**30, id="30 digits")],
)
def test_detect_sets_no_budget_from_a_size_that_is_none(app, monkeypatch, tokens):
    dialog = _private_dialog()
    before = dialog.private_budget.value()
    monkeypatch.setattr(
        settings_module,
        "detect_context",
        lambda url, key, model, **_: ContextSize(tokens=tokens, source="a server"),
    )
    dialog._detect_private_context()
    answered(app, dialog)
    assert dialog.detect_button.isEnabled()
    assert dialog.private_budget.value() == before
    assert dialog.detect_note.text().startswith("The server didn't say.")
    dialog.deleteLater()


def test_detect_still_sets_the_budget_from_a_real_size(app, monkeypatch):
    dialog = _private_dialog()
    monkeypatch.setattr(
        settings_module,
        "detect_context",
        lambda url, key, model, **_: ContextSize(tokens=32768, source="loaded in Ollama"),
    )
    dialog._detect_private_context()
    answered(app, dialog)
    assert dialog.detect_button.isEnabled()
    assert dialog.private_budget.value() == int(32768 * 0.8) - 1_000
    assert "32,768 tokens (loaded in Ollama)" in dialog.detect_note.text()
    dialog.deleteLater()


def test_detect_leaves_the_dialog_free_while_the_server_takes_its_time(app, monkeypatch):
    """Detect asked on the GUI thread: the dialog froze until the server
    answered. Now it asks on a thread of its own, its button off meanwhile;
    closed before the answer, the dialog is left as it was."""
    go, back = threading.Event(), threading.Event()

    def slow(url, key, model, **_):
        go.wait(10)
        back.set()
        return ContextSize(tokens=32768, source="loaded in Ollama")

    monkeypatch.setattr(settings_module, "detect_context", slow)
    dialog = _private_dialog()
    before = dialog.private_budget.value()
    dialog._detect_private_context()
    assert not dialog.detect_button.isEnabled()
    assert dialog.detect_note.text() == "Asking the server…"
    dialog.done(0)  # closed while it asks
    go.set()
    assert back.wait(10)
    QTest.qWait(100)
    assert dialog.detect_note.text() == "Asking the server…"
    assert dialog.private_budget.value() == before
    dialog.deleteLater()


def test_find_a_server_answering_after_its_dialog_has_gone_touches_nothing(app, monkeypatch):
    from PySide6.QtCore import QEvent

    go, back = threading.Event(), threading.Event()

    def slow():
        go.wait(10)
        back.set()
        return []

    monkeypatch.setattr(settings_module, "find_local_servers", slow)
    shown: list[object] = []
    monkeypatch.setattr(SettingsDialog, "_servers_found", lambda *args: shown.append(args))
    dialog = SettingsDialog(config(), None)
    dialog.embeddings_enabled.setChecked(True)
    dialog.embed_where.setCurrentIndex(dialog.embed_where.findData("endpoint"))
    assert dialog.find_server_button.isEnabled()
    dialog.find_server_button.click()
    assert not dialog.find_server_button.isEnabled()
    assert dialog.find_server_note.text() == "Asking…"
    dialog.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    go.set()
    assert back.wait(10)
    QTest.qWait(100)
    assert shown == [], "what it brought back is dropped, the dialog gone"


# --- N3: the catalogs ask for no listing while a key or password is held back ------


def _at_once(refused: list[str]):
    def run(_owner, job, done, name=""):
        try:
            done(job(), None)
        except ProviderError as exc:
            refused.append(str(exc))
            done(None, str(exc))

    return run


@pytest.mark.parametrize("api", ["a1111", "comfyui"])
def test_the_picture_catalog_sends_no_held_back_password(app, tmp_path, monkeypatch, api):
    """As a hand-edited config.json has it: a password in the address of a
    server on another machine, plain http, never confirmed."""
    port = {"a1111": 7860, "comfyui": 8188}[api]
    endpoint = MediaEndpoint(base_url=f"http://admin:PROBE-PASSWORD@192.168.1.50:{port}", api=api)
    cfg = config(image_endpoint=endpoint)
    assert cfg.images().key_withheld
    sent: list[httpx.Request] = []

    def client(*_args, **_kwargs) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=[{"title": "dreamshaper_8.safetensors"}])

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module, "make_client", client)
    refused: list[str] = []
    monkeypatch.setattr(image_jobs, "run_detached", _at_once(refused))
    catalog = image_jobs.ImageCatalog(cfg, tmp_path)
    catalog.ensure(force=True)
    assert sent == [], "the password went with the listing's request"
    assert refused and "isn't sent until you confirm" in refused[0]
    assert not catalog.loading and catalog.models == []


def test_the_video_catalog_sends_no_held_back_password(app, tmp_path, monkeypatch):
    endpoint = MediaEndpoint(
        base_url="http://admin:PROBE-PASSWORD@192.168.1.50:8000/v1", api_key="video-key"
    )
    cfg = config(video_endpoint=endpoint)
    assert cfg.videos().key_withheld
    sent: list[httpx.Request] = []

    def client(*_args, **_kwargs) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"data": []})

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(videos_module, "make_client", client)
    refused: list[str] = []
    monkeypatch.setattr(video_jobs, "run_detached", _at_once(refused))
    catalog = video_jobs.VideoCatalog(cfg, tmp_path)
    catalog.ensure(force=True)
    assert sent == [] and refused and not catalog.loading


# --- N4: Check the server says when a workflow takes its model as typed -----------


def test_check_the_server_says_a_typed_model_is_sent_as_typed(app, monkeypatch):
    def listing(url, **kwargs):
        raise ModelAsTyped("DiffusersLoader", "repo_id")

    monkeypatch.setattr(settings_module, "list_own_models", listing)
    dialog = SettingsDialog(config(), None)
    dialog.image_api.setCurrentIndex(dialog.image_api.findData("comfyui"))
    dialog.image_model.setText("black-forest-labs/FLUX.1-dev")
    dialog.portrait_model.setText("black-forest-labs/FLUX.1-schnell")
    dialog.own_server.check_button.click()
    answered(app, dialog)
    words = dialog.own_server.check_note.text()
    assert words.startswith("✓ ComfyUI answered.") and "as typed" in words
    assert "DiffusersLoader" in words and "⚠" not in words
    assert dialog.image_model.text() == "black-forest-labs/FLUX.1-dev"
    assert dialog.portrait_model.text() == "black-forest-labs/FLUX.1-schnell"
    assert dialog.own_server.check_button.isEnabled()
    dialog.deleteLater()
