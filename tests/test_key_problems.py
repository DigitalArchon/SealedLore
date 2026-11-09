"""An API key with a character no HTTP header can carry (a line break, a zero-width
space from a web page, anything beyond ASCII) is refused in Settings, in the CLI's
configure and before any request, saying what's wrong and nothing of the key. h11's
own refusal quoted the header whole, key included, and that went into a dialog.
(DA Assistant's netguard.key_problem, Nov 2026.)"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from sealedlore.models.config import Config, MediaEndpoint, ProviderConfig, key_problem
from sealedlore.providers.base import ChatRequest, ProviderError, error_detail

SECRET = "sk-live-0123456789"
UNSENDABLE = {
    "a line break": f"{SECRET}\n",
    "a header after it": f"{SECRET}\r\nX-Other: 1",
    "a zero-width space": f"sk-live\u200b{SECRET}",
    "a space": f"sk-live {SECRET}",
    "a non-breaking space": f"sk-live\u00a0{SECRET}",
    "a letter beyond ASCII": f"{SECRET}\u00e9",
    "a control character": f"{SECRET}\x00",
}


@pytest.mark.parametrize("what", sorted(UNSENDABLE))
def test_a_key_no_header_can_carry_is_named_never_shown(what):
    problem = key_problem(UNSENDABLE[what], "NanoGPT")
    assert problem and "NanoGPT" in problem and SECRET not in problem


def test_an_ordinary_key_has_no_problem():
    for key in ("", SECRET, "sk-or-v1-AbC_dEf.123+/=", "nano-1234567890abcdef"):
        assert key_problem(key) is None, key


def _refused_before_sending(make, call) -> str:
    sent: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r)))
    with pytest.raises(ProviderError) as refused:
        call(make(client))
    assert sent == [], "nothing was sent"
    shown = error_detail(refused.value)
    assert SECRET not in shown and "enter it again in Settings" in shown
    return shown


def test_no_request_goes_with_one():
    """Each client that puts a key in a header refuses first: chat on either
    protocol, embeddings, pictures and video, and a TEE model's checks."""
    from sealedlore.messages import ContentPart, PromptMessage
    from sealedlore.models.config import EmbeddingProviderConfig
    from sealedlore.providers.anthropic import AnthropicProvider
    from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
    from sealedlore.providers.images import ImageClient
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider
    from sealedlore.providers.tee import TeeClient

    key = UNSENDABLE["a header after it"]
    hello = PromptMessage(role="user", parts=(ContentPart(text="Hello."),))
    request = ChatRequest(model="m", messages=[hello])
    chat = ProviderConfig(name="NanoGPT", base_url="https://nano-gpt.com/api/v1", api_key=key)
    claude = ProviderConfig(name="Anthropic", base_url="https://api.anthropic.com", api_key=key)
    assert "NanoGPT" in _refused_before_sending(
        lambda c: OpenAICompatibleProvider(chat, client=c), lambda p: list(p.stream(request))
    )
    _refused_before_sending(
        lambda c: AnthropicProvider(claude, client=c), lambda p: list(p.stream(request))
    )
    embeddings = EmbeddingProviderConfig(base_url="https://nano-gpt.com/api/v1", api_key=key)
    _refused_before_sending(
        lambda c: OpenAICompatibleEmbeddings(embeddings, client=c), lambda e: e.embed(["text"])
    )
    _refused_before_sending(
        lambda c: ImageClient("https://nano-gpt.com/api/v1", key, client=c),
        lambda i: i.generate({"model": "m", "prompt": "p"}),
    )
    tee = _refused_before_sending(
        lambda c: TeeClient("https://nano-gpt.com/api/v1", key, "TEE/x", client=c),
        lambda t: t.attest(),
    )
    assert "nano-gpt" in tee


def test_configure_refuses_one_and_saves_nothing(tmp_path: Path, capsys):
    from sealedlore.cli import main

    data = ["--data-dir", str(tmp_path)]
    with pytest.raises(SystemExit) as refused:
        main([*data, "configure", "--base-url", "https://nano-gpt.com/api/v1", "--api-key",
              UNSENDABLE["a zero-width space"]])  # fmt: skip
    assert SECRET not in str(refused.value) and "enter it again" in str(refused.value)
    said = capsys.readouterr()
    assert SECRET not in said.out + said.err
    assert not (tmp_path / "config.json").exists()


def test_settings_refuses_one_where_it_is(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QDialog

    from sealedlore.gui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    endpoint = ProviderConfig(name="NanoGPT", base_url="https://nano-gpt.com/api/v1", model="g")
    cfg = Config(
        providers=[endpoint],
        active_provider_name="NanoGPT",
        image_endpoint=MediaEndpoint(base_url="https://nano-gpt.com/api/v1"),
    )
    dialog = SettingsDialog(cfg, None)
    dialog.image_key.setText(UNSENDABLE["a zero-width space"])
    dialog._save()
    assert dialog.result() != QDialog.Accepted
    assert dialog.error.text().startswith("Images: The API key for pictures has a space")
    assert SECRET not in dialog.error.text()
    assert cfg.image_endpoint.api_key == ""
    dialog.deleteLater()
