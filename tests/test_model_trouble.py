"""Help → Model trouble (engine/host_check.py, gui/model_trouble.py,
gui/bad_hosts.py, the route dialog's marked-bad hosts)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.engine.host_check import (  # noqa: E402
    Candidate,
    HostSummary,
    billed_by,
    check_messages,
    choices,
    default_picks,
    estimate,
    pinned,
    verdict,
)
from sealedlore.engine.speed_test import SpeedResult, run_speed_test  # noqa: E402
from sealedlore.gui.bad_hosts import bad_hosts  # noqa: E402
from sealedlore.gui.model_hosts import hosts_catalog  # noqa: E402
from sealedlore.gui.model_trouble import (  # noqa: E402
    ModelTroubleDialog,
    TroubleContext,
    TroubleRole,
)
from sealedlore.gui.route_dialog import RouteDialog  # noqa: E402
from sealedlore.messages import ContentPart, PromptMessage  # noqa: E402
from sealedlore.models.config import Config, ModelPrice, ProviderConfig  # noqa: E402
from sealedlore.models.generation import ReasoningConfig  # noqa: E402
from sealedlore.models.node import Usage  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.providers.model_hosts import Host, ModelHosts  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
GLM = "z-ai/glm-5.3"


def host(id_: str, price: float, quant: str = "fp8", privacy: str = "zdr") -> Host:
    return Host(
        id=id_,
        name=id_.title(),
        quantization=quant,
        privacy=privacy,
        input_price=price,
        output_price=price * 3,
    )


HOSTS = ModelHosts(
    model=GLM,
    supported=True,
    official="zai",
    hosts=(
        host("zai", 1.47),
        host("morph", 0.75),
        host("sail", 0.21),
        host("cheapfp4", 0.10, quant="fp4"),
        host("novita", 0.82, privacy="no_training"),
        host("scx", 0.88, quant="bf16"),
    ),
)


def result(first: float, reasoning: int = 60, *, error: str | None = None, **kw) -> SpeedResult:
    if error:
        return SpeedResult(model=GLM, error=error)
    return SpeedResult(
        model=GLM,
        first_text=first,
        last_text=first + 3,
        total=first + 3,
        answer_tokens=250,
        reasoning_tokens=reasoning,
        text="Mae looked up from the ledger.",
        **kw,
    )


def summary(key: str, firsts, reasoning, asked: ReasoningConfig | None = None) -> HostSummary:
    candidate = Candidate(key, key or "current", pinned(key) if key else {})
    runs = tuple(result(f, r) for f, r in zip(firsts, reasoning, strict=True))
    return HostSummary(candidate, runs, asked or ReasoningConfig())


# --- which hosts ---------------------------------------------------------------------


def test_the_current_route_comes_first_and_hosts_marked_bad_are_left_out():
    found = choices(HOSTS, {}, bad={"morph"})
    assert found[0].current and found[0].label == "Your route now: NanoGPT's own routing"
    assert "morph" not in [c.key for c in found]
    assert [c for c in found if c.official][0].label == "Zai (the model maker's own)"
    assert found[1].route == {"provider": {"only": ["zai"], "allow_fallbacks": False}}


def test_the_default_hosts_keep_nothing_the_makers_own_then_the_cheapest_at_fp8_or_better():
    picks = default_picks(choices(HOSTS, {}, bad=set()), count=3)
    assert picks == ["", "zai", "sail", "morph"], "cheapfp4 is below the floor"
    # Always the hosts that keep nothing, when there are any (the author).
    picks = default_picks(choices(HOSTS, {}, bad=set()), count=5)
    assert "novita" not in picks and picks[-1] == "scx", "Novita keeps prompts"
    keeping = ModelHosts(
        model=GLM,
        official="zai",
        hosts=(host("zai", 1.47, privacy="no_training"), host("sail", 0.21)),
    )
    assert default_picks(choices(keeping, {}, bad=set())) == ["", "sail"], "not the maker's"
    # No host keeps nothing: the maker's own first, then the cheapest.
    none_private = ModelHosts(
        model=GLM,
        official="zai",
        hosts=(
            host("zai", 1.47, privacy="logs_training"),
            host("novita", 0.82, privacy="no_training"),
        ),
    )
    assert default_picks(choices(none_private, {}, bad=set())) == ["", "zai", "novita"]


def test_the_built_in_prompt_is_a_real_turn_that_no_run_answers_from_anothers_cache():
    first, second = check_messages(1), check_messages(2)
    assert [m.role for m in first] == ["system", "user", "assistant", "user"]
    assert first[0].text != second[0].text and first[1:] == second[1:]
    assert "You are the engine of a collaborative prose roleplay" in first[0].text
    assert "REMEMBER:" in first[-1].text


def test_the_estimate_counts_each_host_at_its_own_price_and_says_what_it_couldnt():
    candidates = choices(HOSTS, {}, bad=set())[:3]  # current, zai, morph
    likely, most, known = estimate(candidates, 2000, 3, listed=ModelPrice(prompt=1, completion=3))
    zai = 3 * (2000 * 1.47 + 700 * 4.41) / 1e6
    morph = 3 * (2000 * 0.75 + 700 * 2.25) / 1e6
    listed = 3 * (2000 * 1 + 700 * 3) / 1e6
    assert likely == pytest.approx(zai + morph + listed) and most > likely and known
    assert not estimate(candidates, 2000, 3)[2], "the current route's price unknown"


def test_the_bill_says_whether_the_host_served_it():
    zai = HOSTS.host("zai")
    usage = Usage(prompt_tokens=2000, completion_tokens=300, cost_reported=True)
    usage.cost = (2000 * 1.47 + 300 * 4.41) / 1e6
    assert billed_by(result(2.0, usage=usage), zai) is True
    other = usage.model_copy(update={"cost": usage.cost * 2})
    assert billed_by(result(2.0, usage=other), zai) is False
    free = usage.model_copy(update={"cost": 0.0})
    assert billed_by(result(2.0, usage=free), zai) is False, "the subscription served it"
    assert billed_by(result(2.0), zai) is None


# --- what the runs show ----------------------------------------------------------------


def test_a_route_that_reasons_far_more_than_the_other_hosts_is_the_host():
    """Morph on GLM 5.3: ~270 tokens a read where the maker's host took ~60."""
    found = verdict(
        [
            summary("", [6.0, 6.2, 12.0], [860, 300, 1300]),
            summary("zai", [4.8, 4.9, 7.7], [66, 60, 170]),
            summary("sail", [3.6, 3.7, 5.5], [41, 57, 180]),
        ]
    )
    assert found.kind == "overthinks" and "the host is the problem" in found.text
    assert found.best == "sail"


