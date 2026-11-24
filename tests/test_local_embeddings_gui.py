"""Settings → Lore for embeddings made on this computer: Made by, the built-in
model's row (gui/builtin_embeddings.py) and Find a server on this computer.
Offscreen, no network: downloads run on a MockTransport, the server probe is
stubbed."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui import builtin_embeddings as row_module  # noqa: E402
from sealedlore.gui import settings_dialog as settings_module  # noqa: E402
from sealedlore.gui.builtin_embeddings import BuiltinModelRow  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, EmbeddingProviderConfig, ProviderConfig  # noqa: E402
from sealedlore.providers import local_embeddings as local  # noqa: E402
from sealedlore.providers.local_servers import FoundServer  # noqa: E402
from tests.test_local_embeddings import hub, stand_ins  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.01)


def config(embeddings: EmbeddingProviderConfig | None = None) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    return Config(providers=[chat], active_provider_name="NanoGPT", embedding_provider=embeddings)


def test_made_by_this_computer_shows_the_model_and_puts_the_endpoint_away(app, tmp_path):
    dialog = SettingsDialog(config(), None, root=tmp_path)
    assert not dialog.embed_where.isEnabled() and dialog.builtin_row.isHidden()
    dialog.embeddings_enabled.setChecked(True)
    assert dialog.embed_where.currentData() == "endpoint"  # as before, by default
    assert dialog.embed_base_url.isEnabled() and dialog.builtin_row.isHidden()
    dialog.embed_where.setCurrentIndex(dialog.embed_where.findData("builtin"))
    assert not dialog.builtin_row.isHidden()
    assert not dialog.embed_base_url.isEnabled() and not dialog.find_server_button.isEnabled()
    assert not dialog.embeddings_fold.is_open()  # the endpoint's rows put away
    assert "Not on this computer yet" in dialog.builtin_row.state.text()
    dialog.deleteLater()


def test_save_keeps_the_choice_and_the_endpoint_typed_beside_it(app, tmp_path):
    saved = config()
    dialog = SettingsDialog(saved, None, root=tmp_path)
    dialog.embeddings_enabled.setChecked(True)
    dialog.embed_base_url.setText("http://127.0.0.1:11434/v1")
    dialog.embed_where.setCurrentIndex(dialog.embed_where.findData("builtin"))
    assert dialog._write()
    assert saved.embedding_provider.builtin
    assert saved.embedding_provider.base_url == "http://127.0.0.1:11434/v1"  # kept for later
    assert saved.embeddings().model == local.BUILTIN_EMBEDDING_MODEL
    reopened = SettingsDialog(saved, None, root=tmp_path)
    assert reopened.embed_where.currentData() == "builtin"
    dialog.deleteLater()
    reopened.deleteLater()


def test_the_private_note_leaves_out_lore_embedded_here(app, tmp_path):
    dialog = SettingsDialog(config(), None, root=tmp_path)
    dialog.embeddings_enabled.setChecked(True)
    assert "lore sent for embeddings" in dialog._still_unencrypted()
    dialog.embed_where.setCurrentIndex(dialog.embed_where.findData("builtin"))
    assert "lore sent for embeddings" not in dialog._still_unencrypted()
    dialog.embed_where.setCurrentIndex(dialog.embed_where.findData("endpoint"))
    dialog.embed_base_url.setText("http://127.0.0.1:11434/v1")
    assert "lore sent for embeddings" not in dialog._still_unencrypted()
    dialog.deleteLater()


def test_find_a_server_fills_in_the_first_that_embeds(app, tmp_path, monkeypatch):
    found = [
        FoundServer("LM Studio", "http://127.0.0.1:1234/v1", ("qwen3-8b",)),
        FoundServer("Ollama", "http://127.0.0.1:11434/v1", ("llama3", "bge-m3:latest")),
    ]
    monkeypatch.setattr(settings_module, "find_local_servers", lambda: found)
    dialog = SettingsDialog(config(EmbeddingProviderConfig(api_key="old")), None, root=tmp_path)
    dialog.find_server_button.click()
    wait_for(app, lambda: not dialog._asking)
    assert dialog.embed_base_url.text() == "http://127.0.0.1:11434/v1"
    assert dialog.embed_model.text() == "bge-m3:latest"
    assert dialog.embed_api_key.text() == ""  # no key goes to a server here
    assert "Filled in: bge-m3:latest on Ollama." in dialog.find_server_note.text()
    dialog.deleteLater()


def test_the_model_downloads_with_its_progress_and_can_be_removed(app, tmp_path, monkeypatch):
    contents = stand_ins(monkeypatch)
    real = local.download
    monkeypatch.setattr(
        row_module,
        "download",
        lambda root, **kw: real(root, client=hub(contents, []), **kw),
    )
    row = BuiltinModelRow(tmp_path)
    assert not row.download_button.isHidden() and row.remove_button.isHidden()
    row.download_button.click()
    assert row.downloading and row.download_button.text() == "Stop"
    wait_for(app, lambda: not row.downloading)
    assert local.installed(tmp_path)
    assert row.state.text().startswith("On this computer")
    assert row.download_button.isHidden() and not row.remove_button.isHidden()
    row.remove_button.click()
    assert not local.installed(tmp_path) and row.state.text().startswith("Not on this computer")
    row.deleteLater()


def test_stop_keeps_nothing_and_says_so(app, tmp_path, monkeypatch):
    stand_ins(monkeypatch)
    started = []

    def slow(root, *, progress, stopped):
        started.append(1)
        while not stopped():
            time.sleep(0.01)
        raise local.DownloadStopped()

    monkeypatch.setattr(row_module, "download", slow)
    row = BuiltinModelRow(tmp_path)
    row.download_button.click()
    wait_for(app, lambda: started)
    row.download_button.click()  # Stop
    wait_for(app, lambda: not row.downloading)
    assert row.state.text().startswith("Stopped: nothing half-downloaded was kept.")
    assert not local.installed(tmp_path)
    row.deleteLater()


def test_a_failed_download_says_why(app, tmp_path, monkeypatch):
    def failing(root, **_kw):
        raise local.ProviderError("Hugging Face answered HTTP 503 for onnx/model_int8.onnx")

    monkeypatch.setattr(row_module, "download", failing)
    row = BuiltinModelRow(tmp_path)
    row.download_button.click()
    wait_for(app, lambda: not row.downloading)
    assert "HTTP 503" in row.state.text() and row.state.text().startswith("⚠")
    row.deleteLater()
