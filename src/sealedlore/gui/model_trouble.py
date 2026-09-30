"""Help → Model trouble: the two checks for a model that has started to
misbehave, and what to do about it (engine/host_check.py decides; this shows).

- **Slow to write**: the built-in test prompt (or the story's own) sent to the
  role's current route and a few other hosts, several times each. The report
  says whether the route overthinks, is just slow, or whether every host is
  the same (the model or the prompt).
- **Writing got worse**: the story's last prompt sent once to a few hosts,
  the replies side by side with the hosts hidden until one is picked.

Either way the author can use a host for the role (a route, models/route.py)
or mark one bad (gui/bad_hosts.py). Nothing is sent before Run, and the
estimate says what it may cost: pinned hosts are billed pay-as-you-go even on
the subscription.

A private scene's prompt, or a TEE chat's, never goes anywhere: their models
usually have one host, so the check offers their equally private alternatives
on the built-in prompt instead. A memory-only chat's prompt may be sent, after
a warning that it then exists on those hosts' servers.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.host_check import (
    DEFAULT_OTHERS,
    DEFAULT_RUNS,
    MAX_TOKENS,
    Candidate,
    HostSummary,
    Mode,
    check_messages,
    choices,
    default_picks,
    estimate,
    verdict,
)
from sealedlore.engine.speed_test import SpeedResult, SpeedTarget
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.gui.bad_hosts import bad_hosts
from sealedlore.gui.model_hosts import hosts_catalog
from sealedlore.gui.speed_test import SpeedTests, speed_tests
from sealedlore.messages import PromptMessage
from sealedlore.models.config import ModelPrice, ProviderConfig
from sealedlore.models.generation import ReasoningConfig
from sealedlore.providers.model_hosts import Host, ModelHosts
from sealedlore.providers.private_catalog import private_alternatives
from sealedlore.providers.tee import is_tee

TITLES = {
    "slow": "The model takes too long before it writes",
    "worse": "The model's writing has got worse",
}
INTROS = {
    "slow": (
        "A reply can be slow to start because the host running the model is overloaded, or "
        "because it has the model think far longer than it should, or because the model or "
        "your prompt simply needs it. This sends the same prompt to your current route and a "
        "few other hosts, several times each, and says which it is."
    ),
    "worse": (
        "Some hosts serve a model at lower precision than they claim, or another model "
        "altogether. This sends your story's last prompt to a few hosts, once each, and shows "
        "the replies side by side. The hosts stay hidden until you pick the one you like best."
    ),
}
# How the menu names each role (models/route.ROUTE_ROLES).
TROUBLE_ROLE_NAMES = {
    "story": "Story model",
    "summarisation": "Summarisation",
    "authoring": "Authoring",
    "scene": "Scene",
    "plot": "Plot",
    "lore": "Lore",
    "image_prompt": "Image prompt writer",
}
COLUMNS = ("Host", "First word", "Reasoning", "Tokens/s", "Answered", "Cost", "Notes")
HOST_COLUMNS = ("Try", "Precision", "Keeps prompts?", "In $/M", "Out $/M")
KEEPS = {"zdr": "no", "no_training": "yes, not for training", "logs_training": "yes, for training"}


@dataclass(frozen=True)
class TroubleRole:
    """One use of a model, as the window has it now."""

    role: str
    label: str
    model: str
    route: dict = field(default_factory=dict)
    reasoning: ReasoningConfig = field(default_factory=ReasoningConfig)


@dataclass
class TroubleContext:
    endpoint: ProviderConfig | None
    roles: list[TroubleRole]
    on_nanogpt: bool = True
    prices: Mapping[str, ModelPrice] = field(default_factory=dict)
    # What the storyteller was last sent, when it may go to other hosts; else why not.
    story_prompt: tuple[PromptMessage, ...] | None = None
    story_prompt_tokens: int = 0
    story_prompt_why: str = "Send a turn first: this uses what the storyteller was last sent."
    memory_only: bool = False
    private_ids: Sequence[str] = ()
    # Set the role's route to one host: (role, model, host).
    use_host: Callable[[str, str, Host], None] | None = None


# What a row sorts by in a column, when not its text: a number (None last).
SORT_ROLE = Qt.UserRole + 1


class _SortRow(QTreeWidgetItem):
    """Sorts by the number behind a figure, unknowns last; the host list's
    first column puts the current route first, then the ticked hosts, so the
    author sees at once what will be tried (the author, Sept 30 2026)."""

    def _key(self, column: int) -> tuple:
        if column == 0:
            current = self.data(0, Qt.UserRole) == ""
            ticked = self.checkState(0) == Qt.Checked
            return (0 if current else 1 if ticked else 2, self.text(0).lower())
        value = self.data(column, SORT_ROLE)
        if value is not None or self.text(column) == "":
            return (value is None, value or 0.0)
        return (False, self.text(column).lower())

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self._key(column), other._key(column)  # type: ignore[attr-defined]
        try:
            return mine < theirs
        except TypeError:  # a number against a word: compare as text
            return str(mine) < str(theirs)


def _money(value: float) -> str:
    return f"${value:.3f}" if value < 1 else f"${value:.2f}"


def _secs(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}s"


class ModelTroubleDialog(QDialog):
    def __init__(
        self,
        context: TroubleContext,
        mode: Mode,
        parent: QWidget | None = None,
        *,
        runner: SpeedTests | None = None,
    ) -> None:
        super().__init__(parent)
        self.context, self.mode = context, mode
        self.runner = runner or speed_tests()
        self.setWindowTitle(TITLES[mode])
        self.resize(900, 720)
        self._hosts: ModelHosts | None = None
        self._candidates: list[Candidate] = []
        self._run_id: int | None = None
        self._results: dict[str, list[SpeedResult]] = {}
        self._targets: list[SpeedTarget] = []
        self._expected = 0
        self._summaries: list[HostSummary] = []
        self._revealed = mode == "slow"
        self._reply_order: list[HostSummary] = []
        self._prompt_tokens_builtin = sum(
            TokenEstimator().raw_count(message.text) for message in check_messages()
        )

        intro = QLabel(INTROS[mode])
        intro.setWordWrap(True)

        self.role = QComboBox()
        roles = context.roles if mode == "slow" else context.roles[:1]
        for index, role in enumerate(roles):
            self.role.addItem(f"{role.label}: {role.model}", index)
        self.role.currentIndexChanged.connect(lambda _i: self._load())

        self.limits = QLabel()
        self.limits.setObjectName("hintLabel")
        self.limits.setWordWrap(True)

        self.hosts = QTreeWidget()
        self.hosts.setColumnCount(len(HOST_COLUMNS))
        self.hosts.setHeaderLabels(list(HOST_COLUMNS))
        self.hosts.setRootIsDecorated(False)
        self.hosts.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.hosts.setMaximumHeight(190)
        self.hosts.setSortingEnabled(True)
        self.hosts.header().setSortIndicatorShown(True)
        self.hosts.itemChanged.connect(lambda *_: self._sync())

        self.builtin = QRadioButton(
            "The built-in test prompt (about 2,000 tokens, nothing of yours)"
        )
        self.own = QRadioButton("My story's last prompt")
        prompt_group = QButtonGroup(self)
        prompt_group.addButton(self.builtin)
        prompt_group.addButton(self.own)
        self.builtin.setChecked(mode == "slow")
        self.own.setChecked(mode == "worse")
        self.own.setEnabled(context.story_prompt is not None)
        if context.story_prompt is None:
            self.own.setToolTip(context.story_prompt_why)
        self.builtin.setVisible(mode == "slow")
        self.own.setVisible(mode == "slow")
        self.builtin.toggled.connect(lambda _on: self._sync())
        self.warning = QLabel()
        self.warning.setObjectName("cannotFix")
        self.warning.setWordWrap(True)
        self.understood = QCheckBox("I understand: send my prompt to the hosts ticked above")
        self.understood.toggled.connect(lambda _on: self._sync())

        self.runs = QSpinBox()
        self.runs.setRange(1, 5)
        self.runs.setValue(DEFAULT_RUNS if mode == "slow" else 1)
        self.runs.setPrefix("Runs per host: ")
        self.runs.setVisible(mode == "slow")
        self.runs.valueChanged.connect(lambda _v: self._sync())

        self.cost = QLabel()
        self.cost.setObjectName("hintLabel")
        self.cost.setWordWrap(True)

        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        self.progress = QLabel()
        self.progress.setObjectName("hintLabel")
        run_row = QHBoxLayout()
        run_row.addWidget(self.runs)
        run_row.addStretch(1)
        run_row.addWidget(self.progress)
        run_row.addWidget(self.run_button)
        run_row.addWidget(self.stop_button)

        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        self.verdict.setTextFormat(Qt.PlainText)
        self.table = QTreeWidget()
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHeaderLabels(list(COLUMNS))
        self.table.setRootIsDecorated(False)
        self.table.header().setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        self.table.header().setSortIndicatorShown(True)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        self.table.setVisible(mode == "slow")
        self.replies = QTabWidget()
        self.replies.setVisible(mode == "worse")
        self.replies.currentChanged.connect(lambda _i: self._sync_actions())

        self.use_button = QPushButton("Use this host")
        self.use_button.setToolTip("Send this role's calls to this host from now on (a route)")
        self.use_button.clicked.connect(self.use_selected)
        self.bad_button = QPushButton("Mark as bad…")
        self.bad_button.setToolTip(
            "Never offer this host for this model when choosing a route by hand, until "
            "allowed again in the route dialog"
        )
        self.bad_button.clicked.connect(lambda: self.mark_selected_bad())
        self.reveal_button = QPushButton("Reveal hosts")
        self.reveal_button.clicked.connect(self.reveal)
        self.reveal_button.setVisible(mode == "worse")
        actions = QHBoxLayout()
        actions.addWidget(self.reveal_button)
        actions.addStretch(1)
        actions.addWidget(self.bad_button)
        actions.addWidget(self.use_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.role)
        layout.addWidget(self.limits)
        layout.addWidget(self.hosts)
        layout.addWidget(self.builtin)
        layout.addWidget(self.own)
        layout.addWidget(self.warning)
        layout.addWidget(self.understood)
        layout.addWidget(self.cost)
        layout.addLayout(run_row)
        layout.addWidget(self.verdict)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.replies, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)

        catalog = hosts_catalog()
        catalog.loaded.connect(self._on_hosts)
        catalog.failed.connect(self._on_hosts_failed)
        self.runner.done.connect(self._on_done)
        self.runner.finished.connect(self._on_finished)
        self._load()

    def done(self, result: int) -> None:
        if self._run_id is not None:
            self.runner.stop(self._run_id)
        for signal, slot in (
            (hosts_catalog().loaded, self._on_hosts),
            (hosts_catalog().failed, self._on_hosts_failed),
            (self.runner.done, self._on_done),
            (self.runner.finished, self._on_finished),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)

    # --- the role and its hosts -----------------------------------------------

    def current_role(self) -> TroubleRole:
        return self.context.roles[self.role.currentData() or 0]

    def _private_model(self) -> bool:
        return is_tee(self.current_role().model)

    def _load(self) -> None:
        role = self.current_role()
        endpoint = self.context.endpoint
        self._hosts = None
        if self._private_model():
            others = private_alternatives(role.model, self.context.private_ids)
            self.limits.setText(
                "This is a TEE or end-to-end encrypted model. It usually has one host, and its "
                "prompts never go anywhere else, so there is no host to switch to. "
                + (
                    "Its equally private alternatives are listed below: the check sends them "
                    "the built-in test prompt only."
                    if others
                    else "There is no equally private alternative to try; to try other models "
                    "or hosts, the story would have to be played outside private mode."
                )
            )
        elif endpoint is None or not self.context.on_nanogpt:
            self.limits.setText(
                "Choosing a host needs NanoGPT: this endpoint serves the model its own way. "
                "The check can still time your current route."
            )
        else:
            self.limits.setText("Loading the hosts for this model…")
            self._hosts = hosts_catalog().hosts(endpoint.base_url, role.model)
            if self._hosts is None:
                hosts_catalog().ensure(endpoint.base_url, role.model)
        self._fill_hosts()

    def _on_hosts(self, base_url: str, model: str) -> None:
        endpoint = self.context.endpoint
        if endpoint is not None and (base_url, model) == (
            endpoint.base_url,
            self.current_role().model,
        ):
            self._hosts = hosts_catalog().hosts(base_url, model)
            self._fill_hosts()

    def _on_hosts_failed(self, base_url: str, model: str, why: str) -> None:
        if model == self.current_role().model:
            self.limits.setText(f"Couldn't list the hosts ({why}). Only your route can be timed.")

    def _fill_hosts(self) -> None:
        role = self.current_role()
        if self._private_model():
            alternatives = private_alternatives(role.model, self.context.private_ids)
            self._candidates = [Candidate("", f"Your model now: {role.model}", {})] + [
                Candidate(f"model:{alt}", alt, {}, model=alt) for alt in alternatives
            ]
            picks = [c.key for c in self._candidates]
        else:
            bad = bad_hosts().marked(role.model)
            self._candidates = choices(self._hosts, role.route, bad=bad)
            picks = default_picks(
                self._candidates, count=DEFAULT_OTHERS if self.mode == "slow" else 3
            )
            if self._hosts is not None:
                shown = len(self._candidates) - 1
                hidden = f" {len(bad)} marked bad are left out." if bad else ""
                self.limits.setText(
                    f"{shown} other hosts serve {role.model}.{hidden} The current route is always "
                    "tried; tick the hosts to compare it with."
                    if shown
                    else f"Only one host serves {role.model} on NanoGPT: there is no other to "
                    "try. The check can still time it."
                )
        self.hosts.blockSignals(True)
        self.hosts.setSortingEnabled(False)
        self.hosts.clear()
        for candidate in self._candidates:
            host = candidate.host
            item = _SortRow(
                [
                    candidate.label,
                    (host.quantization or "unknown") if host else "",
                    KEEPS.get(host.privacy or "", "unknown") if host else "",
                    f"{host.input_price:.2f}" if host and host.input_price is not None else "",
                    f"{host.output_price:.2f}" if host and host.output_price is not None else "",
                ]
            )
            item.setData(0, Qt.UserRole, candidate.key)
            if host is not None:
                item.setData(3, SORT_ROLE, host.input_price)
                item.setData(4, SORT_ROLE, host.output_price)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if candidate.key in picks else Qt.Unchecked)
            if candidate.current:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEnabled)
            self.hosts.addTopLevelItem(item)
        # Ticked first, the current route at the top, until the author
        # sorts by another column.
        self.hosts.setSortingEnabled(True)
        self.hosts.sortByColumn(0, Qt.AscendingOrder)
        self.hosts.blockSignals(False)
        for column in range(1, len(HOST_COLUMNS)):
            self.hosts.resizeColumnToContents(column)
        self._sync()

    def picked(self) -> list[Candidate]:
        keys = {
            self.hosts.topLevelItem(i).data(0, Qt.UserRole)
            for i in range(self.hosts.topLevelItemCount())
            if self.hosts.topLevelItem(i).checkState(0) == Qt.Checked
        }
        return [c for c in self._candidates if c.key in keys or c.current]

    # --- the prompt and what it costs -------------------------------------------

    def _uses_own_prompt(self) -> bool:
        return self.own.isChecked() and not self._private_model()

    def _sync(self) -> None:
        own = self._uses_own_prompt()
        picked = self.picked()
        needs_prompt = self.mode == "worse" and not self._private_model()
        if needs_prompt and self.context.story_prompt is None:
            self.warning.setText(self.context.story_prompt_why)
        elif own:
            hosts = len(picked)
            memory = (
                " This chat is kept in memory only, on this computer. These hosts are not "
                "bound by that: sending the prompt puts the whole conversation it carries on "
                f"{hosts} hosts' servers, under each host's own terms."
                if self.context.memory_only
                else ""
            )
            self.warning.setText(
                f"Your story's last prompt (about {self.context.story_prompt_tokens:,} tokens: "
                f"the world, the cast, the recent story and your turn) goes to {hosts} "
                "hosts, each under its own privacy terms. Those that keep prompts (see the "
                f"list) may keep it.{memory}"
            )
        self.warning.setVisible(own or (needs_prompt and self.context.story_prompt is None))
        self.understood.setVisible(own)
        if self._private_model():
            self.builtin.setChecked(True)
            self.own.setEnabled(False)
            self.own.setToolTip("A TEE or encrypted model's prompt never goes to another host.")
        tokens = self.context.story_prompt_tokens if own else self._prompt_tokens_builtin
        listed = self.context.prices.get(self.current_role().model)
        likely, most, known = estimate(
            [c for c in picked if c.model is None], tokens, self.runs.value(), listed=listed
        )
        note = "" if known else " Some prices aren't known and aren't counted."
        self.cost.setText(
            f"{len(picked)} hosts × {self.runs.value()} "
            f"{'run' if self.runs.value() == 1 else 'runs'}: about {_money(likely)}, at most "
            f"{_money(most)}. A host picked here is billed pay-as-you-go, even for a model your "
            f"subscription includes.{note}"
        )
        ready = (
            len(picked) >= (1 if self.mode == "slow" else 2)
            and (not own or self.understood.isChecked())
            and (not needs_prompt or self.context.story_prompt is not None)
        )
        running = self._run_id is not None
        self.run_button.setEnabled(ready and not running)
        self.stop_button.setEnabled(running)
        self._sync_actions()

    def _messages(self, run: int) -> tuple[PromptMessage, ...]:
        if self._uses_own_prompt() and self.context.story_prompt is not None:
            return self.context.story_prompt
        return check_messages(run)

    # --- running ------------------------------------------------------------------

    def targets(self) -> list[SpeedTarget]:
        role = self.current_role()
        endpoint = self.context.endpoint
        if endpoint is None:
            return []
        found = []
        for candidate in self.picked():
            model = candidate.model or role.model
            for run in range(1, self.runs.value() + 1):
                found.append(
                    SpeedTarget(
                        settings=endpoint.model_copy(update={"model": model}),
                        roles=(candidate.label,),
                        route=dict(candidate.route),
                        # An alternative model is asked for its own lowest level.
                        reasoning=role.reasoning if candidate.model is None else ReasoningConfig(),
                        messages=self._messages(run),
                        max_tokens=MAX_TOKENS,
                        key=f"{candidate.key}\t{run}",
                    )
                )
        return found

    def run(self) -> None:
        targets = self.targets()
        if not targets or self._run_id is not None:
            return
        self._results = {}
        self._expected = len(targets)
        self._targets = targets
        self.table.clear()
        self.replies.clear()
        self.verdict.clear()
        self.progress.setText(f"Running: 0 of {len(targets)} replies…")
        self._run_id = self.runner.start(targets, self.context.prices)
        self._sync()

    def stop(self) -> None:
        if self._run_id is not None:
            self.runner.stop(self._run_id)

    def _on_done(self, run_id: int, row: int, result: SpeedResult) -> None:
        if run_id != self._run_id:
            return
        key = self._targets[row].key.split("\t")[0]
        self._results.setdefault(key, []).append(result)
        got = sum(len(found) for found in self._results.values())
        self.progress.setText(f"Running: {got} of {self._expected} replies…")

    def _on_finished(self, run_id: int) -> None:
        if run_id != self._run_id:
            return
        self._run_id = None
        role = self.current_role()
        self._summaries = [
            HostSummary(c, tuple(self._results.get(c.key, ())), asked=role.reasoning)
            for c in self.picked()
            if self._results.get(c.key)
        ]
        spent = sum(s.cost for s in self._summaries)
        self.progress.setText(f"Done. These calls cost {_money(spent)}.")
        if self.mode == "slow":
            self._show_table()
        else:
            self._show_replies()
        self._sync()

    # --- results ------------------------------------------------------------------

    def _notes(self, summary: HostSummary) -> str:
        notes = []
        if summary.errors:
            notes.append(f"{len(summary.errors)} failed: {summary.errors[0].splitlines()[0]}")
        if summary.ignores_level:
            notes.append("reasoned at length though asked for less")
        if summary.billed is False:
            notes.append("billed at another price: another host may have served it")
        return "; ".join(notes)

    def _show_table(self) -> None:
        found = verdict(self._summaries)
        self.verdict.setText(found.text)
        self.table.setSortingEnabled(False)
        self.table.clear()
        best_item = None
        for summary in self._summaries:
            reasoning = summary.reasoning
            worst = summary.worst_reasoning
            item = _SortRow(
                [
                    summary.candidate.label,
                    f"{_secs(summary.first_word)} (worst {_secs(summary.worst_first_word)})"
                    if summary.first_word is not None
                    else "",
                    f"{reasoning:,.0f} (most {worst:,})" if reasoning is not None else "",
                    f"{summary.rate:.0f}" if summary.rate else "",
                    f"{len(summary.answered)} of {len(summary.results)}",
                    _money(summary.cost),
                    self._notes(summary),
                ]
            )
            item.setData(0, Qt.UserRole, summary.candidate.key)
            for column, value in (
                (1, summary.first_word),
                (2, reasoning),
                (3, summary.rate),
                (4, len(summary.answered)),
                (5, summary.cost),
            ):
                item.setData(column, SORT_ROLE, value)
            self.table.addTopLevelItem(item)
            if summary.candidate.key == found.best:
                best_item = item
        # Quickest first word first, until the author sorts by another column.
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.AscendingOrder)
        for column in range(len(COLUMNS) - 1):
            self.table.resizeColumnToContents(column)
        if best_item is not None:
            self.table.setCurrentItem(best_item)

    def _show_replies(self) -> None:
        """Shuffled, so the tab's place says nothing about which host it is."""
        self._reply_order = list(self._summaries)
        random.shuffle(self._reply_order)
        self.replies.clear()
        for index, summary in enumerate(self._reply_order):
            view = QPlainTextEdit()
            view.setReadOnly(True)
            answered = summary.answered
            view.setPlainText(
                answered[0].text
                if answered
                else "No reply: " + (summary.errors[0] if summary.errors else "nothing came back")
            )
            self.replies.addTab(view, self._reply_title(index, summary))
        self.verdict.setText(
            "Read them, then pick the one you like best. Reveal hosts shows which is which."
        )

    def _reply_title(self, index: int, summary: HostSummary) -> str:
        letter = chr(ord("A") + index)
        return f"{letter}: {summary.candidate.label}" if self._revealed else f"Reply {letter}"

    def reveal(self) -> None:
        self._revealed = True
        for index, summary in enumerate(self._reply_order):
            self.replies.setTabText(index, self._reply_title(index, summary))
        self._sync_actions()

    def selected(self) -> Candidate | None:
        if self.mode == "slow":
            item = self.table.currentItem()
            key = item.data(0, Qt.UserRole) if item is not None else None
        else:
            index = self.replies.currentIndex()
            key = (
                self._reply_order[index].candidate.key
                if 0 <= index < len(self._reply_order)
                else None
            )
        return (
            next((c for c in self._candidates if c.key == key), None) if key is not None else None
        )

    def _sync_actions(self) -> None:
        chosen = self.selected()
        host = chosen.host if chosen is not None else None
        self.use_button.setEnabled(host is not None and self.context.use_host is not None)
        self.bad_button.setEnabled(host is not None)
        self.reveal_button.setEnabled(bool(self._reply_order) and not self._revealed)

    def use_selected(self) -> None:
        chosen = self.selected()
        if chosen is None or chosen.host is None or self.context.use_host is None:
            return
        role = self.current_role()
        self.context.use_host(role.role, role.model, chosen.host)
        self.reveal()
        self.progress.setText(f"{role.label} now goes to {chosen.host.name}.")

    def mark_selected_bad(self, reason: str | None = None) -> None:
        chosen = self.selected()
        if chosen is None or chosen.host is None:
            return
        if reason is None:
            summary = next((s for s in self._summaries if s.candidate.key == chosen.key), None)
            suggested = self._notes(summary) if summary is not None else ""
            reason, accepted = QInputDialog.getText(
                self,
                "Mark as bad",
                f"Why is {chosen.host.name} bad for {self.current_role().model}? "
                "(shown beside it in the route dialog)",
                text=suggested or f"checked {date.today().isoformat()}",
            )
            if not accepted:
                return
        bad_hosts().mark(self.current_role().model, chosen.host.id, reason)
        self.reveal()
        self.progress.setText(
            f"{chosen.host.name} is marked bad for {self.current_role().model}: it won't be "
            "offered when you choose a host (the route dialog can allow it again)."
        )