def test_a_route_that_is_just_slow_is_an_overloaded_host():
    found = verdict(
        [
            summary("", [28.0, 30.0, 32.0], [60, 70, 80]),
            summary("zai", [4.8, 5.0, 7.7], [66, 60, 70]),
            summary("sail", [4.0, 4.5, 5.0], [50, 60, 70]),
        ]
    )
    assert found.kind == "slow" and "slow or overloaded" in found.text


def test_when_every_host_reasons_at_length_it_is_the_model():
    """Kimi K3 with nothing asked: 600-940 tokens a read on every host."""
    found = verdict(
        [
            summary("", [20.0, 22.0, 25.0], [900, 800, 1000]),
            summary("novita", [23.0, 24.0, 30.0], [940, 900, 1000]),
            summary("sail", [10.0, 11.0, 15.0], [767, 800, 900]),
        ]
    )
    assert found.kind == "model" and found.best is None


def test_when_the_route_is_as_good_as_the_rest_it_is_the_prompt():
    found = verdict(
        [
            summary("", [5.0, 5.1, 5.3], [60, 70, 80]),
            summary("zai", [4.8, 5.0, 5.2], [66, 60, 70]),
        ]
    )
    assert found.kind == "prompt" and "your story's prompt" in found.text


def test_a_host_that_ignores_the_level_asked_is_never_the_one_offered():
    """SCX on Kimi K3 ignored "low": 774 tokens a read."""
    low = ReasoningConfig(enabled=True, effort="low")
    fast_but_deaf = summary("scx", [3.0, 3.0, 3.0], [774, 800, 900], low)
    honest = summary("sail", [4.3, 4.5, 7.5], [56, 60, 194], low)
    assert fast_but_deaf.ignores_level and not honest.ignores_level
    found = verdict([summary("", [30.0, 30, 30], [2000, 2000, 2000], low), fast_but_deaf, honest])
    assert found.best == "sail"


