"""The main window.

Holds the docks together and owns the turn lifecycle. Anything with real logic
lives elsewhere: the composer, the cast and scene panels, the inspector, and
the engine behind them. Per-turn overrides (length, outcome mode, dice skill
and difficulty) live in the composer's second row, per §10.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QFile, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore import __version__
from sealedlore.engine.archival import chapter_number, chapter_numbers, turns_in
from sealedlore.engine.authoring import describe, stale_voicing_notes
from sealedlore.engine.branches import next_branch_name, unique_branch_name
from sealedlore.engine.chronicle import armed, format_clock
from sealedlore.engine.export import render_markdown
from sealedlore.engine.plot_md import PLOT_SUFFIX, ParsedPlotFile, parse_plot_markdown
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_edits import texts_in_force
from sealedlore.engine.routing import HostPrice, config_route
from sealedlore.engine.scene_update import SceneProposal
from sealedlore.engine.session import (
    SessionNotice,
    StorySession,
    TurnShape,
)
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.engine.usage import usage_report
from sealedlore.gui.attest_check import attestation_tests
from sealedlore.gui.banners import OverBudgetBanner, StalenessBanner
from sealedlore.gui.branch_bar import BranchBar, opening_words
from sealedlore.gui.cast_panel import CastPanel
from sealedlore.gui.composer import DEFAULT_SUFFIX, Composer
from sealedlore.gui.fields import PageStack, install_wheel_guard
from sealedlore.gui.generate_dialog import GenerateDialog
from sealedlore.gui.inspector import ContextInspector
from sealedlore.gui.lore_panel import LorePanel
from sealedlore.gui.model_hosts import hosts_catalog
from sealedlore.gui.model_picker import ModelCatalog, pick_model, pick_model_and_route
from sealedlore.gui.plot_editor import PlotEditorWindow
from sealedlore.gui.plot_panel import ClockDialog, PlotPanel
from sealedlore.gui.prompt_editor import PromptEditorDialog
from sealedlore.gui.ref_images import load_plot_pictures
from sealedlore.gui.review_dialog import ReviewRequestDialog, ReviewResultDialog
from sealedlore.gui.scene_panel import ScenePanel
from sealedlore.gui.session_line import SessionLine
from sealedlore.gui.settings_dialog import SettingsDialog
from sealedlore.gui.setup_dialog import SetupDialog
from sealedlore.gui.speed_test import speed_tests
from sealedlore.gui.start_dialog import StartDialog
from sealedlore.gui.status import NoticeStatusBar, StatusStrip
from sealedlore.gui.stories import StoryListPanel
from sealedlore.gui.story_map import StoryMap
from sealedlore.gui.style_panel import StylePanel
from sealedlore.gui.summaries_panel import SummariesPanel
from sealedlore.gui.tab_rows import TabRows
from sealedlore.gui.transcript import (
    ChapterCard,
    TranscriptView,
    ViewToggle,
    kind_for,
    speaker_label_for,
)
from sealedlore.gui.usage_dialog import UsageDialog
from sealedlore.gui.welcome import setup_missing, welcome_html
from sealedlore.gui.window_chat import ChatWindow
from sealedlore.gui.window_find import FindWindow
from sealedlore.gui.window_images import ImagesWindow
from sealedlore.gui.window_private import PrivateWindow
from sealedlore.gui.window_text import TextSizeWindow
from sealedlore.gui.worker import GenerationWorker
from sealedlore.gui.wrap_row import WrapRow
from sealedlore.ids import utc_now_iso
from sealedlore.models.authoring import SettingsChange, SettingsReview
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ChatProvider, UnconfiguredProvider
from sealedlore.providers.embeddings import EmbeddingBackend, OpenAICompatibleEmbeddings
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.storage.archive import (
    ARCHIVE_FORMAT,
    ARCHIVE_SUFFIX,
    ArchiveError,
    file_format,
    read_archive,
    restore_archive,
    write_archive,
)
from sealedlore.storage.images import reference_files
from sealedlore.storage.paths import data_home, sample_stories, samples_dir, story_dir
from sealedlore.storage.repository import (
    StoryBundle,
    copy_story,
    load_config_or_recover,
    load_story_bundle,
    read_api_log,
    read_api_log_since,
    save_config,
    save_story_bundle,
)
from sealedlore.storage.repository import delete_story as remove_story
from sealedlore.storage.scenario import (
    SCENARIO_SUFFIX,
    ScenarioError,
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from sealedlore.tree import path_to


def _how_it_starts(story: Story) -> dict:
    """What Setup changes about how a playthrough begins (not the description)."""
    return story.setup.model_dump(exclude={"description"})


# A chat's kept messages past this share of the budget are pointed out.
KEPT_WARNING_SHARE = 0.2


class MainWindow(ChatWindow, FindWindow, ImagesWindow, PrivateWindow, TextSizeWindow, QMainWindow):
    generation_requested = Signal()

    def __init__(self, *, root: Path | None = None, use_mock: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("SealedLore")
        self.resize(1280, 860)
        # The wheel scrolls pages, not the combo under the pointer (fields.py).
        install_wheel_guard(QApplication.instance())

        self.root = root
        self.use_mock = use_mock
        # A settings file that can't be read must not keep the app from
        # starting: it is set aside and the author is told (below).
        self.config, config_notice = load_config_or_recover(root=root)
        self.session: StorySession | None = None
        self._provider: ChatProvider | None = None
        self._embeddings: EmbeddingBackend | None = None
        self.estimator = TokenEstimator(factors=self.config.token_correction_factors)

        self._thread: QThread | None = None
        self._worker: GenerationWorker | None = None
        self._busy = False
        self._plot_editor: PlotEditorWindow | None = None
        # Closing mid-generation waits for the worker's thread to finish.
        self._close_when_idle = False
        # The passage is in and only background reads remain (PassageDone):
        # the composer is open, and a turn sent now waits in `_queued`.
        self._background = False
        self._queued: Callable[[], None] | None = None
        self._queued_text: tuple[str, str | None] | None = None
        # What the plot holds back from the Cast and Lore tabs, as last shown.
        self._shown_hidden: set[str] = set()
        # The progress dialog of a background job (see _start's busy_text).
        self._busy_dialog: QProgressDialog | None = None
        # The endpoint's model list, for every Browse… and the model button.
        self.catalog = ModelCatalog(self)
        self._asking = False
        self._asides_before = 0
        # A competence suggestion waiting for the author, set by the worker job.
        self._pending_tiers: tuple[str, dict] | None = None
        self._pending_review: SettingsReview | None = None
        # The complaint and model of the review in flight, for its findings.
        self._review_request: tuple[str, str] = ("", "")
        self._pending_scene: SceneProposal | None = None
        # Archival and summary rebuilds ride the same worker as generation but
        # produce no transcript message and no generation result.
        self._streaming_job = True
        self._staleness_dismissed = False
        self._counted_summaries: set[tuple[str, str]] = set()

        # The transcript shows the story as written, or as the storyteller is sent it.
        self._storyteller_view = False
        # A message to scroll to once the job is done (an older passage
        # retaken in place: the story after it stays, and so should the eye).
        self._focus_after_job: str | None = None
        # The API log as read so far (story id, byte offset, entries): the log
        # is append-only, and parsing all of it after every turn grew with it.
        self._log_cache: tuple[str | None, int, list[dict]] = (None, 0, [])

        self._build_ui()
        self._build_actions()
        # tiktoken reads its vocabulary on first use (~0.4s): do it now, off
        # the GUI thread, rather than while the first story opens.
        threading.Thread(target=lambda: self.estimator.raw_count("warm up"), daemon=True).start()
        self.stories.refresh()
        # The same no-story state closing a story leaves: the transcript says
        # how to begin, and no Plot page stands for a plot there isn't.
        self._refresh_plot_panel()
        self.reload_transcript()
        self._update_controls()
        if config_notice:
            self.statusBar().showMessage(config_notice, 30000)

    # --- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        self.transcript = TranscriptView()
        self.transcript.regenerate_requested.connect(self._regenerate_node)
        self.transcript.take_back_requested.connect(self._take_back)
        self.transcript.review_mark_requested.connect(self._toggle_review_mark)
        self.transcript.edit_committed.connect(self._edit_node)
        self.transcript.delete_requested.connect(self._delete_node)
        self.transcript.keep_full_requested.connect(self._toggle_keep_full)
        self.transcript.delete_aside_requested.connect(self._delete_aside)
        self.transcript.reroll_requested.connect(self.reroll)
        self.transcript.rewrite_committed.connect(self._rewrite_from)
        self.transcript.default_branch_name = self._default_branch_name
        self.transcript.variant_requested.connect(self._switch_variant)

        self.over_budget = OverBudgetBanner()
        self.over_budget.archive_requested.connect(self.archive_to_target)
        self.over_budget.automatic_requested.connect(self._archive_automatically)
        self.staleness = StalenessBanner()
        self.staleness.rebuild_requested.connect(self._rebuild_stale)
        self.staleness.dismissed.connect(self._dismiss_staleness)

        self.composer = Composer()
        self.composer.send_requested.connect(self.send_turn)
        self.composer.stop_requested.connect(self.stop_generation)
        self.composer.regenerate_requested.connect(self.regenerate)
        self.composer.held_character_changed.connect(self._on_held_changed)

        centre_layout = QVBoxLayout()
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(10)
        # How the open story or chat is set up, always in view (read-only).
        self.session_line = SessionLine()
        centre_layout.addWidget(self.session_line)
        centre_layout.addWidget(self.staleness)
        centre_layout.addWidget(self.over_budget)
        self.view_toggle = ViewToggle()
        self.view_toggle.changed.connect(self.set_storyteller_view)
        # Which branch is being read, always in view (the left dock's list of
        # forks meant nothing to the author: most were takes).
        self.branch_bar = BranchBar()
        self.branch_bar.switch_requested.connect(self._switch_branch)
        self.branch_bar.rename_requested.connect(self._rename_branch)
        self.branch_bar.delete_requested.connect(self._delete_branch)
        # Wraps under a large text size rather than widening the window.
        self._build_find()
        self.top_row = WrapRow()
        self.top_row.add_group([self.view_toggle])
        self.top_row.add_group([self.find_button])
        self.top_row.add_group([self.branch_bar], right=True, whole=True)
        centre_layout.addLayout(self.top_row)
        centre_layout.addWidget(self.find_bar)
        # The story map shares the transcript's place (Map, on the branch bar).
        self.story_map = StoryMap()
        self.story_map.back_requested.connect(lambda: self.show_map(False))
        self.story_map.select_requested.connect(self._select_from_map)
        self.story_map.open_requested.connect(self._open_from_map)
        # A message picked on the map, to scroll to when the story is back.
        self._map_focus: str | None = None
        self.story_map.rename_requested.connect(self._rename_branch)
        self.story_map.delete_requested.connect(self._delete_branch)
        self.branch_bar.map_toggled.connect(self.show_map)
        self.centre_stack = PageStack()
        self.centre_stack.addWidget(self.transcript)
        self.centre_stack.addWidget(self.story_map)
        centre_layout.addWidget(self.centre_stack, 1)
        centre_layout.addWidget(self.composer)

        centre = QWidget()
        centre.setObjectName("centre")
        centre.setLayout(centre_layout)
        self.setCentralWidget(centre)

        self.stories = StoryListPanel(root=self.root)
        self.stories.story_activated.connect(self.open_story)
        self.stories.new_story_requested.connect(self.new_story)
        self.stories.duplicate_settings_requested.connect(self.duplicate_story_settings)
        self.stories.duplicate_story_requested.connect(self.duplicate_entire_story)
        self.stories.delete_requested.connect(self.delete_story)
        self.stories.open_folder_requested.connect(self._open_story_folder)
        left = QDockWidget("Stories", self)
        left.setObjectName("storiesDock")
        left.setWidget(self.stories)
        left.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, left)

        self.cast_panel = CastPanel(self._story_pictures)
        self.cast_panel.changed.connect(self._on_cast_changed)
        self.cast_panel.renamed.connect(self._on_character_renamed)
        self.cast_panel.promote_requested.connect(self._promote_character)
        self.cast_panel.demote_requested.connect(self._demote_character)
        self.cast_panel.accept_requested.connect(self._accept_suggestion)
        self.cast_panel.dismiss_requested.connect(self._dismiss_suggestion)
        self.cast_panel.scan_requested.connect(self.scan_for_characters)
        self.cast_panel.infer_requested.connect(self.infer_competence)
        self.scene_panel = ScenePanel()
        self.scene_panel.changed.connect(self._on_scene_changed)
        self.scene_panel.update_requested.connect(self.update_scene_from_story)
        self.scene_panel.undo_requested.connect(self._undo_scene_update)
        self.plot_panel = PlotPanel()
        self.plot_panel.clock_edit_requested.connect(self.set_story_clock)
        self.plot_panel.fact_changed.connect(self._on_fact_changed)
        self.plot_panel.undo_requested.connect(self._undo_chronicle_update)
        self.plot_panel.spoilers_toggled.connect(self._on_spoilers_toggled)
        self.plot_panel.event_marked.connect(self._on_event_marked)
        self.plot_panel.bring_in_requested.connect(self._on_bring_in)
        self.lore_panel = LorePanel(self._story_pictures)
        self.lore_panel.changed.connect(self._on_lore_changed)
        self.lore_panel.reembed_requested.connect(self.reembed_lore)
        self.style_panel = StylePanel()
        self.style_panel.changed.connect(self._on_style_changed)
        self.summaries_panel = SummariesPanel()
        self.summaries_panel.archive_requested.connect(self.archive_now)
        self.summaries_panel.rebuild_requested.connect(self._rebuild_summary)
        self.summaries_panel.chapter_chosen.connect(self._scroll_to_node)
        self.summaries_panel.edit_committed.connect(self._save_summary)
        self.inspector = ContextInspector()
        self._init_images()
        self._init_private()

        # Scene first, and the page a story opens on: playtesting found the
        # scene read wrong too often to go unwatched, and Cast is for changes
        # made by hand.
        self.right_tabs = TabRows()
        self.right_tabs.addTab(self.scene_panel, "Scene")
        self.right_tabs.addTab(self.cast_panel, "Cast")
        self.right_tabs.addTab(self.plot_panel, "Plot")
        self.right_tabs.addTab(self.lore_panel, "Lore")
        self.right_tabs.addTab(self.style_panel, "Style")
        self.right_tabs.addTab(self.summaries_panel, "Story so far")
        self.right_tabs.addTab(self.images_panel, "Images")
        self.right_tabs.addTab(self.inspector, "Prompt")
        self.right_tabs.set_empty(self._no_story_page())
        right = QDockWidget("Inspector", self)
        right.setObjectName("inspectorDock")
        right.setWidget(self.right_tabs)
        right.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, right)
        self.resizeDocks([left, right], [240, 420], Qt.Horizontal)

        self.status_strip = StatusStrip()
        bar = NoticeStatusBar()
        self.setStatusBar(bar)
        bar.route_status_tips(self)
        # The storyteller's model, one click from another (see choose_story_model).
        self.model_button = QToolButton()
        self.model_button.setObjectName("modelButton")
        self.model_button.setAutoRaise(True)
        self.model_button.clicked.connect(self.choose_story_model)
        bar.add_left(self.model_button)
        # The plot's clock, for stories with one: one click from a correction.
        self.clock_button = QToolButton()
        self.clock_button.setObjectName("clockButton")
        self.clock_button.setAutoRaise(True)
        self.clock_button.setToolTip("Story time. Click to correct it.")
        self.clock_button.clicked.connect(self.set_story_clock)
        self.clock_button.hide()
        bar.add_left(self.clock_button)
        bar.add_left(self.status_strip.budget_label)
        bar.add_left(self.status_strip.bar, 1)
        bar.addPermanentWidget(self.status_strip.lore_label)
        bar.addPermanentWidget(self.status_strip.archive_label)
        bar.addPermanentWidget(self.status_strip.cache_label)
        bar.addPermanentWidget(self.status_strip.cost_label)
        bar.addPermanentWidget(self.status_strip.pictures_label)

    def _build_actions(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        new_action = QAction("&New story…", self)
        new_action.setShortcut(QKeySequence.New)
        new_action.triggered.connect(self.new_story)
        file_menu.addAction(new_action)

        from_premise = QAction("New story from a &premise…", self)
        from_premise.setStatusTip("Describe a story; the model drafts its world, cast and lore")
        from_premise.triggered.connect(self.new_story_from_premise)
        file_menu.addAction(from_premise)
        self._chat_actions(file_menu)
        # One import for every kind of file (see _import_path): there were two
        # that each took either file, and a tester reasonably concluded that
        # the one they hadn't used had lost their story.
        import_action = QAction("&Import…", self)
        import_action.setShortcut(QKeySequence.Open)
        import_action.setStatusTip(
            "A new story from a scenario, a story backup or a plot file (.md)"
        )
        import_action.triggered.connect(self.import_file)
        file_menu.addAction(import_action)
        # The sample stories are inside the AppImage, at a mount point that
        # changes every launch: Import… could never find them there. Listed
        # afresh whenever the menu opens, so a sample added later just appears.
        self.samples_menu = file_menu.addMenu("New story from a &sample")
        self.samples_menu.menuAction().setStatusTip(
            "Start a new story from one of the samples that come with SealedLore"
        )
        self.samples_menu.aboutToShow.connect(self._fill_samples_menu)
        self._fill_samples_menu()
        editor_action = QAction("Plot file &editor…", self)
        editor_action.setStatusTip(
            "Write a plot file — world, cast, places, facts and events — with the format kept right"
        )
        editor_action.triggered.connect(self.open_plot_editor)
        file_menu.addAction(editor_action)

        save_action = QAction("&Save", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.save_story)
        file_menu.addAction(save_action)
        # Leaving a story used to take opening another or making a new one.
        self.close_story_action = QAction("&Close story", self)
        self.close_story_action.setShortcut(QKeySequence.Close)
        self.close_story_action.setStatusTip("Back to the start page")
        self.close_story_action.triggered.connect(self.close_story)
        file_menu.addAction(self.close_story_action)

        file_menu.addSeparator()
        self.settings_action = QAction("Se&ttings…", self)
        # Explicit: QKeySequence.Preferences is empty on GNOME.
        self.settings_action.setShortcut(QKeySequence("Ctrl+,"))
        self.settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(self.settings_action)
        self.prompts_action = QAction("&Prompts (advanced)…", self)
        self.prompts_action.setStatusTip(
            "See and change every instruction SealedLore sends to its models"
        )
        self.prompts_action.triggered.connect(self.open_prompt_editor)
        file_menu.addAction(self.prompts_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        story_menu = self.menuBar().addMenu("S&tory")
        self.setup_action = QAction("Set&up…", self)
        self.setup_action.triggered.connect(self.open_setup)
        story_menu.addAction(self.setup_action)
        self.start_action = QAction("&Start story…", self)
        self.start_action.triggered.connect(self.start_story)
        story_menu.addAction(self.start_action)
        self.restart_action = QAction("&Restart as new playthrough…", self)
        self.restart_action.triggered.connect(lambda: self.restart_story(review=True))
        story_menu.addAction(self.restart_action)

        story_menu.addSeparator()
        self.review_action = QAction("Re&view settings…", self)
        self.review_action.setStatusTip(
            "Say what isn't working; the model proposes changes to the settings"
        )
        self.review_action.triggered.connect(self.review_settings)
        story_menu.addAction(self.review_action)
        self.last_review_action = QAction("&Last review…", self)
        self.last_review_action.setStatusTip(
            "The last review's proposals, findings and what was applied"
        )
        self.last_review_action.triggered.connect(self.show_last_review)
        story_menu.addAction(self.last_review_action)
        self.undo_review_action = QAction("&Undo review changes", self)
        self.undo_review_action.setStatusTip(
            "Put the settings back as they were before the last review was applied"
        )
        self.undo_review_action.triggered.connect(self.undo_review)
        story_menu.addAction(self.undo_review_action)

        story_menu.addSeparator()
        self.import_plot_action = QAction("Import &plot…", self)
        self.import_plot_action.setStatusTip(
            "Give this story the facts and events from a plot file (.md)"
        )
        self.import_plot_action.triggered.connect(self.import_plot_into_story)
        story_menu.addAction(self.import_plot_action)
        # Four ways out of the app, one submenu: the Story menu had seventeen items.
        self.export_menu = story_menu.addMenu("&Export")
        self.export_action = QAction("&Scenario…", self)
        self.export_action.setStatusTip("The shareable base story, without the playthrough")
        self.export_action.triggered.connect(self.export_scenario)
        self.export_menu.addAction(self.export_action)
        self.archive_action = QAction("Story &backup…", self)
        self.archive_action.setStatusTip("Everything, every branch and the API log, in one file")
        self.archive_action.triggered.connect(self.export_story_archive)
        self.export_menu.addAction(self.archive_action)
        self.markdown_action = QAction("&Markdown (this branch)…", self)
        self.markdown_action.triggered.connect(lambda: self.export_markdown(chapter_only=False))
        self.export_menu.addAction(self.markdown_action)
        self.chapter_markdown_action = QAction("Current &chapter as Markdown…", self)
        self.chapter_markdown_action.triggered.connect(
            lambda: self.export_markdown(chapter_only=True)
        )
        self.export_menu.addAction(self.chapter_markdown_action)
        self._image_actions(story_menu)
        story_menu.addSeparator()
        self.usage_action = QAction("&Usage and cost…", self)
        self.usage_action.triggered.connect(self.show_usage)
        story_menu.addAction(self.usage_action)

        view_menu = self.menuBar().addMenu("&View")
        self.storyteller_view_action = QAction("What the &storyteller is sent", self)
        self.storyteller_view_action.setCheckable(True)
        self.storyteller_view_action.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.storyteller_view_action.setStatusTip(
            "Show chapter summaries where the prose has been archived, as the storyteller "
            "gets them; off, the whole story as written"
        )
        self.storyteller_view_action.toggled.connect(self.set_storyteller_view)
        view_menu.addAction(self.storyteller_view_action)
        self._find_actions(view_menu)
        # A view choice, so it lives here (it was a Settings tab of its own).
        self.dice_rolls_action = QAction("Show &dice rolls", self)
        self.dice_rolls_action.setCheckable(True)
        self.dice_rolls_action.setChecked(self.config.show_dice_rolls)
        self.dice_rolls_action.setStatusTip(
            "Show each roll on the turn it was made for; hidden rolls are still recorded "
            "with the story and in the API log"
        )
        self.dice_rolls_action.toggled.connect(self.set_show_dice_rolls)
        view_menu.addAction(self.dice_rolls_action)
        self._text_size_actions(view_menu)
        self._background_actions(view_menu)

        help_menu = self.menuBar().addMenu("&Help")
        samples_action = QAction("&Plot format guide and samples", self)
        samples_action.setStatusTip(
            "Open the folder with the plot file format guide and the sample plots"
        )
        samples_action.triggered.connect(self._open_samples)
        help_menu.addAction(samples_action)
        about_action = QAction("&About SealedLore", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        send_action = QAction("Send turn", self)
        send_action.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        send_action.triggered.connect(self.send_turn)
        self.addAction(send_action)
        # Esc stops a generation, as the Stop button does; otherwise it closes
        # the story map.
        stop_action = QAction("Stop", self)
        stop_action.setShortcut(QKeySequence(Qt.Key_Escape))
        stop_action.triggered.connect(self._on_escape)
        self.addAction(stop_action)

    def _on_escape(self) -> None:
        # Typing in the find bar, Esc closes it, even while a passage streams.
        if self.find_open() and self.find_bar.isAncestorOf(self.focusWidget()):
            self.close_find()
        elif self._busy:
            self.stop_generation()
        elif self.map_shown():
            self.show_map(False)
        elif self.find_open():
            self.close_find()

    # --- stories -----------------------------------------------------------

    def new_story(self) -> None:
        if self._busy or not self._private_allows_close():
            return
        title, accepted = QInputDialog.getText(self, "New story", "Title")
        if not accepted or not title.strip():
            return
        story = Story(title=title.strip())
        provider_config = self.config.active_provider()
        if provider_config is not None and provider_config.model:
            story.defaults.main_model = provider_config.model
        save_story_bundle(StoryBundle(story=story), root=self.root)
        self.stories.refresh(selected_id=story.id)
        self.open_story(story.id)
        self.open_setup()
        # A blank story has nobody to play yet, and nothing said what came next.
        self.statusBar().showMessage(
            "Next: add the cast on the Cast page, then Story → Start story…", 12000
        )

    def _open_new_bundle(self, bundle: StoryBundle, *, review: bool = False) -> None:
        """Save and open a story made from a file or a setup, and begin it.

        `review`: Setup first, and Start only once it is saved, as for a
        premise draft (an import may want changes, the style above all,
        before its opening is written). Restart skips it: it follows a Setup
        the author has just saved, or asks for the setup as it stands.
        """
        save_story_bundle(bundle, root=self.root)
        self.stories.refresh(selected_id=bundle.story.id)
        self.open_story(bundle.story.id)
        if self.session is None or self.session.story.id != bundle.story.id:
            return
        if review and not self.open_setup():
            self.statusBar().showMessage(
                "Not begun yet: Story → Start story… when it's ready", 8000
            )
            return
        self.start_story()

    def _fill_samples_menu(self) -> None:
        """File → New story from a sample: one item per sample story."""
        self.samples_menu.clear()
        samples = sample_stories()
        for name, path in samples:
            action = self.samples_menu.addAction(name)
            action.setStatusTip(f"A new story from {path.name}; Setup opens first")
            action.triggered.connect(lambda _=False, p=path: self.import_sample(p))
        if not samples:
            none = self.samples_menu.addAction("No sample stories found")
            none.setEnabled(False)

    def import_sample(self, path: Path) -> None:
        """A new story from a sample plot file, exactly as File → Import… would."""
        if self._busy or not self._private_allows_close():
            return
        self._import_plot_file(path)

    def _choose_sample(self) -> None:
        """The start page's Try a sample…: the only sample at once, else a list."""
        samples = sample_stories()
        if not samples:
            self._warn_plain(
                "No samples",
                "No sample stories were found with this copy of SealedLore "
                "(they are in SampleStories/ in the source).",
            )
            return
        if len(samples) == 1:
            self.import_sample(samples[0][1])
            return
        self._fill_samples_menu()
        self.samples_menu.exec(QCursor.pos())

    def import_file(self) -> None:
        """A new story from a scenario, a story backup or a plot file."""
        if self._busy or not self._private_allows_close():
            return
        self._import_path(
            "Import",
            # Every kind, shown together by default: split by kind, a plot
            # file once needed "All files" to be seen at all.
            f"SealedLore files (*{SCENARIO_SUFFIX} *{ARCHIVE_SUFFIX} *{PLOT_SUFFIX});;"
            f"Scenarios (*{SCENARIO_SUFFIX});;"
            f"Story backups (*{ARCHIVE_SUFFIX});;"
            f"Plot files (*{PLOT_SUFFIX});;"
            "All files (*)",
        )

    def open_plot_editor(self) -> None:
        """The plot file editor, one window, kept while the app runs."""
        if self._plot_editor is None:
            self._plot_editor = PlotEditorWindow(self)
            self._plot_editor.start_requested.connect(self._start_from_plot_file)
        self._plot_editor.show()
        self._plot_editor.raise_()
        self._plot_editor.activateWindow()

    def _start_from_plot_file(self, path: Path) -> None:
        if self._busy:
            self._warn_plain("Busy", "Wait for the current response to finish, then try again.")
            return
        self._import_plot_file(path)
        self.raise_()
        self.activateWindow()

    def _import_path(self, title: str, filters: str) -> None:
        """Ask for a file and import it as whatever it turns out to be."""
        path, _ = QFileDialog.getOpenFileName(self, title, str(Path.home()), filters)
        if not path:
            return
        if Path(path).suffix.lower() == PLOT_SUFFIX:
            self._import_plot_file(Path(path))
        elif file_format(Path(path)) == ARCHIVE_FORMAT:
            self._import_archive(Path(path))
        else:
            # Anything else gets the scenario reader's plain-words error.
            self._import_scenario(Path(path))

    def _import_scenario(self, path: Path) -> None:
        try:
            scenario = read_scenario(path)
        except ScenarioError as exc:
            self._warn_plain("Could not read scenario", str(exc))
            return
        provider_config = self.config.active_provider()
        model = provider_config.model if provider_config and provider_config.model else None
        self._open_new_bundle(bundle_from_scenario(scenario, main_model=model), review=True)

    def _read_plot_file(self, path: Path) -> ParsedPlotFile | None:
        """Parse a plot file, and show what it had trouble with.

        Errors stop the import; notes are shown and the author decides.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._warn_plain("Could not read plot file", f"{path.name}: {exc}")
            return None
        parsed = parse_plot_markdown(text, fallback_title=path.name.removesuffix(PLOT_SUFFIX))
        if parsed.ok and parsed.pictures:
            # The pictures its cards name, from the folder the file is in. One
            # that can't be read is a note among the others, never a stop.
            parsed.problems += load_plot_pictures(parsed, path.parent)
            parsed.problems.sort(key=lambda problem: problem.line)
        if not parsed.problems:
            return parsed
        details = "\n".join(problem.describe() for problem in parsed.problems)
        if not parsed.ok:
            box = QMessageBox(
                QMessageBox.Warning,
                "Plot file has errors",
                f"{path.name} can't be imported until these are fixed.",
                QMessageBox.Ok,
                self,
            )
            box.setDetailedText(details)
            box.exec()
            return None
        box = QMessageBox(
            QMessageBox.Information,
            "Import plot file",
            f"{path.name} was read, with {len(parsed.problems)} note(s). Import it?",
            QMessageBox.Yes | QMessageBox.Cancel,
            self,
        )
        box.setDetailedText(details)
        return parsed if box.exec() == QMessageBox.Yes else None

    def _import_plot_file(self, path: Path) -> None:
        parsed = self._read_plot_file(path)
        if parsed is None:
            return
        provider_config = self.config.active_provider()
        model = provider_config.model if provider_config and provider_config.model else None
        self._open_new_bundle(bundle_from_scenario(parsed.scenario, main_model=model), review=True)

    def import_plot_into_story(self) -> None:
        """Give the open story a plot file's facts and events, and nothing else."""
        if self.session is None or self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import plot", str(Path.home()), f"Plot files (*{PLOT_SUFFIX});;All files (*)"
        )
        if not path or (parsed := self._read_plot_file(Path(path))) is None:
            return
        if parsed.scenario.plot is None:
            self._warn_plain("No plot", f"{Path(path).name} has no Facts, Timeline or Events.")
            return
        if self.session.story.plot is not None:
            answer = QMessageBox.question(
                self,
                "Replace plot",
                "This story already has a plot. Replace it with this one?",
            )
            if answer != QMessageBox.Yes:
                return
        self.session.attach_plot(parsed.scenario.plot)
        self.statusBar().showMessage(
            "Plot attached: its facts and events. The world, cast and places in the file "
            "were not used; start a new story from the file to use those too.",
            10000,
        )

    def restart_story(self, *, review: bool = False) -> None:
        """A fresh playthrough of this story's setup, leaving this one untouched.

        `review` (the menu's Restart): Setup first, since a new playthrough is
        the only place person and tense can change once a story has begun.
        """
        if self.session is None or self._busy:
            return
        title = f"{self.session.story.title} (new playthrough)"
        files = reference_files(self.session.bundle, self.root)
        self._open_new_bundle(fresh_playthrough(self.session.bundle, title, files), review=review)

    # --- the stories list's menu ---------------------------------------------

    def _bundle_for(self, story_id: str, *, quiet: bool = False) -> StoryBundle | None:
        """The open story's live bundle, or the one on disk."""
        if self.session is not None and self.session.story.id == story_id:
            self.session.save()
            return self.session.bundle
        try:
            return load_story_bundle(story_id, root=self.root)
        except (FileNotFoundError, ValueError) as exc:
            if not quiet:
                self._warn_plain("Could not read story", str(exc))
            return None

    def duplicate_story_settings(self, story_id: str) -> None:
        if self._busy or not self._private_allows_close():
            return
        if (source := self._bundle_for(story_id)) is None:
            return
        bundle = fresh_playthrough(
            source, f"{source.story.title} (copy)", reference_files(source, self.root)
        )
        save_story_bundle(bundle, root=self.root)
        self.stories.refresh(selected_id=bundle.story.id)
        self.open_story(bundle.story.id)
        self.statusBar().showMessage(
            f"Created “{bundle.story.title}” — same setup, unplayed. Story → Start story… "
            "to begin.",
            8000,
        )

    def duplicate_entire_story(self, story_id: str) -> None:
        if self._busy or not self._private_allows_close():
            return
        if (source := self._bundle_for(story_id)) is None:
            return
        try:
            copy = copy_story(story_id, title=f"{source.story.title} (copy)", root=self.root)
        except OSError as exc:
            self._warn_plain("Could not duplicate story", str(exc))
            return
        self.stories.refresh(selected_id=copy.story.id)
        self.open_story(copy.story.id)
        self.statusBar().showMessage(f"Created “{copy.story.title}”, a complete copy", 8000)

    def delete_story(self, story_id: str) -> None:
        if self._busy:
            return
        # A story that can't be read can still be deleted: that is often the
        # only thing left to do with it.
        source = self._bundle_for(story_id, quiet=True)
        if source is not None:
            title = source.story.title
            count = len(source.nodes)
            text = (
                f"Delete “{title}”?\n\nIts {count} message{'s' if count != 1 else ''}, every "
                "branch, its summaries and its cost history go to the Trash, where you can "
                "still restore them."
            )
        else:
            title = story_id
            text = (
                f"Delete the story folder “{story_id}”?\n\nIt couldn't be read, so nothing in "
                "it can be shown. It goes to the Trash, where you can still restore it."
            )
        answer = QMessageBox.question(
            self,
            "Delete story",
            text,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        if self.session is not None and self.session.story.id == story_id:
            self._close_story()
        folder = story_dir(story_id, self.root)
        trashed = QFile.moveToTrash(str(folder))
        if isinstance(trashed, tuple):  # PySide6 returns (ok, path in trash)
            trashed = trashed[0]
        if not trashed:
            answer = QMessageBox.warning(
                self,
                "Delete story",
                f"The Trash isn't available for {folder}.\n\nDelete “{title}” permanently? "
                "This can't be undone.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                self.stories.refresh()
                return
            try:
                remove_story(story_id, root=self.root)
            except OSError as exc:
                self._warn_plain("Could not delete story", f"{folder}: {exc.strerror or exc}")
        self.stories.refresh(selected_id=self.session.story.id if self.session else None)
        self.statusBar().showMessage(
            f"Deleted “{title}”" + (" (it's in the Trash)" if trashed else ""), 8000
        )

    def _open_story_folder(self, story_id: str) -> None:
        """The story's folder in the desktop's file manager (the story list's
        right-click menu)."""
        folder = story_dir(story_id, self.root)
        if not folder.is_dir():
            self._warn_plain("No folder", f"{folder} isn't there any more.")
            self.stories.refresh(selected_id=self.session.story.id if self.session else None)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._warn_plain("Couldn't open the folder", f"The story's files are in\n{folder}")

    def close_story(self) -> None:
        """File → Close story: back to the start page. A chat or a private
        scene kept in memory only asks first, and is gone on Yes."""
        if self.session is None or self._busy or not self._private_allows_close():
            return
        self._close_story()
        self.stories.refresh()

    def _close_story(self) -> None:
        """Back to the no-story state the window starts in."""
        self.show_map(False)
        self.close_find()
        if self.session is not None:
            self.session.close(wait_seconds=0)
            self.session.save()
        self.session = None
        self.setWindowTitle("SealedLore")
        self.status_strip.reset_session_cost()
        self.status_strip.set_budget(None)
        self.status_strip.set_lore(None)
        self.cast_panel.set_cast([], [], [])
        self.scene_panel.set_scene(SceneState(), [])
        self._refresh_plot_panel()
        self.lore_panel.set_entries([])
        self.summaries_panel.set_summaries([], on_path_ids=set(), archivable_turns=0)
        self.staleness.hide()
        self.inspector.clear()
        self.refresh_images()
        self._apply_background()
        self.reload_transcript()
        self._tee_state = ""
        self._sync_chat_ui()
        self._update_controls()

    def export_scenario(self) -> None:
        if self.session is None:
            return
        story = self.session.story
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in story.title).strip()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export as scenario",
            str(Path.home() / f"{safe or 'story'}{SCENARIO_SUFFIX}"),
            f"SealedLore scenarios (*{SCENARIO_SUFFIX})",
        )
        if not path:
            return
        if not path.endswith(".json"):
            path += SCENARIO_SUFFIX
        try:
            bundle = self.session.bundle
            write_scenario(
                Path(path), scenario_from_bundle(bundle, reference_files(bundle, self.root))
            )
        except OSError as exc:
            self._warn_plain("Could not export scenario", f"{path}: {exc.strerror or exc}")
            return
        except ScenarioError as exc:
            self._warn_plain("Could not export scenario", str(exc))
            return
        self.statusBar().showMessage(f"Scenario exported to {path}", 6000)

    def open_setup(self) -> bool:
        """Story → Setup…; True if the author saved it."""
        if self.session is None or self._busy:
            return False
        path = self.session.path()
        start_before = _how_it_starts(self.session.story)
        dialog = SetupDialog(
            self.session.story,
            self.session.cast,
            first_message=path[0] if path else None,
            texts=self.session.texts,
            parent=self,
        )
        if dialog.exec() != SetupDialog.Accepted:
            return False
        self.session.forget_review_undo()
        self.session.story.updated_at = utc_now_iso()
        # Not begun: the scene it will begin in is Setup's, now, not only at Start.
        self.session.sync_unstarted_scene()
        self.session.save()
        self._refresh_scene_panel()
        self.composer.set_default_agency(self.session.story.defaults.agency_mode)
        # Setup's Style tab replaced the style: the Style page must edit the new one.
        self.style_panel.set_style(
            self.session.story.style, started=bool(self.session.nodes), texts=self.session.texts
        )
        self.composer.set_default_length(self.session.story.style.response_style)
        self.setWindowTitle(f"SealedLore — {self.session.story.title}")
        self.stories.refresh(selected_id=self.session.story.id)
        self.reload_transcript()
        self._reassemble_inspector()
        self._update_controls()
        # The opening, the starting scene and the suggested character are only
        # read when a story begins; changed on one already under way, they
        # used to be saved with nothing to show for it.
        if self.session.nodes and _how_it_starts(self.session.story) != start_before:
            self._offer_new_playthrough()
        return True

    def _offer_new_playthrough(self) -> None:
        answer = QMessageBox.question(
            self,
            "Start a new playthrough?",
            "This story has already begun, so the new opening and starting scene "
            "take effect in a new playthrough.\n\n"
            "Start one now? It is a new story with this setup; the story so far is "
            "kept as it is. (Later: Story → Restart as new playthrough….)",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.restart_story()

    def start_story(self) -> None:
        """Pick who to play, then run the opening (§ story setup)."""
        if self.session is None or self._busy or self.session.nodes:
            return
        story = self.session.story
        dialog = StartDialog(story, self.session.cast, parent=self)
        if dialog.exec() != StartDialog.Accepted:
            return
        held = dialog.held_character_id()
        # Before holding: pinning resets the roster the held character joins.
        if (pinned := dialog.pinned()) is not None:
            self.session.pin_at_start(*pinned)
        # On the GUI thread first, so the composer shows who is held (and not
        # whoever wasn't picked) before the opening streams in; begin()
        # repeats both harmlessly.
        if self.session.set_aside_unchosen(held):
            self.refresh_panels()
        self._on_held_changed(held or "")

        opening = story.setup.opening_text.strip()
        if not opening:
            list(self.session.begin(held))
            self.reload_transcript()
            self._reassemble_inspector()
            self._update_controls()
            return
        if story.setup.opening_mode == "as_written":
            # No call writes it, but the scene read after it is one, on the
            # worker like any other: it places whoever the opening put there.
            self.session.provider = self._current_provider()
            self.transcript.clear()
            self.transcript.add_message("Narrator", opening, "assistant")
            self._start(lambda: self.session.begin(held), streaming=False)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return
        self.session.provider = self._current_provider()
        self.transcript.clear()
        self.transcript.add_message(
            "Direction", story.setup.opening_text.strip(), kind_for(DIRECTOR_SPEAKER_ID)
        )
        self._start(lambda: self.session.begin(held))

    def open_story(self, story_id: str) -> None:
        if self._busy:
            return
        leaving = self.session is not None and self.session.story.id != story_id
        # Leaving a memory-only chat or scene loses it: asked first.
        if leaving and not self._private_allows_close():
            return
        self.show_map(False)
        if self.session is not None and self.session.story.id != story_id:
            # Background chapters of the story being left are logged, not lost.
            self.session.close(wait_seconds=0)
        try:
            self.session = StorySession.load(
                story_id,
                self.config,
                self._current_provider(),
                root=self.root,
                estimator=self.estimator,
                learn_corrections=not self.use_mock,
                embeddings=self._current_embeddings(),
            )
        except (FileNotFoundError, ValueError) as exc:
            self._warn_plain("Could not open story", str(exc))
            return
        self._after_open()

    def _after_open(self) -> None:
        """Everything that follows a story being opened, from disk or (a
        memory-only chat) from memory."""
        # A route to one host is checked against that host's price.
        self.session.host_price = self._host_price
        self._fetch_host_prices()
        self.setWindowTitle(f"SealedLore — {self.session.story.title}")
        for notice in self.session.bundle.notices:
            self.statusBar().showMessage(notice, 20000)
        # Length and outcome stick from turn to turn, but not from one story
        # to the next: this story's defaults are its own.
        self.composer.reset_turn_shape()
        self.status_strip.reset_session_cost()
        self._refresh_story_cost()
        self._counted_summaries.clear()
        self._staleness_dismissed = False
        self._resume_private()
        self.refresh_panels()
        self.right_tabs.setCurrentWidget(self.scene_panel)
        self.refresh_images()
        self._apply_background()
        self.reload_transcript()
        self.refresh_summaries()
        self.refresh_retrieval()
        self.refresh_inspector()
        self._tee_state = ""
        self._sync_chat_ui()
        self._attest_chat()
        self._update_controls()

    def refresh_panels(self) -> None:
        if self.session is None:
            return
        story = self.session.story
        # What the plot hasn't brought in stays out of the author's way too,
        # unless they have chosen to see the plot.
        hidden = set() if self.config.show_plot_spoilers else self.session.hidden_ids()
        self._shown_hidden = hidden
        self.cast_panel.set_cast(
            self.session.cast,
            self.session.supporting,
            self.session.character_suggestions,
            hidden=hidden,
        )
        self._refresh_scene_panel()
        self.lore_panel.set_entries(self.session.bundle.lore, hidden=hidden)
        self.lore_panel.set_embeddings_available(self.session.embeddings is not None)
        self.style_panel.set_style(
            story.style, started=bool(self.session.nodes), texts=self.session.texts
        )
        self._refresh_composer()

    def refresh_retrieval(self) -> None:
        """Show what the last turn injected, and how it was found (§7)."""
        if self.session is None:
            return
        report = self.session.last_retrieval
        self.lore_panel.show_retrieval(report)
        self.status_strip.set_lore(report)

    def refresh_summaries(self) -> None:
        """Reload the chapter panel, the staleness banner and the status counter."""
        if self.session is None:
            return
        split = self.session.split()
        self.summaries_panel.set_summaries(
            self.session.summaries,
            on_path_ids={summary.id for summary in split.summaries},
            archivable_turns=turns_in(self.session.next_chunk(allow_partial=True)),
            numbers=chapter_numbers(split.summaries),
        )
        self.status_strip.set_archive(len(split.summaries))
        self.summaries_panel.set_kept_warning(self._kept_warning())

        self._refresh_over_budget()

        stale = [summary for summary in split.summaries if summary.stale]
        if stale and not self._staleness_dismissed:
            self.staleness.show_stale(stale, positions=self._chapter_numbers(stale))
        else:
            self.staleness.hide()

    def _kept_warning(self) -> str | None:
        """A chat's kept messages are sent every turn: say so once they take
        a fifth of the budget."""
        if self.session is None or not self.session.story.chat:
            return None
        kept = self.session.kept_tokens()
        budget = self.session.story.defaults.context_token_budget
        if not budget or kept <= KEPT_WARNING_SHARE * budget:
            return None
        return (
            f"Messages kept in full take ~{kept:,} tokens, {kept / budget:.0%} of the context "
            "budget, and are sent with every message. Unmark some (⋯ → Keep in full) to "
            "leave more room for the conversation."
        )

    def _refresh_over_budget(self) -> None:
        """Offer archival when the prompt is over budget and nothing else will."""
        prompt = (
            self.session.context_overflow()
            if self.session is not None and not self.config.auto_archive and not self._busy
            else None
        )
        if prompt is None:
            self.over_budget.hide()
            return
        self.over_budget.show_over(
            full_total=prompt.full_total,
            budget=prompt.budget.budget,
            left_out=len(prompt.excluded_node_ids),
        )

    def archive_to_target(self) -> None:
        """The banner's Archive now: whole chunks down to the target, with a progress dialog."""
        if self.session is None or self._busy:
            return
        self.session.provider = self._current_provider()
        self.over_budget.hide()
        self._start(
            self.session.archive_down,
            streaming=False,
            busy_text="Archiving the oldest turns into chapters…",
        )

    def _archive_automatically(self) -> None:
        """Turn automatic archival on; the next turn archives in the background."""
        self.config.auto_archive = True
        save_config(self.config, root=self.root)
        self.over_budget.hide()
        self.statusBar().showMessage(
            "Automatic archival is on: chapters will be written in the background from "
            "the next turn",
            8000,
        )

    def _chapter_numbers(self, summaries: list[Summary]) -> list[int]:
        """As the reader counts them along the active path; a chapter on another
        branch falls back to its place among all chapters."""
        assert self.session is not None
        on_path = chapter_numbers(self.session.split().summaries)
        return [
            on_path.get(summary.id) or chapter_number(self.session.summaries, summary)
            for summary in summaries
        ]

    def save_story(self) -> None:
        if self.session is not None:
            self.session.save()
        save_config(self.config, root=self.root)
        self.stories.refresh(selected_id=self.session.story.id if self.session else None)

    def reload_transcript(self) -> None:
        if self.session is None:
            self.transcript.clear()
            missing = [] if self.use_mock else setup_missing(self.config)
            if missing:
                # Nothing but a backup can be used before this is set up.
                self.transcript.show_placeholder(
                    welcome_html(missing, partly_set=len(missing) < 3),
                    [("Open Settings…", self.open_settings), ("Import…", self.import_file)],
                    rich=True,
                )
                return
            hint = "No story open. Pick one in Stories on the left, or start one."
            actions = [
                ("New story…", self.new_story),
                ("New story from a premise…", self.new_story_from_premise),
                ("New simple chat…", self.new_chat),
            ]
            more = [("Import…", self.import_file), ("Try a sample…", self._choose_sample)]
            self.transcript.show_placeholder(hint, actions, more_actions=more)
            return
        self._show_nodes(self.session.full_path())
        self.refresh_branch_bar()
        self._refresh_find()

    def _show_nodes(self, nodes: list[Node]) -> None:
        assert self.session is not None
        self.transcript.set_chat(self.session.story.chat, tee=self.session.tee_chat)
        split = self.session.split()
        setup = self.session.story.setup
        empty_text = (
            "Write your first message below. Story → Setup… changes the system prompt."
            if self.session.story.chat
            else "Story → Start story… to begin from the opening, or type below."
            if setup.opening_text.strip()
            else "Nothing written yet. Type below to begin the story, or set up its world "
            "and opening under Story → Setup…"
        )
        if self._storyteller_view:
            self._show_storyteller_view(nodes, split)
            return
        self.transcript.show_path(
            nodes,
            self.session.cast,
            archived_ids=split.archived_ids,
            asides_for=self.session.asides_at,
            empty_text=empty_text,
            show_rolls=self.config.show_dice_rolls,
            variant_of=self.session.take_positions(),
            marked_ids=frozenset(self.session.review_marks),
            pictures_for=self._pictures_for,
            branch_starts=self.session.branch_start_names(),
        )

    def _chapter_cards(self, nodes: list[Node], split) -> list[ChapterCard]:
        """The chapters on this path, numbered as the navigator numbers them."""
        position = {node.id: index for index, node in enumerate(nodes, start=1)}
        cards = []
        numbers = chapter_numbers(split.summaries)
        for summary in split.summaries:
            number = numbers[summary.id]
            first, last = summary.covered_node_ids[0], summary.covered_node_ids[-1]
            span = f"messages {position.get(first, '?')}–{position.get(last, '?')}"
            kind = (
                f"Chapters {number}–{number + len(summary.merged_from) - 1}  ·  "
                "merged into one part"
                if summary.merged_from
                else f"Chapter {number}"
            )
            text = summary.content
            if self.config.story_ledger and summary.ledger:
                text += "\n\n" + summary.ledger
            if self.session is not None and self.session.story.chat:
                # A chat's parts, with the messages kept in full after them, as
                # they are sent (engine/chat.render_chat_summaries).
                kind = kind.replace("Chapters", "Parts").replace("Chapter", "Part")
                index = {node.id: node for node in nodes}
                for node_id in summary.covered_node_ids:
                    node = index.get(node_id)
                    if node is not None and node.meta.keep_full:
                        who = "Assistant" if node.kind == "assistant" else "You"
                        text += f"\n\nKept in full — {who}:\n{node.content.strip()}"
            cards.append(ChapterCard(f"{kind}  ·  {span}  ·  summary", text, summary.stale))
        return cards

    def _show_storyteller_view(self, nodes: list[Node], split) -> None:
        assert self.session is not None
        prompt = self.session.last_prompt
        if prompt is None:
            try:
                prompt = self.session.assemble(
                    self._current_turn(self.composer.text() or "(nothing typed yet)")
                )
            except Exception:  # a preview only; the view still shows without it
                prompt = None
        excluded = frozenset(prompt.excluded_node_ids) if prompt is not None else frozenset()
        self.transcript.show_storyteller_view(
            self._chapter_cards(nodes, split),
            list(split.verbatim),
            self.session.cast,
            excluded_ids=excluded,
            show_rolls=self.config.show_dice_rolls,
            variant_of=self.session.take_positions(),
            marked_ids=frozenset(self.session.review_marks),
            branch_starts=self.session.branch_start_names(),
        )

    def set_storyteller_view(self, on: bool) -> None:
        """As written, or as sent to the storyteller (the view toggle)."""
        if on == self._storyteller_view:
            return
        self._storyteller_view = on
        self.view_toggle.set_mode(on)
        self.storyteller_view_action.setChecked(on)
        if not self._busy:
            self.reload_transcript()

    def set_show_dice_rolls(self, on: bool) -> None:
        if self.config.show_dice_rolls == on:
            return
        self.config.show_dice_rolls = on
        save_config(self.config, root=self.root)
        if not self._busy:
            self.reload_transcript()

    def refresh_branch_bar(self) -> None:
        if self.session is None:
            self.branch_bar.set_branches([])
            return
        listed = self.session.branch_list()
        pending = self.session.pending_branch
        self.branch_bar.set_branches(
            listed, self._branch_openings(), pending=pending[1] if pending else None
        )

    def _branch_openings(self) -> dict[str | None, str]:
        """How each branch begins: its first message's opening words."""
        if self.session is None:
            return {}
        by_id = {node.id: node for node in self.session.nodes}
        found: dict[str | None, str] = {}
        for branch in self.session.branch_list():
            first = branch.start_id
            if first is None and branch.end_id in by_id:
                first = path_to(self.session.nodes, branch.end_id)[0].id
            if first in by_id:
                found[branch.start_id] = opening_words(by_id[first].content)
        return found

    # --- branches (§2.2, §10) ------------------------------------------------

    def _after_navigation(self, focus_id: str | None = None) -> None:
        self.reload_transcript()
        # The scene lives on the passages, so another branch has its own.
        self._refresh_scene_panel()
        self._refresh_composer()
        self.refresh_summaries()
        self._reassemble_inspector()
        self._update_controls()
        if focus_id:
            QTimer.singleShot(0, lambda: self.transcript.scroll_to_node(focus_id))

    def _switch_variant(self, node_id: str, delta: int) -> None:
        """‹ › on a passage: the other take stands in its place, and the story
        after it stays (a take is never a branch)."""
        if self.session is None or self._busy:
            return
        ids = [take.id for take in self.session.takes(node_id)]
        target = ids.index(node_id) + delta
        if not 0 <= target < len(ids):
            return
        try:
            stale = self.session.switch_take(node_id, delta)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 8000)
            return
        self._after_navigation(ids[target])
        if stale:
            self.statusBar().showMessage(
                "The chapter covering this passage is out of date: rebuild it from the banner.",
                8000,
            )

    # --- branches: named lines, made by rewriting (engine/branches.py) -------

    # --- the story map --------------------------------------------------------

    def map_shown(self) -> bool:
        return self.centre_stack.currentWidget() is self.story_map

    def show_map(self, on: bool) -> None:
        """Every branch on one diagram, in the transcript's place."""
        on = bool(on and self.session is not None and not self.session.in_private)
        leaving = not on and self.map_shown()
        self.centre_stack.setCurrentWidget(self.story_map if on else self.transcript)
        self.composer.setVisible(not on)
        self.view_toggle.setEnabled(not on)
        if self.branch_bar.map_button.isChecked() != on:
            self.branch_bar.map_button.blockSignals(True)
            self.branch_bar.map_button.setChecked(on)
            self.branch_bar.map_button.blockSignals(False)
        if on:
            self._map_focus = None
            self.refresh_map()
            self.story_map.view.setFocus()
        elif leaving:
            focus, self._map_focus = self._map_focus, None
            if focus:
                QTimer.singleShot(0, lambda: self.transcript.scroll_to_node(focus))

    def refresh_map(self, *, keep_view: bool = False) -> None:
        if self.session is None:
            self.story_map.set_lanes([], here=None, text_of=lambda _i: "", openings={})
            return
        by_id = {node.id: node for node in self.session.nodes}
        lanes = self.session.story_map()
        self.story_map.set_lanes(
            lanes,
            here=len(self.session.full_path()) or None,
            text_of=lambda node_id: by_id[node_id].content if node_id in by_id else "",
            openings=self._branch_openings(),
            keep_view=keep_view,
        )

    def _select_from_map(self, start_id: str | None, node_id: str | None) -> None:
        """A click on the map: the story moves to that branch (its end), and
        the map stays, redrawn with it as the one being read. The message
        clicked is where the story opens when the map closes."""
        if self.session is None or self._busy:
            return
        current = self.session.current_branch()
        if current is None or current.start_id != start_id:
            self._switch_branch(start_id)
            self.refresh_map(keep_view=True)
        self._map_focus = node_id

    def _open_from_map(self, start_id: str | None, node_id: str | None) -> None:
        """A double-click on the map: that branch, and the story, scrolled to
        the message (or its end, from its name)."""
        if self.session is None or self._busy:
            return
        self._select_from_map(start_id, node_id)
        self.show_map(False)

    def _switch_branch(self, start_id: str | None) -> None:
        if self.session is None or self._busy or self.session.in_private:
            return
        leaf = self.session.switch_branch(start_id)
        self._after_navigation(leaf.id)
        current = self.session.current_branch()
        if current is not None:
            self.statusBar().showMessage(f"Now reading branch “{current.name}”", 5000)

    def _rename_branch(self, start_id: str | None) -> None:
        if self.session is None or self._busy:
            return
        branch = next((b for b in self.session.branch_list() if b.start_id == start_id), None)
        if branch is None:
            return
        name, ok = QInputDialog.getText(self, "Rename branch", "Name:", text=branch.name)
        if not ok or not name.strip():
            return
        given = self.session.rename_branch(start_id, name)
        self.refresh_branch_bar()
        if given != " ".join(name.split()):
            self.statusBar().showMessage(
                f"Another branch is already called that: named it “{given}”", 8000
            )
        place = self.transcript.place_of()
        self.reload_transcript()
        self.transcript.keep_place(place)
        if self.map_shown():
            self.refresh_map(keep_view=True)

    def _delete_branch(self, start_id: str | None) -> None:
        if self.session is None or self._busy or start_id is None:
            return
        branch = next((b for b in self.session.branch_list() if b.start_id == start_id), None)
        if branch is None or branch.is_main:
            return
        doomed = self.session.deletion_for(start_id)
        inside = [b.name for b in self.session.branch_list() if b.parent_start_id == start_id]
        answer = QMessageBox.question(
            self,
            "Delete branch",
            f"Delete the branch “{branch.name}”: its {len(doomed.node_ids)} messages"
            + (f", and the branches made from it ({', '.join(inside)})" if inside else "")
            + "? Pictures are kept. This can't be undone.",
        )
        if answer != QMessageBox.Yes:
            return
        self.session.delete_branch(start_id)
        self._after_navigation(self.session.story.active_leaf_id)
        if self.map_shown():
            self.refresh_map(keep_view=True)
        self.statusBar().showMessage(f"Deleted the branch “{branch.name}”", 6000)

    def _default_branch_name(self) -> str:
        return next_branch_name(self.session.branch_list()) if self.session is not None else ""

    def _rewrite_from(self, node_id: str, text: str, name: str = "") -> None:
        """⋯ → New branch from here…: the message, changed or not, as the
        start of a new branch, under the name given (blank: "Branch N"). A
        turn is answered at once; a passage is the author's, and waits."""
        if self.session is None or self._busy:
            return
        if self.session.in_private:
            self.statusBar().showMessage("End the private scene to start a branch", 6000)
            return
        node = next((n for n in self.session.nodes if n.id == node_id), None)
        if node is None or not text.strip():
            return
        before = path_to(self.session.nodes, node_id)[:-1]
        current = self.session.current_branch()
        listed = self.session.branch_list()
        name = " ".join(name.split())
        name = unique_branch_name(name, listed) if name else next_branch_name(listed)
        told = (
            f"Started the branch “{name}” at message {len(before) + 1} (rename it from "
            f"the branch bar). “{current.name if current else 'The story'}” is kept."
        )
        if node.kind != "user":
            list(self.session.rewrite_from(node_id, text, name=name))
            self._after_navigation(self.session.story.active_leaf_id)
            self.statusBar().showMessage(told, 10000)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return
        self.session.forget_review_undo()
        self.session.provider = self._current_provider()
        # The story as it stood before the message, then the rewritten turn,
        # with its answer streaming in below.
        self._show_nodes(before)
        label = speaker_label_for(node.speaker_id, self.session.cast)
        self.transcript.add_message(label, text, kind_for(node.speaker_id), node.ooc)
        self.statusBar().showMessage(told, 10000)
        self._start(lambda: self.session.rewrite_from(node_id, text, name=name))

    def _scroll_to_node(self, node_id: str) -> None:
        self.transcript.scroll_to_node(node_id)

    # --- export, import, usage (§9, §8.3) ------------------------------------

    def export_story_archive(self) -> None:
        if self.session is None:
            return
        path = self._save_path("Export story backup", ARCHIVE_SUFFIX, "SealedLore backups")
        if not path:
            return
        try:
            # From memory for a chat kept there: its log and its pictures.
            write_archive(
                path,
                self.session.bundle_to_save(),
                self._story_log(),
                self.session.pictures.all_files(),
            )
        except OSError as exc:
            self._warn_plain("Could not write the story backup", f"{path}: {exc.strerror or exc}")
            return
        self.statusBar().showMessage(f"Story backup written to {path}", 6000)

    def _import_archive(self, path: Path) -> None:
        try:
            bundle, api_log = read_archive(path)
            if bundle.story.chat and bundle.story.chat_keep == "memory":
                # Exported from a chat kept in memory only: back into memory,
                # with nothing written, or saved as an ordinary chat.
                where = self._ask_memory_import(bundle.story.title)
                if where is None:
                    return
                if where == "memory":
                    files, bundle.pending_files = bundle.pending_files, {}
                    self.open_memory_chat(bundle, files=files, log=api_log)
                    self.statusBar().showMessage(
                        f"Opened {bundle.story.title} in memory only", 6000
                    )
                    return
            bundle = restore_archive(bundle, api_log, root=self.root)
        except ArchiveError as exc:
            self._warn_plain("Could not import the story backup", str(exc))
            return
        except OSError as exc:
            self._warn_plain("Could not import the story backup", f"{path}: {exc.strerror or exc}")
            return
        self.stories.refresh(selected_id=bundle.story.id)
        self.open_story(bundle.story.id)
        self.statusBar().showMessage(f"Imported {bundle.story.title}", 6000)

    def _ask_memory_import(self, title: str) -> str | None:
        """ "memory", "disk", or None to cancel: where a memory-only chat's
        backup comes back."""
        box = QMessageBox(self)
        box.setWindowTitle("A chat kept in memory only")
        box.setText(
            f"“{title}” was a chat kept in memory only. Open it in memory again (nothing "
            "is written, and closing it loses it), or save it as an ordinary chat?"
        )
        memory = box.addButton("Open in memory only", QMessageBox.AcceptRole)
        disk = box.addButton("Save as a normal chat", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(memory)
        box.exec()
        clicked = box.clickedButton()
        return "memory" if clicked is memory else "disk" if clicked is disk else None

    def export_markdown(self, *, chapter_only: bool) -> None:
        if self.session is None:
            return
        story = self.session.story
        nodes = list(self.session.split().verbatim) if chapter_only else self.session.path()
        if not nodes:
            self.statusBar().showMessage("Nothing to export yet", 4000)
            return
        path = self._save_path("Export as Markdown", ".md", "Markdown")
        if not path:
            return
        subtitle = "The current chapter" if chapter_only else None
        try:
            path.write_text(
                render_markdown(story.title, nodes, self.session.cast, subtitle=subtitle),
                encoding="utf-8",
            )
        except OSError as exc:
            self._warn_plain("Could not export", f"{path}: {exc.strerror or exc}")
            return
        self.statusBar().showMessage(f"Exported {len(nodes)} messages to {path}", 6000)

    def _save_path(self, title: str, suffix: str, label: str) -> Path | None:
        assert self.session is not None
        name = self.session.story.title
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in name).strip()
        chosen, _ = QFileDialog.getSaveFileName(
            self, title, str(Path.home() / f"{safe or 'story'}{suffix}"), f"{label} (*{suffix})"
        )
        if not chosen:
            return None
        if not chosen.endswith(suffix) and not chosen.endswith(".json"):
            chosen += suffix
        return Path(chosen)

    def _story_log(self) -> list[dict]:
        """The open story's API log: from disk, or a memory-only chat's own."""
        if self.session is not None and self.session.memory_only:
            return list(self.session.memory_log)
        return read_api_log(self.session.story.id, root=self.root)

    def show_usage(self) -> None:
        if self.session is None:
            return
        report = usage_report(
            self._story_log(),
            self.config.model_prices,
            cache_ttl=self.config.cache_ttl,
        )
        UsageDialog(self.session.story.title, report, self).exec()

    def _refresh_story_cost(self) -> None:
        if self.session is None:
            return
        story_id = self.session.story.id
        if self.session.memory_only:
            total = usage_report(
                self.session.memory_log, self.config.model_prices, cache_ttl=self.config.cache_ttl
            ).total
            self.status_strip.set_story_cost(
                total.cost, total.unpriced, total.calls, total.estimated
            )
            return
        cached_id, offset, entries = self._log_cache
        if cached_id != story_id:
            offset, entries = 0, []
        new, start, end = read_api_log_since(story_id, offset, root=self.root)
        if start != offset:  # the file was replaced: start over
            entries = []
        entries = entries + new
        self._log_cache = (story_id, end, entries)
        total = usage_report(
            entries, self.config.model_prices, cache_ttl=self.config.cache_ttl
        ).total
        self.status_strip.set_story_cost(total.cost, total.unpriced, total.calls, total.estimated)

    # --- provider ----------------------------------------------------------

    def _current_provider(self) -> ChatProvider:
        """The provider for this session, built once and reused.

        Rebuilt only when the settings change: a fresh OpenAI-compatible
        provider opens its own httpx client, so building one per turn would
        leak a connection pool every turn.
        """
        sealed = self._chat_provider()
        if sealed is not None:
            return sealed  # an end-to-end encrypted chat (window_chat)
        if self._provider is None:
            self._provider = self._build_provider()
        return self._provider

    def _build_provider(self) -> ChatProvider:
        if self.use_mock:
            return MockChatProvider()
        provider_config = self.config.active_provider()
        if provider_config is None:
            # Never the mock by accident: a job started without an endpoint
            # fails with the reason instead of saving canned text as story.
            return UnconfiguredProvider()
        return OpenAICompatibleProvider(provider_config)

    def _current_embeddings(self) -> EmbeddingBackend | None:
        """The embeddings client, built once. None means keyword retrieval (§7)."""
        if self._embeddings is None:
            settings = self.config.embeddings()
            if settings is not None:
                self._embeddings = OpenAICompatibleEmbeddings(settings)
        return self._embeddings

    def _discard_provider(self) -> None:
        self._discard_sealed_provider()
        provider, self._provider = self._provider, None
        close = getattr(provider, "close", None)
        if close is not None:
            close()

        embeddings, self._embeddings = self._embeddings, None
        close_embeddings = getattr(embeddings, "close", None)
        if close_embeddings is not None:
            close_embeddings()

    def _current_turn(self, text: str) -> TurnRequest:
        assert self.session is not None
        if self.in_chat:
            return self._chat_turn(text)
        return TurnRequest(
            speaker_id=self.composer.speaker_id(),
            user_text=text,
            controlled_character_id=self.composer.held_character_id(),
            ooc=self.composer.ooc_text(),
            npc_scope=self.composer.npc_scope(),
            npc_scope_ids=self.composer.npc_scope_ids(),
            agency_mode=self.composer.agency_mode(),
            response_style=self.composer.response_style(),
            dice_domain=self.composer.dice_domain(),
            difficulty=self.composer.difficulty_level(),
        )

    def _current_shape(self) -> TurnShape | None:
        """The composer's per-turn controls, for a retake that follows them.
        A chat has none: a retake is the same message again."""
        if self.in_chat:
            return None
        return TurnShape(
            response_style=self.composer.response_style(),
            agency_mode=self.composer.agency_mode(),
            npc_scope=self.composer.npc_scope(),
            npc_scope_ids=self.composer.npc_scope_ids(),
            difficulty=self.composer.difficulty_level(),
            dice_domain=self.composer.dice_domain(),
        )

    def _shape_summary(self) -> str:
        """What a retake is about to change to, named in the status bar."""
        length = self.composer.length.currentText().removesuffix(DEFAULT_SUFFIX)
        outcome = self.composer.agency.currentText().removesuffix(DEFAULT_SUFFIX)
        return f"Regenerating — {length}, {outcome}"

    # --- generation --------------------------------------------------------

    def send_turn(self) -> None:
        if self.session is None or (self._busy and not self._can_queue()):
            return
        text = self.composer.text()
        if not text or not self._tee_allows_send():
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return

        if self.composer.is_question():

            def ask() -> None:
                self.session.provider = self._current_provider()
                self._start(lambda: self.session.ask(text), question=text)

            self._queue_or_run(ask, text, None)
            return

        # Built now, from the composer as the author set it; if it has to wait
        # for the background reads, it goes as it was sent.
        turn = self._current_turn(text)
        label = speaker_label_for(turn.speaker_id, self.session.cast)

        def send() -> None:
            # A turn written under the new settings is the story moving on; the
            # review's snapshot no longer describes the settings as they stand.
            self.session.forget_review_undo()
            self.session.provider = self._current_provider()
            # Written at the newest message, wherever the reader was.
            self.transcript.ensure_at_latest()
            self.transcript.add_message(label, text, kind_for(turn.speaker_id), turn.ooc)
            self._start(lambda: self.session.send(turn))

        self._queue_or_run(send, text, turn.ooc, shown=(label, kind_for(turn.speaker_id)))

    def _can_queue(self) -> bool:
        return self._busy and self._background and self._queued is None

    def _queue_or_run(
        self,
        run: Callable[[], None],
        text: str,
        ooc: str | None,
        *,
        shown: tuple[str, str] | None = None,
    ) -> None:
        """Send now, or, while the last turn's background reads run, once they finish.

        The reads are the worker's; the author's next turn can't start until
        they are done, but there is no reason to stop them writing it.
        """
        self.composer.clear_turn()
        if not self._busy:
            run()
            return
        self._queued = run
        self._queued_text = (text, ooc)
        if shown is not None:
            self.transcript.add_message(shown[0], text, shown[1], ooc)
        self.statusBar().showMessage(
            "Queued: your turn goes as soon as the background reads finish", 8000
        )
        self._update_controls()

    def _on_passage_done(self) -> None:
        """The passage is in: the composer opens while the reads run."""
        self._background = True
        self._update_controls()
        self.composer.focus_input()

    def _run_queued(self) -> None:
        run, self._queued, self._queued_text = self._queued, None, None
        if run is not None and self.session is not None and not self._busy:
            run()

    def _drop_queued(self) -> None:
        """Stopped before it could go: the turn goes back to the composer to send again."""
        if self._queued_text is not None:
            text, ooc = self._queued_text
            self.composer.input.setPlainText(text)
            if ooc:
                self.composer.ooc.setPlainText(ooc)
        self._queued = None
        self._queued_text = None

    def regenerate(self) -> None:
        if self.session is None or self._busy:
            return
        if not self.session.full_path() or not self._tee_allows_send():
            return
        self.session.provider = self._current_provider()
        # The composer's controls are what the author has in front of them, so
        # a retake follows them: "make it longer" then Regenerate used to come
        # back at exactly the old length.
        shape = self._current_shape()
        self.statusBar().showMessage(self._shape_summary(), 6000)
        self._start(lambda: self.session.regenerate(shape=shape))

    def reroll(self, node_id: str = "") -> None:
        """Roll again for the latest dice turn: a new sibling take, new outcome."""
        if self.session is None or self._busy:
            return
        path = self.session.full_path()
        if not path or (node_id and path[-1].id != node_id):
            return
        self.session.provider = self._current_provider()
        self._start(lambda: self.session.regenerate(reroll=True, shape=self._current_shape()))

    def _regenerate_node(self, node_id: str) -> None:
        """Another take of any passage; the old one stays as a sibling (§2.2)."""
        if self.session is None or self._busy:
            return
        path = self.session.full_path()
        if path and path[-1].id == node_id:
            self.regenerate()
            return
        self.session.provider = self._current_provider()
        # Show the story as it stood before that passage while the new take
        # streams in, so it doesn't appear below the later messages.
        before = path_to(self.session.nodes, node_id)[:-1]
        self._show_nodes(before)
        shape = self._current_shape()
        self.statusBar().showMessage(self._shape_summary(), 6000)
        self._focus_after_job = before[-1].id if before else None
        self._start(lambda: self.session.regenerate(node_id, shape=shape))

    def _take_back(self, node_id: str) -> None:
        """Return an author turn to the composer, and remove it and what followed.

        The manual version of this is copying the text out, deleting the turn
        and its reply, and pasting it back — which is what the author was doing
        to change a setting and send the same turn again.
        """
        if self.session is None or self._busy or self._refuse_outside_scene(node_id):
            return
        node = next((n for n in self.session.nodes if n.id == node_id), None)
        if node is None or node.kind != "user":
            return
        try:
            deletion = self.session.deletion_for(node_id)
        except KeyError:
            return

        after = deletion.on_path - 1
        details = ["The turn comes back in the composer with its settings, ready to edit and send."]
        if after:
            details.append(f"{after} message(s) after it will be removed.")
        if deletion.other_branches:
            details.append(f"{deletion.other_branches} other take(s) below it will go too.")
        if deletion.summaries:
            details.append(
                "The chapter summary covering it will be removed; the prose it "
                "covered goes back to the model word for word."
            )
        details.append("This can't be undone.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Take back this turn")
        box.setText("Take this turn back to the composer?")
        box.setInformativeText("\n\n".join(details))
        take_back = box.addButton("Take back", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(take_back)
        box.exec()
        if box.clickedButton() is not take_back:
            return

        self.composer.restore_turn(node)
        self.session.delete_from(node_id)
        self.reload_transcript()
        self.refresh_panels()
        self.refresh_summaries()
        self._reassemble_inspector()
        self._update_controls()
        self.statusBar().showMessage("Turn taken back — edit it and send again", 6000)

    # --- archival and summaries (§5.2, §5.3) --------------------------------

    def archive_now(self) -> None:
        """Archive the next chunk on demand, rather than waiting for the budget."""
        if self.session is None or self._busy:
            return
        if not self.session.next_chunk(allow_partial=True):
            self.statusBar().showMessage(
                "Not enough history to archive yet — the recent turns always stay verbatim", 6000
            )
            return
        self.session.provider = self._current_provider()
        self._start(self._archive_job, streaming=False, busy_text="Summarising the oldest chapter…")

    def _archive_job(self):
        assert self.session is not None
        # Asked for by hand, so a short chapter is the author's call (§5.2).
        chunk = self.session.next_chunk(allow_partial=True)
        if not chunk:
            return
        summary = self.session.archive(chunk)
        numbers = chapter_numbers(self.session.split().summaries)
        yield SessionNotice(
            f"Archived {turns_in(chunk)} turns into chapter "
            f"{numbers.get(summary.id) or chapter_number(self.session.summaries, summary)}"
        )
        yield from self.session.suggest_characters_after_archive(chunk)

    def _rebuild_summary(self, summary_id: str) -> None:
        if self.session is None or self._busy:
            return
        try:
            summary = self.session.summary_by_id(summary_id)
        except KeyError:
            return
        if summary.hand_edited and not self._confirm_overwrite([summary]):
            return
        self._rebuild([summary_id])

    def _rebuild_stale(self) -> None:
        """The banner's one action (§5.3)."""
        if self.session is None or self._busy:
            return
        stale = [summary for summary in self.session.split().summaries if summary.stale]
        hand_edited = [summary for summary in stale if summary.hand_edited]
        if hand_edited and not self._confirm_overwrite(hand_edited):
            return
        if stale:
            self._rebuild([summary.id for summary in stale])

    def _confirm_overwrite(self, summaries: list[Summary]) -> bool:
        """§5.2: rebuilding must confirm before discarding the author's words."""
        chapters = ", ".join(f"Chapter {number}" for number in self._chapter_numbers(summaries))
        answer = QMessageBox.question(
            self,
            "Overwrite your edits?",
            f"{chapters} was written by you. Rebuilding replaces your text with a "
            "fresh summary from the model, and your version is not kept.",
            QMessageBox.Cancel | QMessageBox.Yes,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes

    def _rebuild(self, summary_ids: list[str]) -> None:
        assert self.session is not None
        self.session.provider = self._current_provider()
        self._staleness_dismissed = False
        self._start(
            lambda: self._rebuild_job(summary_ids),
            streaming=False,
            busy_text="Rebuilding the chapter summaries…",
        )

    def _rebuild_job(self, summary_ids: list[str]):
        assert self.session is not None
        for summary_id in summary_ids:
            self.session.rebuild_summary(summary_id, force=True)
            yield SessionNotice(f"Rebuilt chapter summary ({summary_id[:8]})")

    def _save_summary(self, summary_id: str, content: str) -> None:
        if self.session is None:
            return
        self.session.edit_summary(summary_id, content)
        self.refresh_summaries()
        self._reassemble_inspector()
        self.statusBar().showMessage("Summary saved as your words", 4000)

    def _edit_node(self, node_id: str, content: str) -> None:
        """Edit any message and report what it made stale — never block it (§5.3)."""
        if self.session is None or self._busy:
            return
        try:
            went_stale = self.session.edit_node(node_id, content)
        except KeyError:
            return
        if went_stale:
            self._staleness_dismissed = False
        place = self.transcript.place_of(node_id)
        self.reload_transcript()
        self.transcript.keep_place(place)
        self.refresh_summaries()
        self._reassemble_inspector()
        if not went_stale:
            self.statusBar().showMessage("Message edited", 4000)

    def _delete_node(self, node_id: str) -> None:
        """Delete a message and everything after it, once the author confirms."""
        if self.session is None or self._busy or self._refuse_outside_scene(node_id):
            return
        try:
            deletion = self.session.deletion_for(node_id)
        except KeyError:
            return

        after = deletion.on_path - 1
        text = f"Delete this message and the {after} after it?" if after else "Delete this message?"
        details = []
        if deletion.other_branches:
            details.append(f"{deletion.other_branches} other take(s) below it will be deleted too.")
        if deletion.summaries:
            details.append(
                "The chapter summary covering it will be removed; the prose it "
                "covered goes back to the model word for word."
            )
        if deletion.asides:
            details.append(f"{len(deletion.asides)} question(s) asked here will go with it.")
        details.append("This can't be undone.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Delete messages")
        box.setText(text)
        box.setInformativeText("\n\n".join(details))
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return

        self.session.delete_from(node_id)
        self.reload_transcript()
        # Deleting steps the scene back to the one standing where the story now ends.
        self.refresh_panels()
        self.refresh_summaries()
        self._reassemble_inspector()
        self._update_controls()
        self.statusBar().showMessage(f"Deleted {len(deletion.node_ids)} message(s)", 5000)

    def _delete_aside(self, aside_id: str) -> None:
        if self.session is None or self._busy:
            return
        self.session.delete_aside(aside_id)
        self.reload_transcript()

    def _dismiss_staleness(self) -> None:
        self._staleness_dismissed = True
        self.staleness.hide()

    def _absorb_summary_costs(self) -> None:
        """Summarising is spend like any other; fold it into the session total."""
        if self.session is None:
            return
        for summary in self.session.summaries:
            key = (summary.id, summary.edited_at or summary.created_at)
            if key in self._counted_summaries:
                continue
            self._counted_summaries.add(key)
            self.status_strip.add_cost(summary.usage)

    def _start(
        self,
        factory,
        *,
        streaming: bool = True,
        question: str | None = None,
        busy_text: str | None = None,
        provider=None,
    ) -> None:
        """Run a job on the worker thread.

        `busy_text` is for jobs that don't stream into the transcript — a
        review, a summary, a scan: with nothing else moving on screen they
        looked as if they had failed, so a progress dialog says what's running
        and offers Stop.
        """
        if question is not None:
            self.transcript.begin_aside(question)
        elif streaming:
            self.transcript.begin_streaming()
        self._streaming_job = streaming
        # An aside isn't a turn: its cost is counted, but there is no passage
        # to validate and `last_result` still describes the last real turn.
        self._asking = question is not None
        self._asides_before = len(self.session.bundle.asides) if self.session else 0

        if provider is None and self.session is not None:
            # Stop reaches whichever model the job is talking to: in a private
            # scene, the private one.
            private = self.session.private_provider if self.session.in_private else None
            provider = private or self.session.provider
        worker = GenerationWorker(factory, provider=provider)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.text_delta.connect(self.transcript.append_streaming)
        worker.notice.connect(self._on_notice)
        worker.passage_done.connect(self._on_passage_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        # Cleanup hangs off the thread rather than the worker: the worker must
        # stay alive until its own thread has actually stopped, so dropping
        # references while `finished` is still being delivered would free the
        # object out from under Qt.
        thread.finished.connect(self._on_generation_finished)

        self._worker = worker
        self._thread = thread
        self._busy = True
        self._background = False
        self._update_controls()
        self._show_busy(busy_text)
        thread.start()

    def _show_busy(self, text: str | None) -> None:
        if text is None:
            return
        dialog = QProgressDialog(text, "Stop", 0, 0, self)
        dialog.setWindowTitle("SealedLore")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(420)
        dialog.canceled.connect(self._on_busy_stopped)
        dialog.show()
        self._busy_dialog = dialog

    def _on_busy_stopped(self) -> None:
        if self._busy_dialog is not None:
            self._busy_dialog.setLabelText("Stopping…")
        self.stop_generation()

    def _hide_busy(self) -> None:
        dialog, self._busy_dialog = self._busy_dialog, None
        if dialog is None:
            return
        # hide(), not close(): a QProgressDialog's close emits canceled.
        dialog.canceled.disconnect(self._on_busy_stopped)
        dialog.hide()
        dialog.deleteLater()

    def stop_generation(self) -> None:
        if self._busy and self._worker is not None:
            self._worker.stop()
            if self._background:
                self._drop_queued()
                self.statusBar().showMessage("Stopping the background reads", 4000)
            else:
                self.statusBar().showMessage("Stopping — partial text is kept", 4000)

    def _on_notice(self, text: str) -> None:
        self.statusBar().showMessage(text, 12000)

    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"Generation failed: {message}", 10000)
        # The message can carry the endpoint's own words; never render it as HTML.
        self._warn_plain("Generation failed", message)

    def _warn_plain(self, title: str, message: str) -> None:
        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, self)
        box.setTextFormat(Qt.PlainText)
        box.exec()

    def _open_samples(self) -> None:
        """The plot format guide and sample plots, in the file manager."""
        folder = samples_dir()
        if folder is None:
            self._warn_plain(
                "Samples not found",
                "The plot format guide and sample plots aren't installed with this copy "
                "of SealedLore (they are in SampleStories/ in the source).",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _no_story_page(self) -> QWidget:
        """The inspector while no story is open."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        title = QLabel("No story open")
        title.setObjectName("emptyTitle")
        text = QLabel(
            "This side shows the open story: its cast, the scene, lore, style, "
            "chapters, pictures and the prompt the storyteller was sent. Pick a "
            "story in Stories on the left, or start one."
        )
        text.setObjectName("hintLabel")
        text.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(text)
        layout.addStretch(1)
        return page

    def _show_about(self) -> None:
        """The version and where the data lives: what a bug report needs."""
        folder = self.root if self.root is not None else data_home()
        samples = samples_dir()
        QMessageBox.about(
            self,
            "About SealedLore",
            f"<b>SealedLore {__version__}</b><br><br>"
            f"Data folder: {folder}<br>"
            f"Sample stories and the plot format guide: {samples or 'not found'}<br>"
            f"PySide6 {pyside_version}",
        )

    def _on_generation_finished(self) -> None:
        self._busy = False
        self._background = False
        self._hide_busy()
        if self._close_when_idle:
            # thread.finished fires just before the thread exits; wait out that
            # moment, or closeEvent could still see it running and ignore the
            # close again with nothing left to retry it.
            if self._thread is not None:
                self._thread.wait()
            # The worker's finalizer has saved; closeEvent does the rest.
            QTimer.singleShot(0, self.close)
            return
        self.transcript.end_streaming()
        # The worker mutated the session on its own thread; rebuild the view
        # from story state now that it is finished, so the two cannot drift.
        self.reload_transcript()
        focus, self._focus_after_job = self._focus_after_job, None
        if focus is not None:
            QTimer.singleShot(0, lambda: self.transcript.scroll_to_node(focus))
        self.refresh_summaries()
        self.refresh_retrieval()
        if self._streaming_job:
            # The kept prompt is exactly what was sent, so show that.
            self.refresh_inspector()
        else:
            # Archival changed the story under it; the kept prompt is history.
            self._reassemble_inspector()

        self._absorb_summary_costs()
        # The log is the complete record: turns, summaries, asides and scans.
        self._refresh_story_cost()
        if self.session is not None:
            for usage in self.session.unreported_usage:
                self.status_strip.add_cost(usage)
            self.session.unreported_usage.clear()
            if self.session.character_suggestions:
                self.refresh_panels()
            # The scene read moves the roster on after every passage, and the
            # speakers the composer offers follow who is present.
            self._refresh_scene_panel()
            self._refresh_composer()
        if self._asking:
            if self.session is not None and len(self.session.bundle.asides) > self._asides_before:
                self.status_strip.add_usage(self.session.bundle.asides[-1].usage)
            self.stories.refresh(selected_id=self.session.story.id if self.session else None)
            self._update_controls()
            return
        result = self.session.last_result if self.session else None
        if self._streaming_job and result is not None:
            self.status_strip.add_usage(result.usage)
        self.stories.refresh(selected_id=self.session.story.id if self.session else None)
        self._update_controls()
        self._offer_pending_tiers()
        self._offer_pending_review()
        self._offer_pending_scene()
        self._sync_private_ui()
        self._offer_private_summary()
        if self._queued is not None:
            # After the thread has let go of the job, not inside its signal.
            QTimer.singleShot(0, self._run_queued)

    # --- panels ------------------------------------------------------------

    def _on_character_renamed(self, character_id: str, old_name: str) -> None:
        if self.session is None:
            return
        character = next(
            (c for c in (*self.session.cast, *self.session.supporting) if c.id == character_id),
            None,
        )
        if character is not None:
            self.session.rename_on_rosters(character, old_name)

    def _on_cast_changed(self) -> None:
        if self.session is None:
            return
        self.session.forget_review_undo()
        self.session.save()
        # Presence and the selectors are keyed on the cast, so they follow it.
        self._refresh_scene_panel()
        self._refresh_composer()
        self._reassemble_inspector()

    def _after_tier_change(self, select_id: str | None = None) -> None:
        assert self.session is not None
        self.refresh_panels()
        if select_id:
            self.cast_panel.select_character(select_id)
        self._reassemble_inspector()

    def _promote_character(self, character_id: str) -> None:
        if self.session is None or self._busy:
            return
        self.session.promote(character_id)
        self._after_tier_change(character_id)
        self.statusBar().showMessage(
            "Moved to the cast: on the roster now, and one cache miss next turn", 6000
        )

    def _demote_character(self, character_id: str) -> None:
        if self.session is None or self._busy:
            return
        try:
            self.session.demote(character_id)
        except ValueError:
            QMessageBox.information(
                self,
                "Playing this character",
                "You are playing this character. Choose someone else under Playing first.",
            )
            return
        self._after_tier_change(character_id)

    def _accept_suggestion(self, suggestion_id: str, to_cast: bool) -> None:
        if self.session is None or self._busy:
            return
        card = self.session.accept_suggestion(suggestion_id, to_cast=to_cast)
        self._after_tier_change(card.id)

    def _dismiss_suggestion(self, suggestion_id: str) -> None:
        if self.session is None or self._busy:
            return
        self.session.dismiss_suggestion(suggestion_id)
        self.refresh_panels()

    # --- authoring: drafting from a premise, reviewing settings ---------------

    def _needs_provider(self) -> bool:
        """True (after telling the author) when there is no endpoint to call."""
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return True
        return False

    def _remember_authoring_model(self, model: str) -> None:
        chosen = model.strip() or None
        if chosen != self.config.authoring_model:
            self.config.authoring_model = chosen
            save_config(self.config, root=self.root)

    def new_story_from_premise(self) -> None:
        if self._busy or self._needs_provider() or not self._private_allows_close():
            return
        provider_config = self.config.active_provider()
        chat_model = provider_config.model if provider_config and provider_config.model else ""
        dialog = GenerateDialog(
            self._current_provider(),
            model=self.config.authoring_model or "",
            default_model=chat_model,
            browse=self._browse_models,
            texts=texts_in_force(self.config.prompt_edits),
            parent=self,
            route_for=lambda model: config_route(self.config, "authoring", model),
        )
        accepted = dialog.exec() == GenerateDialog.Accepted
        self._remember_authoring_model(dialog.model_text())
        if not accepted or dialog.draft is None:
            return
        try:
            bundle, warnings = dialog.draft.build(main_model=chat_model or None, root=self.root)
        except ValueError as exc:
            self._warn_plain("Could not use the draft", str(exc))
            return
        self.stories.refresh(selected_id=bundle.story.id)
        self.open_story(bundle.story.id)
        if self.session is None or self.session.story.id != bundle.story.id:
            return
        if warnings:
            listing = "\n".join(f"• {warning}" for warning in warnings[:12])
            box = QMessageBox(
                QMessageBox.Information,
                "A few things were left out",
                f"The draft is saved, but these parts of it couldn't be used:\n\n{listing}",
                QMessageBox.Ok,
                self,
            )
            box.setTextFormat(Qt.PlainText)  # names and text from the model
            box.exec()
        # Read it before playing it: Setup first (the style too), then the Start
        # dialog. Cancel leaves the draft saved but not begun: before, the
        # opening was written anyway, in whatever style the draft had chosen.
        if self.open_setup():
            self.start_story()
        else:
            self.statusBar().showMessage(
                "The draft is saved but not begun: Story → Start story… when it's ready", 8000
            )

    def _toggle_review_mark(self, node_id: str) -> None:
        if self.session is None:
            return
        marked = self.session.mark_for_review(node_id)
        count = len(self.session.review_marks)
        self.statusBar().showMessage(
            (
                f"Marked as evidence for the next review ({count} marked)."
                if marked
                else f"No longer marked ({count} marked)."
            ),
            6000,
        )

    def review_settings(self) -> None:
        if self.session is None or self._busy or self._needs_provider():
            return
        last = self.session.last_review
        dialog = ReviewRequestDialog(
            sizes=self.session.review_sizes(),
            model=self.config.authoring_model or "",
            default_model=self.session.authoring_model,
            context_for=self.session.cached_context_length,
            price_for=self.session.cached_price,
            output_limit_for=self.session.cached_output_limit,
            browse=self._browse_models,
            last_complaint=last.complaint if last else "",
            clear_marks=self.session.review_marks.clear,
            parent=self,
        )
        accepted = dialog.exec() == ReviewRequestDialog.Accepted
        if not accepted:
            self.reload_transcript()  # the marks may have been cleared
            return
        self._remember_authoring_model(dialog.model_text())
        complaint = dialog.complaint_text()
        replies = dialog.reply_count()
        whole_prompt = dialog.include_whole_prompt()
        side_texts = dialog.include_side_texts()
        model = dialog.model_text() or None
        self.session.provider = self._current_provider()
        self._pending_review = None
        self._review_request = (complaint, model or self.session.authoring_model)
        self._start(
            lambda: self._review_job(complaint, replies, whole_prompt, model, side_texts),
            streaming=False,
            busy_text=(
                f"Reviewing this story's settings with {model or self.session.authoring_model}…"
                "\n\nThis usually takes between ten seconds and a minute."
            ),
        )

    def _review_job(
        self,
        complaint: str,
        replies: int,
        whole_prompt: bool,
        model: str | None,
        side_texts: bool = False,
    ):
        assert self.session is not None
        review = self.session.review_settings(
            complaint,
            replies=replies,
            whole_prompt=whole_prompt,
            model=model,
            side_texts=side_texts,
        )
        self._pending_review = review
        count = len(review.changes)
        yield SessionNotice(f"Review ready: {count} proposed change{'s' if count != 1 else ''}")

    def _offer_pending_review(self) -> None:
        """Show the proposals; nothing changes unless the author ticks and applies."""
        if self._pending_review is None or self.session is None:
            return
        review, self._pending_review = self._pending_review, None
        complaint, model = self._review_request
        dialog = ReviewResultDialog(
            review, self.session.bundle, parent=self, complaint=complaint, model=model
        )
        if dialog.exec() != ReviewResultDialog.Accepted:
            self._update_controls()
            return
        chosen = dialog.selected_changes()
        send_direction = dialog.send_director_turn() and bool(review.director_turn)
        results = self.session.apply_review(chosen, sent_director_turn=send_direction)
        applied = [change for change in chosen if results.get(change.id) is None]
        problems = [
            f"{describe(change)}: {results[change.id]}"
            for change in chosen
            if results.get(change.id)
        ]
        if any(c.kind == "model" and c.field == "main_model" for c in applied):
            self._set_default_model(self.session.story.defaults.main_model)
        self.composer.set_default_agency(self.session.story.defaults.agency_mode)
        self.setWindowTitle(f"SealedLore — {self.session.story.title}")
        self.stories.refresh(selected_id=self.session.story.id)
        self.refresh_panels()
        self._refresh_composer()
        self._reassemble_inspector()
        self._update_controls()
        if send_direction and review.director_turn:
            self.composer.set_direction(review.director_turn)
        if applied:
            names = ", ".join(describe(change) for change in applied)
            self.statusBar().showMessage(
                f"Applied: {names}. The next turn uses the new settings; Story → Undo review "
                "changes puts them back.",
                15000,
            )
        if problems:
            self._warn_plain(
                "Some changes weren't applied",
                "\n".join(f"• {problem}" for problem in problems),
            )
        self._offer_restart_after_review(review, applied)

    def _characters_changed_by(self, changes: Sequence[SettingsChange]) -> list[Character]:
        """The cards a review touched, for the branch offer."""
        assert self.session is not None
        names = {
            (change.target or "").strip().lower()
            for change in changes
            if change.kind in ("character", "character_move")
        }
        names |= {
            str(change.value.get("name", "")).strip().lower()
            for change in changes
            if change.kind == "character_add" and isinstance(change.value, dict)
        }
        return [
            character
            for character in (*self.session.cast, *self.session.supporting)
            if character.name.strip().lower() in names
        ]

    def _offer_restart_after_review(
        self, review: SettingsReview, applied: Sequence[SettingsChange]
    ) -> None:
        """The story so far is the strongest signal the storyteller has, so a
        changed card may not take against it: offer a fresh start or a branch
        from before the character's first appearance."""
        assert self.session is not None
        changed = self._characters_changed_by(applied)
        if not (review.history_bound or changed) or not self.session.nodes:
            return
        path = self.session.path()
        position = {node.id: index for index, node in enumerate(path, start=1)}
        # The earliest first appearance among the changed cards, if it isn't
        # the very first message: branching from before it re-introduces them.
        first: tuple[int, Character, Node] | None = None
        for character in changed:
            node = self.session.first_appearance(character)
            if node is not None and position[node.id] > 1:
                candidate = (position[node.id], character, node)
                if first is None or candidate[0] < first[0]:
                    first = candidate

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("The story so far")
        box.setText("The settings are changed, but the story so far may pull against them.")
        details = [
            "The storyteller reads the recent story every turn, and it is the strongest "
            "signal it has: a character written one way for many passages tends to stay "
            "that way whatever their card now says."
        ]
        if review.director_turn:
            details.append("The Director turn in the composer tells it what changes from here.")
        details.append(
            "To have things be as they should from the start, you can restart the story "
            "with these settings as a new playthrough (this one is kept)"
            + (
                f", or branch from before {first[1].name} first appeared (message {first[0]}) "
                "and play on from there."
                if first
                else "."
            )
        )
        box.setInformativeText("\n\n".join(details))
        restart = box.addButton("Restart with these settings", QMessageBox.AcceptRole)
        branch = (
            box.addButton(f"Branch before message {first[0]}", QMessageBox.ActionRole)
            if first
            else None
        )
        box.addButton("Continue from here", QMessageBox.RejectRole)
        box.setDefaultButton(QMessageBox.StandardButton.NoButton)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restart:
            self.restart_story()
        elif branch is not None and clicked is branch and first is not None:
            parent_id = first[2].parent_id
            if parent_id is not None:
                name = f"Before {first[1].name} appeared"
                self.session.branch_from(parent_id, name)
                self._after_navigation(parent_id)
                self.statusBar().showMessage(
                    f"Continuing from message {first[0] - 1} as the branch “{name}”, which "
                    "starts with your next turn; the story as it was is kept. The next "
                    "passage will introduce them afresh.",
                    10000,
                )

    def show_last_review(self) -> None:
        if self.session is None or self._busy or self.session.last_review is None:
            return
        record = self.session.last_review
        dialog = ReviewResultDialog(
            record.review,
            self.session.bundle,
            parent=self,
            complaint=record.complaint,
            model=record.model,
            applied_ids=record.applied_ids,
            read_only=True,
        )
        dialog.exec()

    def undo_review(self) -> None:
        if self.session is None or self._busy or self.session.review_undo is None:
            return
        if not self.session.undo_review():
            return
        self.composer.set_default_agency(self.session.story.defaults.agency_mode)
        self.setWindowTitle(f"SealedLore — {self.session.story.title}")
        self.stories.refresh(selected_id=self.session.story.id)
        self.refresh_panels()
        self._refresh_composer()
        self._reassemble_inspector()
        self._update_controls()
        self.statusBar().showMessage("The settings are back as they were before the review.", 8000)

    # --- models -------------------------------------------------------------

    def _model_source(self) -> tuple[ChatProvider, str, bool]:
        provider_config = self.config.active_provider()
        if provider_config is None and not self.use_mock:
            raise ValueError("Set a base URL and API key in Settings first.")
        endpoint = provider_config.base_url if provider_config else "mock"
        return self._current_provider(), endpoint, False

    def _browse_models(self, current: str, **limits) -> str | None:
        return pick_model(self.catalog, self._model_source, current, self, **limits)

    def _browse_models_and_route(self, current: str, route):
        """Browse… for a field with a route (New simple chat)."""
        provider = self.config.active_provider()
        endpoint = provider.base_url if provider is not None and not self.use_mock else None
        return pick_model_and_route(
            self.catalog, self._model_source, current, route, endpoint, self
        )

    def _host_price(self, model: str, host: str) -> HostPrice | None:
        """A host's listed price, if its model's hosts have been fetched. On
        the reply's thread: it reads the catalog's kept list and fetches
        nothing (`_fetch_host_prices` does, when a story opens)."""
        provider = self.config.active_provider()
        found = hosts_catalog().hosts(provider.base_url, model) if provider else None
        listed = found.host(host) if found is not None else None
        if listed is None or listed.input_price is None or listed.output_price is None:
            return None
        return HostPrice(listed.input_price, listed.output_price, listed.cache_read_price)

    def _fetch_host_prices(self) -> None:
        """The hosts of every model routed to one host, so its replies can
        be checked against its price."""
        provider = self.config.active_provider()
        if provider is None or self.use_mock:
            return
        routes = list(self.config.model_routes.values())
        if self.session is not None and self.session.story.defaults.main_route:
            routes.append(self.session.story.defaults.main_route)
        for route in routes:
            if route.priority == "host" and route.host_model:
                hosts_catalog().ensure(provider.base_url, route.host_model)

    def _hosts_of(self, model: str) -> tuple[str, ...] | None:
        """A model's hosts from the endpoint's list; None until it's loaded."""
        info = (self.catalog.models or {}).get(model) if self.catalog.models else None
        if not self.catalog.models:
            return None
        return info.hosts if info is not None else ()

    def _can_regenerate(self) -> bool:
        if self.session is None:
            return False
        path = self.session.full_path()
        # A private scene's summary is the author's approved text, not a take.
        return bool(path) and path[-1].meta.private_summary_of is None

    def _refresh_model_button(self) -> None:
        # The line above the transcript says the same things, and more; an
        # attestation finishing refreshes only this.
        self.session_line.show_session(self.session, tee_state=self._tee_state)
        private = self._private_model_text()
        if private is not None:
            self.model_button.setText(private)
            if self.in_private:
                self.model_button.setToolTip(
                    self._say(
                        "A private scene: every turn goes to the private model only. End "
                        "it with the button beside the composer."
                    )
                )
                self.model_button.setEnabled(False)
            else:
                self.model_button.setToolTip(
                    "A TEE chat: every call goes to this model, attested when the chat "
                    "opened. Click to move to another TEE model."
                )
                self.model_button.setEnabled(not self._busy)
            self.model_button.setVisible(True)
            return
        model = self.session.model if self.session else ""
        # The id's last part reads best in a status bar: "claude-sonnet-4.6".
        self.model_button.setText(f"{model.rsplit('/', 1)[-1] or 'no model'} ▾")
        self.model_button.setToolTip(
            f"Storyteller model: {model or 'none set'}. Click to choose another."
        )
        self.model_button.setVisible(self.session is not None)
        self.model_button.setEnabled(self.session is not None and not self._busy)

    def choose_story_model(self) -> None:
        """The model button: pick this story's storyteller from the endpoint's list."""
        if self.session is None or self._busy:
            return
        chosen = self._browse_models(self.session.model, **self._chat_model_limits())
        if not chosen or chosen == self.session.model:
            return
        if not self._chat_model_allowed(chosen):
            return
        self.session.story.defaults.main_model = chosen
        if not self.session.memory_only:
            # A memory-only chat teaches the config nothing, its model included;
            # Settings changes an open story's model only when its field changed.
            self._set_default_model(chosen)
        self.session.save()
        if self.in_chat:
            self._chat_model_changed()
        self._reassemble_inspector()
        self._update_controls()
        self.statusBar().showMessage(
            f"Storyteller model: {chosen}. The next turn re-sends the whole prompt once.", 8000
        )

    def _set_default_model(self, model: str | None) -> None:
        """Make `model` the provider's default too, as saving Settings does.

        Settings writes the provider's model onto the open story when saved, so
        a story model set anywhere else has to become the provider's as well, or
        the next save of Settings would quietly put the old one back.
        """
        provider_config = self.config.active_provider()
        if model and provider_config is not None and provider_config.model != model:
            provider_config.model = model
            save_config(self.config, root=self.root)

    def infer_competence(self, character_id: str) -> None:
        if self.session is None or self._busy:
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return
        self.session.provider = self._current_provider()
        self._pending_tiers = None
        character = next(
            (c for c in (*self.session.cast, *self.session.supporting) if c.id == character_id),
            None,
        )
        self._start(
            lambda: self._infer_job(character_id),
            streaming=False,
            busy_text=f"Suggesting skills for {character.name if character else 'this character'}…",
        )

    def _infer_job(self, character_id: str):
        assert self.session is not None
        tiers = self.session.infer_competence(character_id)
        self._pending_tiers = (character_id, tiers)
        yield SessionNotice(f"Suggested {len(tiers)} skills")

    def _offer_pending_tiers(self) -> None:
        """Show drafted skills; nothing changes unless the author accepts (§2.3)."""
        if self._pending_tiers is None or self.session is None:
            return
        character_id, tiers = self._pending_tiers
        self._pending_tiers = None
        character = next(
            (c for c in (*self.session.cast, *self.session.supporting) if c.id == character_id),
            None,
        )
        if character is None:
            return
        listing = "\n".join(f"  {domain} — {tier}" for domain, tier in tiers.items())
        box = QMessageBox(self)
        box.setWindowTitle("Suggested skills")
        box.setText(f"Suggested skills for {character.name}:\n\n{listing}")
        if character.competence.tiers:
            box.setInformativeText("These replace the current skills. You can edit them after.")
        else:
            box.setInformativeText("You can edit them after.")
        use = box.addButton("Use these", QMessageBox.AcceptRole)
        box.addButton("Discard", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is use:
            self.cast_panel.show_suggested_skills(character_id, tiers)

    def scan_for_characters(self) -> None:
        if self.session is None or self._busy:
            return
        if not self.session.path():
            self.statusBar().showMessage("Nothing written yet to look through", 4000)
            return
        if self.config.active_provider() is None and not self.use_mock:
            QMessageBox.information(
                self,
                "No provider configured",
                "Set a base URL, API key and model in Settings first.",
            )
            return
        self.session.provider = self._current_provider()
        self._start(
            self.session.scan_recent,
            streaming=False,
            busy_text="Looking for new characters in the recent story…",
        )

    def _on_scene_changed(self) -> None:
        if self.session is None:
            return
        # Stamped on the passage the story has reached: the author's card is
        # the scene from here, and the next read starts from it.
        self.session.set_scene(self.session.story.scene)
        self.scene_panel.show_changes(self.session.story.scene, passage=0, can_undo=False)
        self.scene_panel.show_age(
            self.session.scene_age(), kept=self.config.scene_reads == "every_turn"
        )
        self._refresh_composer()
        self._reassemble_inspector()

    def _undo_scene_update(self) -> None:
        if self.session is None or self._busy:
            return
        if self.session.undo_scene_update():
            self._refresh_scene_panel()
            self._refresh_composer()
            self._reassemble_inspector()
            self.statusBar().showMessage("Scene put back as it was before this passage", 5000)

    def _refresh_scene_panel(self) -> None:
        if self.session is None:
            return
        session = self.session
        # Supporting characters as the Cast page shows them: the plot's
        # hidden ones stay out unless spoilers are on.
        hidden = set() if self.config.show_plot_spoilers else session.hidden_ids()
        self.scene_panel.set_scene(
            session.story.scene,
            session.cast,
            supporting=[c for c in session.supporting if c.id not in hidden],
            held_id=session.story.held_character_id,
            log=session.scene_log_on_path(),
        )
        self.scene_panel.show_changes(
            session.story.scene, passage=len(session.path()), can_undo=session.scene_changed_here()
        )
        self.scene_panel.show_age(session.scene_age(), kept=self.config.scene_reads == "every_turn")
        self._refresh_plot_panel()

    def _refresh_plot_panel(self) -> None:
        """The plot's clock and facts at the leaf; they follow it like the scene.

        The Plot page is shown only for a story with a plot: for the rest it
        held a paragraph saying there was none (Story → Import plot… gives
        one, and Help → Plot format guide and samples explains them)."""
        session = self.session
        plot = session.story.plot if session is not None else None
        self.right_tabs.setTabVisible(self.right_tabs.indexOf(self.plot_panel), plot is not None)
        chronicle = session.chronicle if session is not None else None
        started = bool(session and session.path())
        days = session.plot_days() if session is not None and plot is not None else {}
        armed_ids = (
            frozenset(item.event.id for item in armed(plot, chronicle, days))
            if plot is not None and chronicle is not None
            else frozenset()
        )
        held_back: list[tuple[str, str, bool]] = []
        if session is not None and plot is not None:
            still_hidden = session.hidden_ids()
            held_back = [
                (
                    item.id,
                    getattr(item, "name", None) or getattr(item, "title", ""),
                    item.id not in still_hidden,
                )
                for item in (*session.cast, *session.supporting, *session.bundle.lore)
                if item.plot_hidden
            ]
        self.plot_panel.set_plot(
            plot,
            chronicle,
            started=started,
            spoilers=self.config.show_plot_spoilers,
            days=days,
            armed_ids=armed_ids,
            held_back=held_back,
        )
        self.plot_panel.show_changes(
            chronicle,
            passage=len(session.path()) if session else 0,
            can_undo=bool(session and session.chronicle_changed_here()),
        )
        self.clock_button.setVisible(chronicle is not None)
        # An event that brings someone in, or a move to a branch where it
        # hasn't yet, changes what the Cast and Lore tabs may show. Rebuilt
        # only when that changes: rebuilding on every refresh would reset a
        # card the author is in the middle of editing.
        if session is not None and not self.config.show_plot_spoilers:
            hidden = session.hidden_ids()
            if hidden != self._shown_hidden:
                self._shown_hidden = hidden
                self.cast_panel.set_cast(session.cast, hidden=hidden)
                self.lore_panel.set_entries(session.bundle.lore, hidden=hidden)
                self._refresh_composer()
        if chronicle is not None:
            self.clock_button.setText(format_clock(chronicle.minutes))

    def set_story_clock(self) -> None:
        if self.session is None or self._busy or (chronicle := self.session.chronicle) is None:
            return
        dialog = ClockDialog(chronicle.minutes, parent=self)
        if dialog.exec() != ClockDialog.Accepted:
            return
        self.session.set_clock(dialog.minutes())
        self._refresh_plot_panel()
        self._reassemble_inspector()

    def _on_fact_changed(self, name: str, value: str) -> None:
        if self.session is None or self._busy:
            return
        try:
            self.session.set_fact(name, value)
        except ValueError as exc:
            self._warn_plain("Couldn't set the fact", str(exc))
        self._refresh_plot_panel()
        self._reassemble_inspector()

    def _on_spoilers_toggled(self, on: bool) -> None:
        self.config.show_plot_spoilers = on
        save_config(self.config, root=self.root)
        self.refresh_panels()

    def _on_event_marked(self, event_id: str, state: str) -> None:
        if self.session is None or self._busy:
            return
        try:
            self.session.mark_event(event_id, state)  # type: ignore[arg-type]
        except ValueError as exc:
            self._warn_plain("Couldn't mark the event", str(exc))
        self._refresh_plot_panel()
        self._reassemble_inspector()

    def _on_bring_in(self, item_id: str) -> None:
        if self.session is None or self._busy:
            return
        try:
            self.session.bring_in(item_id)
        except ValueError as exc:
            self._warn_plain("Couldn't bring them in", str(exc))
        self._refresh_plot_panel()
        self._reassemble_inspector()

    def _undo_chronicle_update(self) -> None:
        if self.session is None or self._busy:
            return
        if self.session.undo_chronicle_update():
            self._refresh_plot_panel()
            self._reassemble_inspector()
            self.statusBar().showMessage(
                "Clock and facts put back as they were before this passage", 5000
            )

    def update_scene_from_story(self) -> None:
        """Read the scene back out of the recent prose, for the author to approve."""
        if self.session is None or self._busy:
            return
        self._pending_scene = None
        self._start(
            self._scene_update_job,
            streaming=False,
            busy_text=(
                f"Reading the recent passages with {self.session.scene_model}…\n\n"
                "This usually takes a few seconds."
            ),
        )

    def _scene_update_job(self):
        assert self.session is not None
        self._pending_scene = self.session.suggest_scene()
        yield SessionNotice("Scene read from the story")

    def _offer_pending_scene(self) -> None:
        """Show what the model read, and apply it only if the author says so."""
        proposal = self._pending_scene
        self._pending_scene = None
        if proposal is None or self.session is None:
            return
        scene = self.session.story.scene
        names = {character.id: character.name for character in self.session.cast}
        lines = []
        for label, before, after in (
            ("Location", scene.location, proposal.location),
            ("Time", scene.time_of_day, proposal.time_of_day),
            ("Situation", scene.situation, proposal.situation),
        ):
            if after and after != before:
                lines.append(f"{label}: {after}")
        if proposal.found_anyone and (
            proposal.present_character_ids != list(scene.present_character_ids)
            or proposal.present_others != list(scene.present_others)
        ):
            here = [
                names.get(character_id, character_id)
                for character_id in proposal.present_character_ids
            ]
            lines.append(f"In the scene: {', '.join(here) or '(no cast member)'}")
            if proposal.present_others:
                lines.append(f"Also here: {', '.join(proposal.present_others)}")
        if proposal.privacy and proposal.privacy != scene.privacy:
            lines.append(f"Privacy: {proposal.privacy}")
        if not lines:
            self.statusBar().showMessage("The scene already matches the story", 5000)
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Update the scene")
        box.setText("Set the scene to what the story says?")
        box.setInformativeText("\n\n".join(lines))
        box.setTextFormat(Qt.PlainText)
        apply_it = box.addButton("Update scene", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(apply_it)
        box.exec()
        if box.clickedButton() is not apply_it:
            return

        leaf = self.session.path()
        self.session.set_scene(proposal.applied_to(scene, leaf_id=leaf[-1].id if leaf else None))
        self.refresh_panels()
        self._refresh_composer()
        self._reassemble_inspector()
        self.statusBar().showMessage("Scene updated from the story", 5000)

    def _on_lore_changed(self) -> None:
        if self.session is None:
            return
        self.session.forget_review_undo()
        self.session.save()
        # Vectors are keyed by content hash, so an edited entry re-embeds
        # itself on the next turn; nothing to do here but persist.
        self._reassemble_inspector()

    def reembed_lore(self) -> None:
        """Rebuild every vector — for when the model changed outside the app."""
        if self.session is None or self._busy:
            return
        if self.session.embeddings is None:
            self.statusBar().showMessage(
                "No embeddings endpoint configured — retrieval is using keywords", 6000
            )
            return
        self._start(self._reembed_job, streaming=False, busy_text="Embedding the lorebook…")

    def _reembed_job(self):
        assert self.session is not None
        self.session.bundle.embeddings.clear()
        vectors = self.session.ensure_lore_vectors()
        yield SessionNotice(f"Embedded {len(vectors)} lore entries")

    def _on_held_changed(self, character_id: str) -> None:
        """Taking up a character hands them the voice, and the scene follows them.

        Someone the scene knows to be elsewhere is not pulled into it: the
        story moves to where they are (StorySession.hold). Human testing found
        the other half of this: holding Paul while the roster said he was
        elsewhere told the model the author's own character couldn't see or
        hear the scene, and since only present characters can be speakers, the
        turn went in as a first-person Director message instead.
        """
        if self.session is None:
            return
        moved = self.session.hold(character_id or None)
        self._refresh_scene_panel()
        self._refresh_composer()
        if character_id:
            self.composer.select_speaker(character_id)
        self._reassemble_inspector()
        if moved:
            name = next((c.name for c in self.session.cast if c.id == character_id), "them")
            self.statusBar().showMessage(
                f"The scene moves to {name}. The next passage fills in who is with them.", 8000
            )

    def _refresh_composer(self) -> None:
        assert self.session is not None
        story = self.session.story
        self.composer.refresh(
            self.session.visible_cast(),
            story.scene,
            story.held_character_id,
            default_length=story.style.response_style,
        )
        self.composer.set_default_agency(story.defaults.agency_mode)

    def _on_style_changed(self) -> None:
        if self.session is None:
            return
        self.session.forget_review_undo()
        self.session.save()
        self.composer.set_default_length(self.session.story.style.response_style)
        self._reassemble_inspector()

    def _reassemble_inspector(self) -> None:
        """Re-derive the prompt because story state changed since the last turn.

        The inspector otherwise keeps showing the last request that was sent,
        which is what you want straight after a turn and misleading after an
        edit, an archival or a change to the cast, scene or held character.
        """
        if self.session is None:
            return
        self.session.last_prompt = None
        self.refresh_inspector()

    def refresh_inspector(self) -> None:
        if self.session is None:
            return
        prompt = self.session.last_prompt
        if prompt is None:
            draft = self.composer.text() or "(nothing typed yet)"
            if self.composer.is_question():
                prompt = self.session.assemble_question(draft)
            else:
                prompt = self.session.assemble(self._current_turn(draft))
        self.inspector.show_prompt(
            prompt,
            model=self.session.model,
            params=self.session.story.defaults.generation,
            use_cache_control=self.session.uses_cache_control(),
            approximate=self.estimator.is_approximate,
            warnings=self._prompt_warnings(),
            extra_body=self.session.route_for("story"),
        )
        self.status_strip.set_budget(prompt.budget, approximate=self.estimator.is_approximate)

    def _prompt_warnings(self) -> list[str]:
        stale = stale_voicing_notes(self.session.story.style.notes, self.session.cast)
        if not stale:
            return []
        return [
            f"Style notes say who plays whom ({len(stale)} sentence"
            f"{'s' if len(stale) != 1 else ''}): the roster already does, and changes when "
            "you switch. Remove them from Style → Other notes, or let a settings review do it."
        ]

    def open_settings(self) -> None:
        # Applying settings replaces the provider and saves the story, and the
        # worker is the only writer while it runs.
        if self._busy or self._settings_paused():
            return
        story = self.session.story if self.session else None
        model_before = self.session.model if self.session else None
        dialog = SettingsDialog(
            self.config,
            story,
            self,
            catalog=self.catalog,
            image_catalog=self.image_catalog,
            fetch_models=not self.use_mock,
        )
        if dialog.exec() == SettingsDialog.Accepted:
            save_config(self.config, root=self.root)
            self._discard_provider()
            if self.session is not None:
                # Undo restores the story's settings wholesale; after a save
                # here it would take these edits with it.
                self.session.forget_review_undo()
                self.session.save()
                self.session.provider = self._current_provider()
                self.session.embeddings = self._current_embeddings()
                self.lore_panel.set_embeddings_available(self.session.embeddings is not None)
                self._reassemble_inspector()
                # The budget or the archival switch may have changed.
                self.refresh_summaries()
                if self.in_chat and self.session.model != model_before:
                    self._chat_model_changed()
            else:
                # The first-run welcome gives way, or says what is still missing.
                self.reload_transcript()
            self._update_controls()

    def open_prompt_editor(self) -> None:
        """File → Prompts (advanced)…: the texts for this story and for every story."""
        if self._busy or self._settings_paused():
            return
        story = self.session.story if self.session else None
        dialog = PromptEditorDialog(self.config, story, root=self.root, parent=self)
        if dialog.exec() != PromptEditorDialog.Accepted:
            return
        dialog.apply_to(self.config, story)
        save_config(self.config, root=self.root)
        if self.session is not None:
            # Undo restores the story's settings wholesale; after a save here
            # it would take these edits with it.
            self.session.forget_review_undo()
            self.session.save()
            self.style_panel.set_style(self.session.story.style, texts=self.session.texts)
            self._reassemble_inspector()
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self._busy
        has_story = self.session is not None
        self._refresh_model_button()
        self.composer.set_busy(
            busy,
            has_story=has_story,
            can_regenerate=self._can_regenerate(),
            background=self._background,
            queued=self._queued is not None,
        )
        self.stories.setEnabled(not busy)
        self.close_story_action.setEnabled(has_story and not busy)
        self.status_strip.set_routes(self.session.route_checks() if has_story else [])
        self.settings_action.setEnabled(not busy)
        self.prompts_action.setEnabled(not busy)
        # With no story the inspector shows its empty page, not greyed forms
        # that look usable and answer nothing (their scrollbars included).
        self.right_tabs.show_empty(not has_story)
        self.cast_panel.setEnabled(has_story and not busy)
        self.scene_panel.setEnabled(has_story and not busy)
        self.plot_panel.setEnabled(has_story and not busy)
        self.clock_button.setEnabled(has_story and not busy)
        self.summaries_panel.setEnabled(has_story and not busy)
        self.style_panel.setEnabled(has_story and not busy)
        self.find_action.setEnabled(has_story)
        self.find_button.setEnabled(has_story)
        if self.session is not None:
            # A story that has begun keeps its person and tense.
            self.style_panel.set_started(bool(self.session.nodes))
        self.lore_panel.setEnabled(has_story and not busy)
        self.last_review_action.setEnabled(
            has_story and not busy and bool(self.session and self.session.last_review)
        )
        self.undo_review_action.setEnabled(
            has_story and not busy and bool(self.session and self.session.review_undo)
        )
        for action in (
            self.setup_action,
            self.review_action,
            self.import_plot_action,
            self.restart_action,
            self.export_action,
            self.archive_action,
            self.markdown_action,
            self.chapter_markdown_action,
            self.usage_action,
        ):
            action.setEnabled(has_story and not busy)
        self.start_action.setEnabled(
            has_story and not busy and not (self.session and self.session.nodes)
        )
        self._update_image_controls()
        self._update_private_controls()
        if self.in_chat:
            # What only a story has; and nothing written for a memory-only chat.
            for action in (
                self.start_action,
                self.review_action,
                self.import_plot_action,
                self.last_review_action,
                self.undo_review_action,
            ):
                action.setEnabled(False)
            if self.memory_chat:
                # Its pictures are kept in memory with it (storage.picture_store),
                # and it can be exported (a backup, Markdown) but not restarted
                # or made a scenario.
                for action in (self.restart_action, self.export_action):
                    action.setEnabled(False)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_centre_width()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # The editor's unsaved file gets its own say before the app goes.
        if self._plot_editor is not None and self._plot_editor.isVisible():
            self._plot_editor.close()
            if self._plot_editor.isVisible():
                event.ignore()
                return
        if self._thread is not None and self._thread.isRunning():
            # A stream notices stop() only at its next chunk, so waiting here
            # with a timeout could save under the worker and destroy a running
            # thread. Close once thread.finished says it is done instead.
            if self._worker is not None:
                self._worker.stop()
            self._close_when_idle = True
            self.statusBar().showMessage("Stopping — closes when the current response ends")
            event.ignore()
            return
        if not self._images_allow_close() or not self._private_allows_close():
            event.ignore()
            return
        self._wait_for_attestation()
        self.catalog.wait()
        attestation_tests().wait()
        speed_tests().wait()
        hosts_catalog().wait()
        if self.session is not None:
            self.session.close()
            self.session.save()
        self._discard_provider()
        save_config(self.config, root=self.root)
        super().closeEvent(event)
