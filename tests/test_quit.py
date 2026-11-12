"""Quitting never waits long on a fetch nobody needs any more (gui/workers.py).

The window's close waited up to 35 seconds each, one after another, on the
GUI thread, for the model listing, attestation tests, a speed test and the
hosts fetch, none of which (but the speed test) could be cancelled; and a
QThread still running after its wait was destroyed, which Qt treats as
fatal. They run on daemon threads now, and what they bring back once their
owner is gone, or the app is quitting, is dropped.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

import shiboken6  # noqa: E402
from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore import threads  # noqa: E402
from sealedlore.engine.attestation_check import AttestationCheck  # noqa: E402
from sealedlore.engine.speed_test import SpeedResult, SpeedTarget  # noqa: E402
from sealedlore.gui import attest_check, model_hosts, workers  # noqa: E402
from sealedlore.gui.attest_check import attestation_tests  # noqa: E402
from sealedlore.gui.collector import install_collector_pause  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.model_hosts import hosts_catalog  # noqa: E402
from sealedlore.gui.model_picker import ModelCatalog  # noqa: E402
from sealedlore.gui.speed_test import speed_tests  # noqa: E402
from sealedlore.models.config import ProviderConfig  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def gate(app):
    """What the hung fetches wait for; let go after the test, whatever
    happened, and their daemon threads seen out."""
    event = threading.Event()
    yield event
    event.set()
    pump(app, lambda: threads.alive() == 0, timeout=20)


class _Hanging:
    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate

    def fetch_model_prices(self) -> dict:
        self._gate.wait(30)
        return {}


def test_closing_waits_for_no_listing_attestation_hosts_or_speed_test(
    app, tmp_path: Path, monkeypatch, gate
):
    window = MainWindow(root=tmp_path, use_mock=True)
    window.catalog.ensure(lambda: (_Hanging(gate), "https://example.org", False))
    checked: list[str] = []

    def hanging_check(settings):
        gate.wait(30)
        checked.append(settings.model)
        return AttestationCheck("full", "Attested")

    monkeypatch.setattr(attest_check, "check_attestation", hanging_check)
    attestation_tests().start(ProviderConfig(name="test", model="TEE/some-model"))
    monkeypatch.setattr(model_hosts, "fetch_model_hosts", lambda *_args: gate.wait(30))
    hosts_catalog().ensure("https://nano-gpt.com/api/v1", "some/model")

    class Slow(MockChatProvider):
        def stream(self, request):
            gate.wait(30)
            yield from super().stream(request)

    monkeypatch.setattr(speed_tests(), "make_provider", lambda _settings: Slow(["word"]))
    run = speed_tests().start(
        [SpeedTarget(ProviderConfig(name="test", model="some/model"), ("Story",))]
    )
    assert window.catalog.loading and speed_tests().is_running(run)

    began = time.monotonic()
    window.close()
    assert time.monotonic() - began < 2.0, "quitting waited on a fetch nobody needs"
    assert not window.isVisible()

    # They end later; nothing of them is lost to a crash, and the window's
    # catalog takes its (empty) answer as ever.
    gate.set()
    pump(app, lambda: not window.catalog.loading and not speed_tests().is_running(run))
    pump(app, lambda: threads.alive() == 0)
    assert checked == ["TEE/some-model"]


def test_what_comes_back_for_an_owner_deleted_meanwhile_is_dropped(app, gate):
    catalog = ModelCatalog()
    changed: list[int] = []
    catalog.changed.connect(lambda: changed.append(1))
    catalog.ensure(lambda: (_Hanging(gate), "https://example.org", False))
    assert changed == [1]
    shiboken6.delete(catalog)  # its dialog closed while it fetched
    gate.set()
    pump(app, lambda: threads.alive() == 0)
    app.processEvents()
    assert changed == [1]


def test_once_the_app_begins_to_quit_nothing_more_is_handed_back(app):
    owner = QObject()
    called: list[int] = []
    try:
        workers.close_gate()
        assert not workers.post(owner, called.append, 1)
        thread = workers.run_detached(
            owner, lambda: 2, lambda result, _error: called.append(result)
        )
        thread.join(5)
        app.processEvents()
        assert called == []
    finally:
        workers.install(app)
        install_collector_pause(app)
    assert workers.post(owner, called.append, 3)
    pump(app, lambda: called == [3])


def test_a_detached_jobs_failure_comes_back_in_words(app):
    owner = QObject()
    got: list = []

    def broken():
        raise ValueError("the listing was empty")

    workers.run_detached(owner, broken, lambda result, error: got.append((result, error)))
    pump(app, lambda: got)
    assert got[0][0] is None and "the listing was empty" in got[0][1]


def test_a_speed_tests_rows_still_come_back_in_order(app, monkeypatch):
    monkeypatch.setattr(speed_tests(), "make_provider", lambda _settings: MockChatProvider(["x"]))
    began: list[int] = []
    done: list[SpeedResult] = []
    tests = speed_tests()
    tests.began.connect(lambda _run, row: began.append(row))
    tests.done.connect(lambda _run, _row, result: done.append(result))
    run = tests.start(
        [
            SpeedTarget(ProviderConfig(name="test", model="a/model"), ("Story",)),
            SpeedTarget(ProviderConfig(name="test", model="b/model"), ("Scene",)),
        ]
    )
    pump(app, lambda: not tests.is_running(run))
    assert began == [0, 1]
    assert sorted(result.model for result in done) == ["a/model", "b/model"]
