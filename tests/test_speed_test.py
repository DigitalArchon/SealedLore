"""The model speed test (engine/speed_test.py, gui/speed_test.py)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.speed_test import (  # noqa: E402
    MAX_TOKENS,
    Role,
    SpeedResult,
    plan_targets,
    run_speed_test,
)
from sealedlore.engine.tokens import TokenEstimator, fallback_counter  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.speed_test import SpeedTestDialog, describe, speed_tests  # noqa: E402
from sealedlore.models.config import ModelPrice, ProviderConfig  # noqa: E402
from sealedlore.models.node import Usage  # noqa: E402
from sealedlore.providers.base import (  # noqa: E402
    ChatProvider,
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
)

MAIN = ProviderConfig(name="main", base_url="https://nano-gpt.com/api/v1", api_key="k")
LOCAL = ProviderConfig(name="private", base_url="http://localhost:11434/v1")


class Clock:
    """Time that moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Scripted(ChatProvider):
    """Yields (seconds to wait, event) pairs, moving the test's clock."""

    def __init__(self, clock: Clock, script, *, error: Exception | None = None) -> None:
        self.clock = clock
        self.script = list(script)
        self.error = error
        self.requests: list[ChatRequest] = []
        self.cancelled = False

    def build_payload(self, request: ChatRequest) -> dict:
        return {"model": request.model}

    def cancel(self) -> None:
        self.cancelled = True

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        for wait, event in self.script:
            self.clock.now += wait
            if self.cancelled:
                raise StreamCancelled()
            yield event


def estimator() -> TokenEstimator:
    return TokenEstimator(counter=fallback_counter)


# --- which models are tested --------------------------------------------------------


def test_a_model_used_in_several_places_is_tested_once():
    targets = plan_targets(
        [
            Role("Story", "big/storyteller", MAIN),
            Role("Summarisation", "", MAIN, "Story"),
            Role("Authoring", " big/storyteller ", MAIN, "Story"),
            Role("Scene", "small/reader", MAIN, "Summarisation"),
            Role("Plot", "", MAIN, "Scene"),
            Role("Lore", "", MAIN, "Scene"),
            Role("Image prompt writer", "", MAIN, "Story"),
            Role("Private", "small/reader", LOCAL),
        ]
    )
    assert [(t.model, t.roles) for t in targets] == [
        ("big/storyteller", ("Story", "Summarisation", "Authoring", "Image prompt writer")),
        ("small/reader", ("Scene", "Plot", "Lore")),
        # The same id on another endpoint is another model to wait for.
        ("small/reader", ("Private",)),
    ]
    assert targets[2].settings.base_url == LOCAL.base_url
    assert targets[0].settings.api_key == "k"


def test_a_role_with_nothing_to_fall_back_to_is_left_out():
    targets = plan_targets(
        [
            Role("Story", "", MAIN),
            Role("Scene", "", MAIN, "Story"),
            Role("Plot", "a/model", MAIN, "Scene"),
            Role("Loop", "", MAIN, "Loop"),
        ]
    )
    assert [(t.model, t.roles) for t in targets] == [("a/model", ("Plot",))]


def test_the_same_model_on_two_routes_is_two_targets():
    """Story on the subscription, the scene on a fast route: the comparison
    worth seeing. A blank role takes its fallback's model and its route."""
    fast = {"provider": {"sort": "latency", "min_quantization": "fp8"}}
    targets = plan_targets(
        [
            Role("Story", "z-ai/glm-5.3", MAIN),
            Role("Summarisation", "", MAIN, "Story"),
            Role("Scene", "z-ai/glm-5.3", MAIN, "Summarisation", fast, "Fastest first word"),
            Role("Plot", "", MAIN, "Scene"),
        ]
    )
    assert [(t.label, t.roles, t.route) for t in targets] == [
        ("z-ai/glm-5.3", ("Story", "Summarisation"), {}),
        ("z-ai/glm-5.3 · Fastest first word", ("Scene", "Plot"), fast),
    ]