def test_a_route_that_failed_every_run_says_so():
    failing = HostSummary(Candidate("", "current", {}), (result(0, error="HTTP 503"),) * 2)
    found = verdict([failing, summary("zai", [5.0], [60])])
    assert found.kind == "failing" and "HTTP 503" in found.text and found.best == "zai"


def test_the_speed_test_can_send_a_prompt_of_its_own_and_keeps_the_answer():
    provider = MockChatProvider(["Mae looked up."], reasoning="Hm. " * 50)
    messages = [PromptMessage(role="user", parts=(ContentPart(text="Go on."),))]
    found = run_speed_test(provider, "m", messages=messages, max_tokens=1234)
    assert provider.requests[0].messages == messages
    assert provider.requests[0].params.max_tokens == 1234
    assert found.text == "Mae looked up." and found.reasoning_estimate == 50


# --- the dialog ------------------------------------------------------------------------


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeRunner(QObject):
    """The speed test runner's signals, answered from a table by host."""

    done = Signal(int, int, object)
    finished = Signal(int)

    def __init__(self, answers: dict[str, list[SpeedResult]]):
        super().__init__()
        self.answers = answers
        self.targets = []
        self.stopped = False

    def start(self, targets, prices=None) -> int:
        self.targets = list(targets)
        return 7

    def stop(self, run_id: int) -> None:
        self.stopped = True

    def answer(self) -> None:
        for row, target in enumerate(self.targets):
            key, run = target.key.split("\t")
            self.done.emit(7, row, self.answers[key][int(run) - 1])
        self.finished.emit(7)


@pytest.fixture
def config(tmp_path: Path):
    cfg = Config()
    saved = []
    bad_hosts().attach(cfg, lambda: saved.append(True))
    bad_hosts().keep = True
    hosts_catalog()._found[(NANO, GLM)] = (time.monotonic(), HOSTS)
    yield cfg
    bad_hosts().forget_session()


def context(**changes) -> TroubleContext:
    used = []
    fields = dict(
        endpoint=ProviderConfig(name="n", base_url=NANO, api_key="k", model=GLM),
        roles=[TroubleRole("scene", "Scene", GLM, {}, ReasoningConfig())],
        use_host=lambda role, model, h: used.append((role, model, h.id)),
    )
    fields.update(changes)
    found = TroubleContext(**fields)
    found.used = used  # type: ignore[attr-defined]
    return found


OVERTHINKING = {
    "": [result(6.0, 860), result(6.2, 300), result(12.0, 1300)],
    "zai": [result(4.8, 66), result(4.9, 60), result(7.7, 170)],
    "sail": [result(3.6, 41), result(3.7, 57), result(5.5, 180)],
    "morph": [result(6.0, 900), result(6.0, 800), result(6.0, 850)],
    "scx": [result(5.0, 60), result(5.0, 60), result(5.0, 60)],
}


def test_the_slow_check_names_the_host_and_can_route_to_the_best_or_mark_one_bad(app, config):
    runner = FakeRunner(OVERTHINKING)
    ctx = context()
    dialog = ModelTroubleDialog(ctx, "slow", runner=runner)
    picked = [c.key for c in dialog.picked()]
    assert picked == ["", "zai", "morph", "sail", "scx"], "only hosts that keep nothing"
    # Ticked first, the current route at the top; any column sorts.
    shown = [dialog.hosts.topLevelItem(i) for i in range(dialog.hosts.topLevelItemCount())]
    ticks = [item.checkState(0) == Qt.Checked for item in shown]
    assert shown[0].data(0, Qt.UserRole) == "" and ticks == sorted(ticks, reverse=True)
    dialog.hosts.sortByColumn(3, Qt.AscendingOrder)
    prices = [dialog.hosts.topLevelItem(i).text(3) for i in range(len(shown))]
    assert prices[-1] == "", "the current route has no price of its own: last"
    assert prices[:-1] == sorted(prices[:-1], key=float), "by price, as numbers"
    assert "about $" in dialog.cost.text() and "pay-as-you-go" in dialog.cost.text()
    dialog.run()
    assert len(runner.targets) == 5 * 3
    assert runner.targets[0].route == {} and runner.targets[3].route == {
        "provider": {"only": ["zai"], "allow_fallbacks": False}
    }
    assert runner.targets[0].messages[0].text.startswith("Check 1.")
    assert runner.targets[1].messages[0].text.startswith("Check 2.")
    runner.answers["novita"] = runner.answers["scx"]
    runner.answer()
    assert "the host is the problem" in dialog.verdict.text()
    assert dialog.selected().key == "sail", "the best is selected"
    dialog.use_selected()
    assert ctx.used == [("scene", GLM, "sail")]

    # Mark Morph bad: it is kept in the config, and next time left out.
    item = next(
        dialog.table.topLevelItem(i)
        for i in range(dialog.table.topLevelItemCount())
        if dialog.table.topLevelItem(i).data(0, Qt.UserRole) == "morph"
    )
    dialog.table.setCurrentItem(item)
    dialog.mark_selected_bad("reasoned 5x the others")
    assert config.bad_hosts[GLM]["morph"].reason == "reasoned 5x the others"
    again = ModelTroubleDialog(context(), "slow", runner=FakeRunner({}))
    assert "morph" not in [c.key for c in again._candidates]
    assert "1 marked bad are left out" in again.limits.text()


