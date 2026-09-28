"""A NanoGPT model's hosts (providers/model_hosts.py) and the Hosts column in
the model list. `tests/data/nanogpt_providers_glm53.json` is a trimmed
capture of NanoGPT's public listing for GLM 5.3 (Sept 2026)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.catalog import model_info
from sealedlore.providers.base import ProviderError
from sealedlore.providers.model_hosts import (
    fetch_model_hosts,
    hosts_url,
    parse_model_hosts,
)

DATA = json.loads((Path(__file__).parent / "data" / "nanogpt_providers_glm53.json").read_text())


def test_a_listing_gives_each_hosts_figures_and_nanogpts_own():
    hosts = parse_model_hosts("z-ai/glm-5.3", DATA)
    assert hosts.supported
    assert hosts.auto_first_token_ms and hosts.auto_tokens_per_second
    parasail = hosts.host("parasail")
    assert parasail is not None and parasail.name == "Parasail"
    assert parasail.fp8_or_better and parasail.privacy_label == "No retention"
    assert parasail.first_token_ms and parasail.tokens_per_second
    assert parasail.input_price == pytest.approx(
        next(p for p in DATA["providers"] if p["provider"] == "parasail")["pricing"][
            "inputPer1kTokens"
        ]
        * 1000
    )
    crusoe = hosts.host("crusoe")
    assert crusoe is not None and crusoe.bits == 4 and not crusoe.fp8_or_better
    # A precision NanoGPT doesn't know doesn't pass the floor, as its own
    # min_quantization doesn't.
    assert not hosts.host("relace").fp8_or_better
    assert hosts.host("friendli").privacy_label == "Kept, not trained on"
    assert "streamlake" not in {host.id for host in hosts.available}


def test_the_listing_is_on_the_endpoints_host_outside_its_api():
    assert (
        hosts_url("https://nano-gpt.com/api/v1", "z-ai/glm-5.3")
        == "https://nano-gpt.com/api/models/z-ai%2Fglm-5.3/providers"
    )


def test_a_failed_listing_is_a_provider_error(monkeypatch):
    import sealedlore.providers.model_hosts as module

    def refuse(url, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    monkeypatch.setattr(module, "make_client", refuse)
    with pytest.raises(ProviderError, match="hosts"):
        fetch_model_hosts("https://nano-gpt.com/api/v1", "z-ai/glm-5.3")


def test_the_model_list_carries_each_models_hosts():
    info = model_info("z-ai/glm-5.3", {"name": "GLM 5.3", "providers": ["zai", "parasail", 7]})
    assert info.hosts == ("zai", "parasail")
    assert model_info("anthropic/claude-sonnet-4.6", {"name": "Sonnet"}).hosts == ()


# --- the window ------------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_the_picker_counts_each_models_hosts():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.engine.catalog import ModelInfo
    from sealedlore.gui.model_picker import COLUMNS, ModelCatalog, ModelPickerDialog

    QApplication.instance() or QApplication([])
    catalog = ModelCatalog()
    catalog.models = {
        "z-ai/glm-5.3": ModelInfo(id="z-ai/glm-5.3", hosts=("zai", "parasail", "nube")),
        "one/host": ModelInfo(id="one/host", hosts=("only",)),
        "anthropic/x": ModelInfo(id="anthropic/x"),
        "TEE/glm-5.3": ModelInfo(id="TEE/glm-5.3", hosts=("a", "b")),
    }
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    dialog = ModelPickerDialog(catalog, lambda: (None, "e", False))
    column = COLUMNS.index("Hosts")
    table = dialog.table
    shown = {
        table.topLevelItem(i).text(0): table.topLevelItem(i)
        for i in range(table.topLevelItemCount())
    }
    assert shown["z-ai/glm-5.3"].text(column) == "3"
    assert "parasail" in shown["z-ai/glm-5.3"].toolTip(column)
    assert shown["one/host"].text(column) == "1"
    assert shown["anthropic/x"].text(column) == ""
    assert shown["TEE/glm-5.3"].text(column) == "—", "an enclave is its own host"
    dialog.deleteLater()


def test_the_catalog_keeps_a_models_hosts(monkeypatch):
    pytest.importorskip("PySide6")
    import time

    from PySide6.QtWidgets import QApplication

    import sealedlore.gui.model_hosts as module

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        module, "fetch_model_hosts", lambda base, model: parse_model_hosts(model, DATA)
    )
    catalog = module.HostsCatalog()
    loaded: list = []
    catalog.loaded.connect(lambda base, model: loaded.append(model))
    catalog.ensure("https://nano-gpt.com/api/v1", "z-ai/glm-5.3")
    deadline = time.monotonic() + 10
    while not loaded and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert loaded == ["z-ai/glm-5.3"]
    assert catalog.hosts("https://nano-gpt.com/api/v1", "z-ai/glm-5.3").host("parasail")
    catalog.ensure("https://nano-gpt.com/api/v1", "z-ai/glm-5.3")  # kept: nothing fetched
    assert not catalog.loading("https://nano-gpt.com/api/v1", "z-ai/glm-5.3")