def test_the_request_carries_its_route(estimator):
    from sealedlore.providers.mock import MockChatProvider

    provider = MockChatProvider(["A lamp over dark water, turning. " * 6], chunk_size=24)
    route = {"provider": {"sort": "latency", "min_quantization": "fp8"}}
    run_speed_test(provider, "z-ai/glm-5.3", estimator=estimator, route=route)
    assert provider.payloads[-1]["provider"] == route["provider"]


# --- what is measured ---------------------------------------------------------------


def pieces(first: float, gap: float, texts: list[str]) -> list:
    """Text arriving a piece at a time: the first after `first` seconds."""
    return [(first if i == 0 else gap, TextDelta(text=text)) for i, text in enumerate(texts)]


def test_the_first_word_is_the_first_text_and_the_rate_is_the_answers():
    clock = Clock()
    usage = {
        "prompt_tokens": 14,
        "completion_tokens": 260,
        "completion_tokens_details": {"reasoning_tokens": 200},
    }
    provider = Scripted(
        clock,
        [
            (0.5, ReasoningDelta(text="Let me think.")),
            # Five pieces of ten characters, half a second apart.
            *pieces(
                2.5, 0.5, ["The lamp t", "urned over", " the water", " and the d", "ark came. "]
            ),
            (
                0.5,
                StreamCompleted(
                    usage=Usage(prompt_tokens=14, completion_tokens=260), raw_usage=usage
                ),
            ),
        ],
    )
    result = run_speed_test(provider, "a/reasoner", clock=clock, estimator=estimator())

    assert result.ok
    assert result.first_reasoning == pytest.approx(0.5)
    # The wait the author sits through: the reasoning is part of it.
    assert result.first_text == pytest.approx(3.0)
    assert result.last_text == pytest.approx(5.0)
    assert result.total == pytest.approx(5.5)
    assert result.answer_seconds == pytest.approx(2.0)
    # 260 reported less 200 of reasoning.
    assert result.answer_tokens == 60 and not result.tokens_estimated
    assert result.reasoning_tokens == 200
    # The first piece came *at* the first word: the two seconds after it
    # wrote the other four fifths, 48 tokens. Never 60 over the whole 5.5.
    assert result.tokens_per_second == pytest.approx(24.0)
    request = provider.requests[0]
    assert request.params.max_tokens == MAX_TOKENS
    assert not request.params.reasoning.enabled  # never asked for
    assert len(request.messages) == 1 and request.messages[0].role == "user"


def test_each_number_says_what_it_covers():
    """A tester read "163 tokens in 6.6s" beside "360 tokens a second": the
    answer had been written in under half a second, after six of reasoning,
    and the note had divided by the whole wait."""
    clock = Clock()
    usage = {"completion_tokens": 938, "completion_tokens_details": {"reasoning_tokens": 784}}
    provider = Scripted(
        clock,
        [
            (1.1, ReasoningDelta(text="Hm.")),
            *pieces(3.4, 0.05, ["word " * 3] * 9),
            (0.0, StreamCompleted(usage=Usage(completion_tokens=938), raw_usage=usage)),
        ],
    )
    result = run_speed_test(provider, "private/a-model", clock=clock)
    first, rate, in_all, notes = describe(result)
    assert (first, in_all) == ("4.5s", "4.9s")
    # 154 tokens, eight ninths of them in the 0.4s after the first piece.
    assert rate == "342"
    assert notes.startswith("the answer: 154 tokens in 0.4s; reasoned for 3.4s (784 tokens)")
    assert "4.9s" not in notes  # the whole wait is its own column


def test_tokens_are_counted_here_when_the_endpoint_gives_none_to_trust():
    clock = Clock()
    texts = ["word " * 10] * 4
    # Reasoning was seen, and the count doesn't say how much of it was that.
    provider = Scripted(
        clock,
        [
            (1.0, ReasoningDelta(text="Hm.")),
            *pieces(1.0, 1.0, texts),
            (0.0, StreamCompleted(usage=Usage(prompt_tokens=14, completion_tokens=900))),
        ],
    )
    result = run_speed_test(provider, "a/model", clock=clock, estimator=estimator())
    assert result.tokens_estimated and result.reasoning_tokens is None
    assert result.answer_tokens == fallback_counter("".join(texts))
    assert result.tokens_per_second == pytest.approx(result.answer_tokens * 0.75 / 3.0)
    assert describe(result)[1].startswith("~")
    assert "the answer: ~" in describe(result)[3]