def test_the_storys_own_prompt_goes_only_after_the_warning_is_accepted(app, config):
    prompt = (PromptMessage(role="user", parts=(ContentPart(text="The story so far."),)),)
    runner = FakeRunner(OVERTHINKING)
    dialog = ModelTroubleDialog(
        context(story_prompt=prompt, story_prompt_tokens=31_000, memory_only=True),
        "slow",
        runner=runner,
    )
    dialog.own.setChecked(True)
    assert dialog.warning.isVisibleTo(dialog) and "31,000 tokens" in dialog.warning.text()
    assert "kept in memory only" in dialog.warning.text()
    assert not dialog.run_button.isEnabled()
    dialog.understood.setChecked(True)
    assert dialog.run_button.isEnabled()
    dialog.run()
    assert all(target.messages == prompt for target in runner.targets)


def test_without_a_prompt_to_send_the_writing_check_says_why(app, config):
    dialog = ModelTroubleDialog(
        context(story_prompt_why="A private scene is open: nothing from it goes anywhere."),
        "worse",
        runner=FakeRunner({}),
    )
    assert "private scene" in dialog.warning.text() and not dialog.run_button.isEnabled()


def test_the_writing_check_shows_the_replies_blind_until_revealed(app, config):
    prompt = (PromptMessage(role="user", parts=(ContentPart(text="The story so far."),)),)
    answers = {key: [result(3.0, 50)] for key in ("", "zai", "morph", "sail")}
    answers["sail"] = [SpeedResult(model=GLM, first_text=2.0, text="Sail's reply.")]
    runner = FakeRunner(answers)
    ctx = context(story_prompt=prompt, story_prompt_tokens=30_000)
    dialog = ModelTroubleDialog(ctx, "worse", runner=runner)
    # Hosts that keep nothing, since the story's own prompt is going out.
    assert [c.key for c in dialog.picked()] == ["", "zai", "morph", "sail"]
    dialog.understood.setChecked(True)
    dialog.run()
    assert len(runner.targets) == 4, "one reply each"
    runner.answer()
    titles = [dialog.replies.tabText(i) for i in range(dialog.replies.count())]
    assert titles == ["Reply A", "Reply B", "Reply C", "Reply D"]
    sail = next(
        i
        for i in range(dialog.replies.count())
        if dialog.replies.widget(i).toPlainText() == "Sail's reply."
    )
    dialog.replies.setCurrentIndex(sail)
    dialog.use_selected()
    assert ctx.used == [("scene", GLM, "sail")]
    assert "Sail" in dialog.replies.tabText(sail), "revealed once chosen"


def test_a_private_model_is_offered_only_its_equally_private_alternatives(app, config):
    runner = FakeRunner({})
    ctx = context(
        roles=[TroubleRole("story", "Private model", "TEE/glm-5.3")],
        private_ids=["TEE/glm-5.3", "private/glm-5-3", "TEE/kimi-k3"],
        story_prompt=(PromptMessage(role="user", parts=(ContentPart(text="secret"),)),),
    )
    dialog = ModelTroubleDialog(ctx, "slow", runner=runner)
    assert "no host to switch to" in dialog.limits.text()
    assert [c.label for c in dialog.picked()] == ["Your model now: TEE/glm-5.3", "private/glm-5-3"]
    assert not dialog.own.isEnabled()
    dialog.run()
    assert {t.model for t in runner.targets} == {"TEE/glm-5.3", "private/glm-5-3"}
    assert all(t.route == {} for t in runner.targets)
    assert all("secret" not in t.messages[-1].text for t in runner.targets)


