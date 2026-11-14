"""Chutes E2EE wired in (providers/chutes_provider.py): the factory, the
plain-client rule, the keyless check, Settings, the window's attestation, the
labels and marks, and a chat moved onto it. Offline, offscreen."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.attestation_check import check_attestation  # noqa: E402
from sealedlore.engine.catalog import ModelInfo  # noqa: E402
from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.gui.model_picker import COLUMNS, ModelCatalog, ModelPickerDialog  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog, private_service_of  # noqa: E402
from sealedlore.gui.transcript import tee_tip  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    CHUTES_BASE_URL,
    Config,
    ProviderConfig,
)
from sealedlore.models.private import Attestation  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers import make_chat_provider  # noqa: E402
from sealedlore.providers.base import ProviderError, StreamCompleted  # noqa: E402
from sealedlore.providers.chutes_provider import (  # noqa: E402
    KEY_BINDING_UNCHECKED,
    ChutesE2EEProvider,
)
from sealedlore.providers.endpoints import EndpointsProvider  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.providers.openai_compat import OpenAICompatibleProvider  # noqa: E402
from sealedlore.providers.privacy import CHUTES, privacy_of  # noqa: E402
from sealedlore.providers.tee import MEASUREMENTS_LISTED, WORKLOAD_UNMEASURED  # noqa: E402
from sealedlore.storage.repository import StoryBundle  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
MODEL = "Qwen/Qwen3.8-27B-TEE"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def chutes(**changes) -> ProviderConfig:
    return ProviderConfig(name="Chutes", base_url=CHUTES_BASE_URL, api_key="cpk_t", **changes)


def config(*extra: ProviderConfig) -> Config:
    nano = ProviderConfig(name="Nano", base_url=NANO, api_key="k")
    return Config(providers=[nano, *extra], active_provider_name="Nano")


def chutes_record(instances: int = 2) -> Attestation:
    return Attestation(
        verified=True,
        level="partial",
        attested="instance",
        encrypted=True,
        service=CHUTES,
        instances=instances,
        hardware="Intel TDX",
        shortfalls=[WORKLOAD_UNMEASURED, MEASUREMENTS_LISTED],
        detail="2 of 2 instances attested; attested: 0f1e2d3c (8xpro_6000 (1.5.0))",
    )


# --- the factory and the plain-client rule -----------------------------------------------


def test_a_chutes_endpoint_gets_its_own_sealing_client_everywhere():
    assert isinstance(make_chat_provider(chutes(model=MODEL)), ChutesE2EEProvider)
    routing = EndpointsProvider(config(chutes()))
    assert isinstance(routing.provider_for(f"Chutes::{MODEL}"), ChutesE2EEProvider)
    assert isinstance(routing._plain(chutes()), ChutesE2EEProvider), "its listing too"
    routing.close()
    assert privacy_of(f"Chutes::{MODEL}", config(chutes())).service == CHUTES


def test_only_the_hosts_own_sealing_client_reaches_it():
    """Chutes' client reaches api.chutes.ai; a plain one, or another
    service's sealing client, never does."""
    assert ChutesE2EEProvider(chutes())._get_client() is not None

    class OtherSealer(OpenAICompatibleProvider):
        seals = "tinfoil"

    for client in (OpenAICompatibleProvider(chutes()), OtherSealer(chutes())):
        with pytest.raises(ProviderError, match="end-to-end encrypted endpoint"):
            client._get_client()


# --- the keyless check --------------------------------------------------------------------


@pytest.fixture
def real(monkeypatch):
    """The real capture of Chutes' public evidence (tests/test_chutes_evidence.py)."""
    import tests.test_chutes_evidence as evidence
    from sealedlore.providers import dcap, nras

    fixture = evidence.json.loads(evidence.FIXTURE.read_text(encoding="utf-8"))
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})
    yield fixture, evidence
    dcap._cache.clear()
    nras._keys.update(at=0.0, keys={})