def test_a_cost_is_the_endpoints_or_an_estimate_or_nothing():
    clock = Clock()
    events = lambda usage: [(1.0, TextDelta(text="Dusk.")), (1.0, StreamCompleted(usage=usage))]  # noqa: E731
    free = Usage(prompt_tokens=14, completion_tokens=5, cost=0.0, cost_reported=True)
    result = run_speed_test(Scripted(clock, events(free)), "a/model", clock=clock)
    assert result.cost == 0.0 and not result.cost_estimated  # a reported zero is a cost

    unpriced = Usage(prompt_tokens=1000, completion_tokens=1000)
    result = run_speed_test(Scripted(clock, events(unpriced)), "a/model", clock=clock)
    assert result.cost is None

    prices = {"a/model": ModelPrice(prompt=1.0, completion=3.0)}
    result = run_speed_test(
        Scripted(clock, events(unpriced)), "a/model", clock=clock, prices=prices
    )
    assert result.cost == pytest.approx(0.004) and result.cost_estimated
    assert "~$0.0040" in describe(result)[3]


def test_failures_are_results_not_exceptions():
    clock = Clock()
    failed = run_speed_test(
        Scripted(clock, [], error=ProviderError("HTTP 404", body="model not found")),
        "no/model",
        clock=clock,
    )
    assert not failed.ok and "model not found" in failed.error

    usage = {"completion_tokens": 4000, "completion_tokens_details": {"reasoning_tokens": 4000}}
    thought_only = run_speed_test(
        Scripted(
            clock,
            [
                (1.0, ReasoningDelta(text="...")),
                (9.0, StreamCompleted(finish_reason="length", raw_usage=usage)),
            ],
        ),
        "a/reasoner",
        clock=clock,
    )
    assert not thought_only.ok and "no text" in thought_only.error
    assert "reasoning" in thought_only.error
    first, rate, in_all, notes = describe(thought_only)
    assert (first, rate, in_all) == ("", "", "10.0s")
    assert "reasoned for 9.0s (4000 tokens)" in notes

    stopping = Scripted(clock, [(1.0, TextDelta(text="Dusk"))])
    stopping.cancel()
    stopped = run_speed_test(stopping, "a/model", clock=clock)
    assert stopped.stopped and not stopped.ok
    assert describe(stopped) == ("", "", "", "Stopped")


def test_an_answer_that_comes_in_a_burst_has_no_rate():
    """What would be timed is the network's delivery, not the model's writing."""
    clock = Clock()
    at_once = run_speed_test(
        Scripted(clock, [(2.0, TextDelta(text="All of it.")), (0.0, StreamCompleted())]),
        "a/model",
        clock=clock,
    )
    assert at_once.ok and at_once.tokens_per_second is None
    assert describe(at_once)[:3] == ("2.0s", "all at once", "2.0s")

    few = run_speed_test(
        Scripted(
            clock, [*pieces(1.0, 1.0, ["One. ", "Two. ", "Three."]), (0.0, StreamCompleted())]
        ),
        "a/model",
        clock=clock,
    )
    assert few.ok and few.tokens_per_second is None


def test_white_space_before_the_answer_is_not_its_first_word():
    clock = Clock()
    result = run_speed_test(
        Scripted(
            clock,
            [(1.0, TextDelta(text="\n\n")), *pieces(2.0, 0.5, ["a b ", "c d ", "e f ", "g h "])]
            + [(0.0, StreamCompleted())],
        ),
        "a/model",
        clock=clock,
    )
    assert result.first_text == pytest.approx(3.0)