def test_a_host_marked_bad_shows_greyed_in_the_route_dialog_and_can_be_allowed_again(app, config):
    bad_hosts().mark(GLM, "morph", "reasoned too long")
    dialog = RouteDialog(NANO, GLM, None)
    labels = [dialog.table.topLevelItem(i).text(0) for i in range(dialog.table.topLevelItemCount())]
    assert "Morph · marked bad" in labels
    assert dialog.host.findData("morph") < 0, "it can't be chosen"
    row = next(
        dialog.table.topLevelItem(i)
        for i in range(dialog.table.topLevelItemCount())
        if dialog.table.topLevelItem(i).data(0, Qt.UserRole + 1) == "morph"
    )
    dialog.table.setCurrentItem(row)
    assert dialog.allow_button.isEnabled()
    dialog.allow_button.click()
    assert GLM not in config.bad_hosts
    assert dialog.host.findData("morph") >= 0
    dialog.done(QDialog.Rejected)


def test_a_memory_only_chats_marks_stay_in_memory(app, config):
    bad_hosts().keep = False
    bad_hosts().mark(GLM, "morph", "slow")
    assert config.bad_hosts == {} and "morph" in bad_hosts().marked(GLM)
    bad_hosts().forget_session()
    assert bad_hosts().marked(GLM) == {}


# --- the window ------------------------------------------------------------------------


@pytest.fixture
def window(app, tmp_path: Path, story, cast, monkeypatch):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import StoryBundle, save_story_bundle
    from tests.conftest import make_exchange

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    bundle.story.defaults.main_model = GLM
    save_story_bundle(bundle, root=tmp_path)
    monkeypatch.setattr(ModelTroubleDialog, "exec", lambda self: 0)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


def test_the_help_menu_opens_both_checks(window):
    help_menu = next(
        a.menu() for a in window.menuBar().actions() if a.text().replace("&", "") == "Help"
    )
    trouble = next(
        a.menu() for a in help_menu.actions() if a.text().replace("&", "") == "Model trouble"
    )
    texts = [a.text() for a in trouble.actions()]
    assert texts == [
        "The model takes too long before it writes…",
        "The model's writing has got worse…",
    ]
    dialog = window.open_model_trouble("slow")
    assert dialog is not None and dialog.current_role().model == GLM
    assert "Story model" in dialog.current_role().label


def test_the_storys_prompt_is_offered_once_there_is_one_and_never_from_a_private_scene(window):
    from sealedlore.engine.prompt import TurnRequest
    from sealedlore.models.private import PrivateSpan

    window.session.last_prompt = None
    context = window._trouble_context()
    assert context.story_prompt is None and "Send a turn first" in context.story_prompt_why
    window.session.assemble(
        TurnRequest(
            speaker_id="char-serrik", user_text="Go.", controlled_character_id="char-serrik"
        )
    )
    context = window._trouble_context()
    assert context.story_prompt is not None and context.story_prompt_tokens > 0

    window.session.story.private_spans.append(PrivateSpan(model="TEE/glm-5.3"))
    context = window._trouble_context()
    assert context.story_prompt is None and "private scene" in context.story_prompt_why
    assert [(r.label, r.model) for r in context.roles] == [("Private model", "TEE/glm-5.3")]
    window.session.story.private_spans.clear()  # or closing the window asks about the scene


def test_using_a_host_makes_it_the_roles_route(window):
    from sealedlore.storage.repository import load_config

    window._use_host("scene", GLM, HOSTS.host("sail"))
    route = window.config.model_routes["scene"]
    assert (route.priority, route.host, route.host_model, route.fp8) == ("host", "sail", GLM, True)
    assert load_config(root=window.root).model_routes["scene"].host == "sail"


def test_a_passage_caught_reasoning_at_length_says_so_in_the_transcript(window):
    from PySide6.QtWidgets import QLabel

    passage = next(n for n in window.session.nodes if n.id == "a1")
    passage.meta.reasoning_caught = ["moonshotai/kimi-k3"]
    window.reload_transcript()
    widget = window.transcript.message_widget("a1")
    notes = [
        label.text() for label in widget.findChildren(QLabel) if label.objectName() == "messageNote"
    ]
    assert len(notes) == 1 and "moonshotai/kimi-k3 reasoned at length" in notes[0]