def public_provider(real, *, key: str = "") -> ChutesE2EEProvider:
    import sealedlore.providers.chutes_provider as module
    from sealedlore.providers import dcap, nras

    fixture, evidence = real
    moment = datetime.fromtimestamp(fixture["now"], UTC)
    checks = evidence.checks_handler(fixture)
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        url = request.url
        if url.host == "llm.chutes.ai":
            listed = {"id": fixture["model"], "chute_id": fixture["chute_id"]}
            return httpx.Response(200, json={"data": [{**listed, "confidential_compute": True}]})
        if url.path == f"/chutes/{fixture['chute_id']}/evidence":
            return httpx.Response(200, json=fixture["evidence"])
        if url.path == "/servers/tdx/measurements":
            return httpx.Response(200, json=fixture["measurements"])
        return checks(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = module.ChutesE2EEProvider(
        chutes().model_copy(update={"api_key": key}),
        client=client,
        listing_client=client,
        checks_client=client,
        intel=lambda raw, session: dcap.verify(raw, session, now=moment),
        nvidia=lambda answer, nonce, session: nras.verify(
            answer, nonce, session, now=fixture["now"]
        ),
    )
    provider.asked = asked
    return provider


def test_without_a_key_the_public_evidence_is_checked_and_the_binding_said_unchecked(
    real, monkeypatch
):
    import sealedlore.providers.chutes_provider as module

    fixture, _evidence = real
    monkeypatch.setattr(module.secrets, "token_hex", lambda _n: fixture["nonce"])
    provider = public_provider(real)
    attestation = provider.attest_public(fixture["model"])
    assert attestation.instances == 3 and attestation.level == "partial"
    assert attestation.service == CHUTES and attestation.encrypted
    assert attestation.shortfalls == [
        WORKLOAD_UNMEASURED,
        MEASUREMENTS_LISTED,
        KEY_BINDING_UNCHECKED,
    ]
    assert not [r for r in provider.asked if "authorization" in r.headers], "no key went anywhere"
    assert not [r for r in provider.asked if r.url.path.startswith("/e2e/")], "no discovery"


def test_test_attestation_on_chutes_with_and_without_a_key(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        ChutesE2EEProvider, "attest", lambda self, model: seen.append("keyed") or chutes_record()
    )
    public = chutes_record(3)
    public.shortfalls.append(KEY_BINDING_UNCHECKED)
    monkeypatch.setattr(
        ChutesE2EEProvider, "attest_public", lambda self, model: seen.append("keyless") or public
    )
    keyed = check_attestation(chutes(model=MODEL, api="chutes-e2ee"))
    assert keyed.outcome == "partial" and "partially" in keyed.headline
    assert "Sealed on this computer to the key of one Chutes instance" in keyed.detail
    keyless = check_attestation(chutes(model=MODEL).model_copy(update={"api_key": ""}))
    assert keyless.outcome == "partial"
    assert "discovery and the key binding weren't checked" in keyless.headline
    assert "Checked without a key" in keyless.detail and "Sealed on" not in keyless.detail
    assert seen == ["keyed", "keyless"]


# --- Settings -----------------------------------------------------------------------------


def test_settings_adds_chutes_and_offers_it_for_private_scenes(app):
    cfg = config()
    dialog = SettingsDialog(cfg, None)
    dialog.add_chutes.click()
    assert dialog.base_url.text() == CHUTES_BASE_URL
    assert dialog.chat_api.currentData() == "chutes-e2ee"
    assert "always partial" in dialog.api_note.text()
    dialog.api_key.setText("cpk_1")
    dialog.private_service.setCurrentIndex(dialog.private_service.findData("chutes"))
    assert dialog.private_url.text() == CHUTES_BASE_URL
    dialog.private_model.setText(MODEL)
    assert dialog.private_model.privacy.text() == "🔐 E2EE · Chutes ⚠ partial"
    assert not dialog.private_attest.isHidden()
    dialog._save()
    assert cfg.endpoint_named("Chutes").api == "chutes-e2ee"
    assert (cfg.private_provider.api, private_service_of(cfg.private_provider)) == (
        "chutes-e2ee",
        "chutes",
    )


def test_the_picker_says_encrypted_by_chutes_partially(app):
    catalog = ModelCatalog()
    catalog.models = {f"Chutes::{MODEL}": ModelInfo(id=f"Chutes::{MODEL}", name=MODEL)}
    catalog._endpoint, catalog._fetched_at = "e", datetime.now(UTC)
    cfg = config(chutes())
    dialog = ModelPickerDialog(
        catalog, lambda: (None, "e", False), privacy_of=lambda m: privacy_of(m, cfg)
    )
    row = dialog.table.topLevelItem(0)
    assert row.text(COLUMNS.index("Privacy")) == "🔐 Encrypted (Chutes, partial)"
    assert "always partial" in row.toolTip(COLUMNS.index("Privacy"))
    dialog.deleteLater()


# --- marks and moves ------------------------------------------------------------------------


def test_a_chutes_passage_names_its_service_and_instance(tmp_path):
    from tests.conftest import make_exchange

    assert "instance 0f1e2d3c" in tee_tip("encrypted", None, "chutes", "0f1e2d3c")
    cfg = config(chutes())
    story = Story(title="Sealed", mode="chat", chat_keep="memory")
    story.defaults.main_model = f"Chutes::{MODEL}"
    nodes = make_exchange(1)
    story.active_leaf_id = nodes[-1].id
    session = StorySession(
        StoryBundle(story=story, nodes=nodes), cfg, MockChatProvider(), root=tmp_path
    )
    assert session.tee_chat and session.privacy(session.model).service == CHUTES
    nodes[-1].meta.model = f"Chutes::{MODEL}"
    session._last_sealed = True
    session._last_completed = StreamCompleted(sealed=True, sealed_to="0f1e2d3c-aaaa-bbbb")
    session._check_tee(nodes[-1])
    meta = nodes[-1].meta
    assert (meta.tee, meta.tee_service, meta.tee_instance) == ("encrypted", "chutes", "0f1e2d3c")
    said = session.note_model_move("private/glm-5-3", f"Chutes::{MODEL}")
    assert said and "Chutes E2EE, end-to-end encrypted" in said


def test_a_chat_on_chutes_attests_through_its_client_and_says_partial(
    app, tmp_path, monkeypatch, story, cast
):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import save_story_bundle
    from tests.conftest import make_exchange
    from tests.test_chat_gui import make_chat, wait_for

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    try:
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        monkeypatch.setattr(
            window.config,
            "providers",
            [ProviderConfig(name="n", base_url=NANO, api_key="k"), chutes()],
        )
        monkeypatch.setattr(window.config, "active_provider_name", "n")
        monkeypatch.setattr(window, "_attest_chat", lambda: None)
        make_chat(window, monkeypatch, model=f"Chutes::{MODEL}", keep="memory")

        attested: list[str] = []
        monkeypatch.setattr(ChutesE2EEProvider, "check_offered", lambda self, model: None)
        monkeypatch.setattr(
            ChutesE2EEProvider,
            "attest",
            lambda self, model: attested.append(model) or chutes_record(),
        )
        window.use_mock = False
        window._discard_provider()
        assert isinstance(window._sealed_chat_provider(), ChutesE2EEProvider)
        settings = window.config.endpoint_for(window.session.model).model_copy(
            update={"model": MODEL}
        )
        window._start_attestation(settings)
        wait_for(app, lambda: "Chutes" in window._tee_state)
        assert attested == [MODEL]
        assert "🔐 E2EE · Chutes ⚠ partial" in window._private_model_text()
        window._refresh_model_button()
        tip = window.model_button.toolTip()
        assert "one Chutes instance" in tip and "workload holding the key unmeasured" in tip
        window.use_mock = True
        window._discard_provider()
    finally:
        window._chat_allows_close = lambda: True
        window.close()