def test_an_encrypted_models_attestation_is_timed_apart():
    clock = Clock()

    class Sealed(Scripted):
        def attest(self):
            self.clock.now += 4.0

    provider = Sealed(
        clock,
        [(1.0, TextDelta(text="Dusk ")), (1.0, TextDelta(text="fell.")), (0.0, StreamCompleted())],
    )
    result = run_speed_test(provider, "private/a-model", clock=clock)
    assert result.attest_seconds == pytest.approx(4.0)
    assert result.first_text == pytest.approx(1.0)  # not the four seconds before
    assert result.total == pytest.approx(2.0)
    assert "enclave attested in 4.0s first" in describe(result)[3]

    # Attested beforehand, as the dialog does so that the checking (heavy
    # work on this machine) can't make other models' first words late.
    from sealedlore.engine.speed_test import attest_first

    clock = Clock()
    provider = Sealed(clock, [(1.0, TextDelta(text="Dusk.")), (0.0, StreamCompleted())])
    took = attest_first(provider, "private/a-model", clock=clock)
    assert took == pytest.approx(4.0)
    result = run_speed_test(provider, "private/a-model", clock=clock, attest=False, attested=took)
    assert clock.now == pytest.approx(105.0), "attested once, not again"
    assert result.attest_seconds == pytest.approx(4.0)
    plain = Scripted(clock, [(1.0, TextDelta(text="Dusk.")), (0.0, StreamCompleted())])
    assert attest_first(plain, "a/model", clock=clock) is None
    assert "attested" not in describe(run_speed_test(plain, "a/model", clock=clock))[3]

    class Refusing(Scripted):
        def attest(self):
            raise ProviderError("the enclave's attestation was refused")

    refused = attest_first(Refusing(clock, []), "private/a-model", clock=clock)
    assert isinstance(refused, SpeedResult) and "refused" in refused.error


# --- the dialog -----------------------------------------------------------------------


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


def test_the_table_fills_in_as_each_model_answers(app, monkeypatch):
    from sealedlore.providers.mock import MockChatProvider

    made: list[str] = []

    def make(settings: ProviderConfig):
        made.append(settings.model)
        if settings.model == "broken/model":
            return MockChatProvider(error=ProviderError("HTTP 401", body="bad key"))
        return MockChatProvider(["A lamp over dark water, turning. " * 6], chunk_size=24)

    monkeypatch.setattr(speed_tests(), "make_provider", make)
    targets = plan_targets(
        [
            Role("Story", "big/storyteller", MAIN),
            Role("Scene", "broken/model", MAIN, "Story"),
            Role("Plot", "", MAIN, "Scene"),
        ]
    )
    dialog = SpeedTestDialog(targets)
    assert [dialog.table.horizontalHeaderItem(i).text() for i in range(6)] == [
        "Model",
        "Used for",
        "First word",
        "Tokens a second",
        "In all",
        "Notes",
    ]
    wait_for(app, lambda: not dialog.stop_button.isEnabled())

    assert sorted(made) == ["big/storyteller", "broken/model"]
    cell = lambda row, column: dialog.table.item(row, column).text()  # noqa: E731
    assert cell(0, 0) == "big/storyteller" and cell(0, 1) == "Story"
    assert cell(0, 2).endswith("s") and cell(0, 4).endswith("s")
    assert cell(0, 5).startswith("the answer: ")
    assert cell(1, 1) == "Scene, Plot"
    assert cell(1, 2) == "" and "HTTP 401" in cell(1, 5)
    assert "bad key" in dialog.table.item(1, 5).toolTip()
    assert dialog.status.text() == "Done: 1 of 2 answered."
    assert isinstance(dialog.results[1], SpeedResult)
    dialog.close()
    dialog.deleteLater()


def test_the_models_are_tested_at_the_same_time(app, monkeypatch):
    """One after another, a run took as long as all of them together."""
    import threading
    import time

    from sealedlore.providers.mock import MockChatProvider

    in_flight, most = [0], [0]
    lock = threading.Lock()

    class Slow(MockChatProvider):
        def stream(self, request):
            with lock:
                in_flight[0] += 1
                most[0] = max(most[0], in_flight[0])
            try:
                yield from super().stream(request)
            finally:
                with lock:
                    in_flight[0] -= 1

    def make(settings: ProviderConfig):
        provider = Slow(["word " * 40], chunk_size=20)
        provider.delay = 0.05  # ten pieces: half a second each model
        return provider

    monkeypatch.setattr(speed_tests(), "make_provider", make)
    names = ["a/model", "b/model", "c/model", "d/model"]
    dialog = SpeedTestDialog(plan_targets([Role(name, name, MAIN) for name in names]))
    started = time.monotonic()
    wait_for(app, lambda: not dialog.stop_button.isEnabled())
    took = time.monotonic() - started
    assert most[0] == 4
    assert took < 1.5, "four half-second tests, together"
    assert dialog.status.text() == "Done: 4 of 4 answered."
    # Each result is in its own model's row, whatever order they came in.
    assert [dialog.results[row].model for row in range(4)] == names
    dialog.close()
    dialog.deleteLater()


def test_the_collector_is_paused_while_a_runs_threads_are_alive(app, monkeypatch):
    """Collecting on one of the run's threads swept up Qt objects and crashed
    in PySide's hand-back of them to the GUI thread: a segfault in the full
    test run, every time."""
    import gc

    from sealedlore.providers.mock import MockChatProvider

    seen: list[bool] = []

    class Watching(MockChatProvider):
        def stream(self, request):
            seen.append(gc.isenabled())
            yield from super().stream(request)

    monkeypatch.setattr(speed_tests(), "make_provider", lambda settings: Watching(["word " * 40]))
    assert gc.isenabled()
    dialog = SpeedTestDialog(plan_targets([Role("a", "a/model", MAIN), Role("b", "b/model", MAIN)]))
    wait_for(app, lambda: not dialog.stop_button.isEnabled())
    assert seen == [False, False]
    assert gc.isenabled(), "and back on when the run ends"
    dialog.close()
    dialog.deleteLater()


def test_closing_the_dialog_ends_the_run(app, monkeypatch):
    from sealedlore.providers.mock import MockChatProvider

    providers: list[MockChatProvider] = []

    def make(settings: ProviderConfig):
        provider = MockChatProvider(["word " * 4000], chunk_size=8)
        provider.delay = 0.05
        providers.append(provider)
        return provider

    monkeypatch.setattr(speed_tests(), "make_provider", make)
    targets = plan_targets([Role("Story", "a/model", MAIN), Role("Scene", "b/model", MAIN)])
    dialog = SpeedTestDialog(targets)
    wait_for(app, lambda: len(providers) == 2)
    run_id = dialog._run
    dialog.reject()  # the Close button; a dialog never shown ignores close()
    # Both were in flight, with minutes of answer to come: both end at once.
    wait_for(app, lambda: not speed_tests().is_running(run_id), timeout=5.0)
    assert all(provider._cancelled for provider in providers)
    dialog.deleteLater()
    speed_tests().wait()


def test_settings_tests_the_fields_as_they_stand(app, tmp_path: Path, story):
    from sealedlore.gui.settings_dialog import SettingsDialog

    window = MainWindow(root=tmp_path, use_mock=True)
    story.defaults.main_model = "this/story"
    dialog = SettingsDialog(window.config, story, window, catalog=window.catalog)
    dialog.api_key.setText("typed-key")
    dialog.model.setText("big/storyteller")
    dialog.summarization_model.setText("")
    dialog.authoring_model.setText("")
    dialog.scene_model.setText("small/reader")
    dialog.plot_model.setText("")
    dialog.lore_model.setText("")
    dialog.image_prompt_model.setText("")
    dialog.private_url.setText("http://localhost:11434/v1")
    dialog.private_model.setText("local/model")

    targets = dialog.speed_targets()
    assert [(t.model, t.roles) for t in targets] == [
        ("big/storyteller", ("Story", "Summarisation", "Authoring", "Image prompt writer")),
        ("this/story", ("This story",)),
        ("small/reader", ("Scene", "Plot", "Lore")),
        ("local/model", ("Private",)),
    ]
    assert targets[0].settings.api_key == "typed-key"
    assert targets[3].settings.base_url == "http://localhost:11434/v1"
    assert window.config.scene_model != "small/reader"  # nothing was saved

    dialog.base_url.setText("http://example.com/v1")
    dialog._test_speed()
    assert "https" in dialog.recommended_note.text()
    dialog.reject()
    window.close()
