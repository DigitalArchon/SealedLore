"""How fast the transcript builds, on a synthetic long story (no model, no network).

Asked for by the author (Sept 2026): scrolling back to the start of a long
story, or a Find match near it, built every message in between (~7.5s for
516 messages on the display). This measures that, and where the time goes,
so a speed-up is kept only if it shows here.

usage (from the repo root, venv active):
    python tools/bench/transcript_bench.py [--label NAME] [--exchanges 250]
        [--repeats 3] [--profile] [--display]

Offscreen by default, with the app's own stylesheet and font (without the
stylesheet the column lays its messages out zero high). --display runs on
$DISPLAY instead, for one confirming run. Results are printed and appended
to results.md beside this script (git-ignored) under --label.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

ARGS = argparse.ArgumentParser()
ARGS.add_argument("--label", default="run")
ARGS.add_argument("--exchanges", type=int, default=250)
ARGS.add_argument("--repeats", type=int, default=3)
ARGS.add_argument("--profile", action="store_true")
ARGS.add_argument("--display", action="store_true")
# Measuring only: what the app-wide wheel guard (gui/fields.py) costs.
ARGS.add_argument("--no-wheel-guard", action="store_true")
ARGS.add_argument("--skip-text-size", action="store_true")
# Measuring only: the speed-ups undone at run time, for a before/after in the
# same conditions (the app-wide wheel guard, no height cache, the tree walked
# once per message). The Edit button's window per message can't be undone
# here: compare with a run from before that change.
ARGS.add_argument("--old", action="store_true")
OPTS = ARGS.parse_args()
if not OPTS.display:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

import sealedlore.gui.transcript as transcript  # noqa: E402
from sealedlore.gui.app import build_app  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.aside import Aside  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.node import Node, NodeMeta, Roll, Usage  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.tree import link_child  # noqa: E402

WORDS = (
    "the town slept through its long night while light from the watchtower spilled "
    "across the main street and somewhere below a door banged shut she turned "
    "toward the radio reading numbers that made no sense he did not answer at once "
    "the guard glanced at the gate then back at the stranger by the fire cold "
    "air moved over the ice outside and the shelter held as it always did quietly "
    "careful weight of a question nobody wanted to ask first the chalk board still "
    "showed the county sketched in faint lines three thousand miles of road a hundred and "
    "fifty farms tea steam rising from clay cups the wind against the stone her ribs "
    "ached with every breath but she kept her voice level and waited for him to speak"
).split()
FIRST_WORD = "Kestrelwing"  # only in the first message: a Find jump to the start


def sentence(rng: random.Random) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(8, 22))]
    return " ".join(words).capitalize() + rng.choice([".", ".", ".", "?", "!"])


def prose(rng: random.Random, low: int, high: int) -> str:
    target = rng.randint(low, high)
    paragraphs, count = [], 0
    while count < target:
        para = " ".join(sentence(rng) for _ in range(rng.randint(2, 5)))
        paragraphs.append(para)
        count += len(para.split())
    return "\n\n".join(paragraphs)


def make_bundle(exchanges: int, seed: int = 7) -> StoryBundle:
    rng = random.Random(seed)
    held = Character(id="held", name="John Carver", is_player_available=True)
    other = Character(id="rosa", name="Rosa Delgado")
    story = Story(title=f"Bench · {exchanges * 2} messages")
    story.held_character_id = held.id
    nodes: list[Node] = []
    asides: list[Aside] = []
    previous: Node | None = None
    for index in range(exchanges):
        user = Node(
            kind="user",
            speaker_id=held.id,
            content=prose(rng, 10, 80),
            ooc="Keep it tense." if rng.random() < 0.08 else None,
            meta=NodeMeta(controlled_character_id=held.id),
        )
        if index == 0:
            user.content = f"{FIRST_WORD} {user.content}"
        meta = NodeMeta(
            model="anthropic/claude-sonnet-4.6",
            usage=Usage(prompt_tokens=40_000, completion_tokens=300, cache_read_tokens=38_000),
        )
        if rng.random() < 0.1:
            meta.roll = Roll(die=100, value=rng.randint(1, 100), band="success", target=60)
        if rng.random() < 0.15:
            meta.reasoning = prose(rng, 30, 120)
        reply = Node(
            kind="assistant", speaker_id="__narrator__", content=prose(rng, 40, 400), meta=meta
        )
        if previous is not None:
            link_child(previous, user)
        link_child(user, reply)
        nodes += [user, reply]
        previous = reply
        if rng.random() < 0.03:
            asides.append(
                Aside(anchor_node_id=reply.id, question=sentence(rng), answer=prose(rng, 30, 90))
            )
    story.active_leaf_id = nodes[-1].id
    return StoryBundle(story=story, cast=[held, other], nodes=nodes, asides=asides)


# --- counting, only here: nothing in src/ is instrumented --------------------

HFW_CALLS = 0
CONSTRUCT = 0.0
BODY_ASKS = 0
BODY_MEASURED = 0


class CountingLabel(QLabel):
    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        global HFW_CALLS
        HFW_CALLS += 1
        return super().heightForWidth(width)


def instrument() -> None:
    """Count label measurements and time spent constructing message widgets."""
    transcript.QLabel = CountingLabel  # the module's own name lookup
    original = transcript.TranscriptView._add_node

    def timed(self, *args, **kwargs):
        global CONSTRUCT
        start = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            CONSTRUCT += time.perf_counter() - start

    transcript.TranscriptView._add_node = timed

    body = getattr(transcript, "TextBody", None)
    if body is not None:  # the height-caching label, once it exists
        asked = body.heightForWidth

        def counted(self, width):
            global BODY_ASKS, BODY_MEASURED
            BODY_ASKS += 1
            BODY_MEASURED += width not in self._heights
            return asked(self, width)

        body.heightForWidth = counted


def undo_speedups(app: QApplication) -> None:
    import sealedlore.gui.fields as fields
    from sealedlore.engine.session import StorySession
    from sealedlore.tree import takes_of

    app.installEventFilter(fields.WheelGuard())  # beside the per-control one
    transcript.TextBody.heightForWidth = QLabel.heightForWidth

    def walked(self):
        def position(node_id):
            active = {node.id for node in self.full_path()}
            ids = [take.id for take in takes_of(self.nodes, node_id, active)]
            return ids.index(node_id) + 1, len(ids)

        return position

    StorySession.take_positions = walked


# --- the measurements -------------------------------------------------------


def settle(app: QApplication, rounds: int = 3) -> None:
    for _ in range(rounds):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def fresh(window: MainWindow, app: QApplication, story_id: str) -> None:
    window._close_story()
    settle(app)
    window.open_story(story_id)
    settle(app)


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def build_all(window: MainWindow, app: QApplication, first_id: str) -> None:
    """Show the first message: every message back to it before the window,
    one page around it since."""
    window.transcript.message_widget(first_id)
    window.transcript._layout.activate()
    settle(app)


def scroll_to_start(window: MainWindow, app: QApplication) -> list[float]:
    """Page up to the start as scrolling to the top does; each page's time."""
    pages = []
    view = window.transcript
    bar = view.verticalScrollBar()
    while view.unbuilt:
        # The reader reaching the top is what builds the page above.
        pages.append(timed(lambda: (bar.setValue(0), settle(app))))
        if len(pages) > 100:
            raise AssertionError("paging up never reached the start")
    return pages


def main() -> int:
    app = build_app(["bench"])
    instrument()
    if OPTS.old:
        undo_speedups(app)
    if OPTS.no_wheel_guard:
        import sealedlore.gui.fields as fields
        import sealedlore.gui.main_window as main_window

        main_window.install_wheel_guard = lambda _app: None
        fields.install_wheel_guard = lambda _app: None
    root = Path(tempfile.mkdtemp(prefix="sealedlore-bench-"))
    sizes = sorted({40 // 2, 200 // 2, OPTS.exchanges})
    bundles = {n: make_bundle(n) for n in sizes}
    for bundle in bundles.values():
        save_story_bundle(bundle, root=root)
    main_bundle = bundles[OPTS.exchanges]
    story_id, first_id = main_bundle.story.id, main_bundle.nodes[0].id
    words = sum(len(n.content.split()) for n in main_bundle.nodes)

    window = MainWindow(root=root, use_mock=True)
    window.resize(1366, 768)
    window.show()
    settle(app)

    rows: list[tuple[str, str]] = []

    def median(fn, setup=None) -> float:
        samples = []
        for _ in range(OPTS.repeats):
            if setup:
                setup()
            samples.append(fn())
        return statistics.median(samples)

    opened = median(
        lambda: timed(lambda: (window.open_story(story_id), settle(app))),
        setup=lambda: (window._close_story(), settle(app)),
    )
    rows.append(("open (newest page)", f"{opened:.2f}s"))

    global HFW_CALLS, CONSTRUCT, BODY_ASKS, BODY_MEASURED
    samples, hfw, construct, asks, measured = [], [], [], [], []
    for _ in range(OPTS.repeats):
        fresh(window, app, story_id)
        HFW_CALLS, CONSTRUCT, BODY_ASKS, BODY_MEASURED = 0, 0.0, 0, 0
        samples.append(timed(lambda: build_all(window, app, first_id)))
        hfw.append(HFW_CALLS)
        construct.append(CONSTRUCT)
        asks.append(BODY_ASKS)
        measured.append(BODY_MEASURED)
    built = statistics.median(samples)
    rows.append(("show the first message (a jump)", f"{built:.2f}s"))
    rows.append(("  of which constructing widgets", f"{statistics.median(construct):.2f}s"))
    rows.append(("  plain label heightForWidth calls", f"{statistics.median(hfw):,.0f}"))
    if getattr(transcript, "TextBody", None) is not None:
        rows.append(
            (
                "  text body heights asked / measured",
                f"{statistics.median(asks):,.0f} / {statistics.median(measured):,.0f}",
            )
        )

    def find_first() -> float:
        fresh(window, app, story_id)
        window.find_bar.field.setText(FIRST_WORD)
        window.find_bar._timer.stop()
        return timed(lambda: (window._on_find_changed(FIRST_WORD), settle(app)))

    rows.append(("Find jump to the first message", f"{median(find_first):.2f}s"))

    fresh(window, app, story_id)
    pages = scroll_to_start(window, app)
    rows.append(
        (
            "scroll up to the start, page by page",
            f"{sum(pages):.2f}s ({len(pages)} pages, slowest {max(pages, default=0):.2f}s)",
        )
    )
    built_now = sum(
        1 for w in window.transcript.findChildren(transcript.MessageWidget) if w.isVisible()
    )
    resize = median(lambda: timed(lambda: (window.resize(window.width() + 1, 768), settle(app))))
    rows.append((f"relayout, {built_now} built (resize 1px)", f"{resize:.2f}s"))
    if not OPTS.skip_text_size:
        scale = timed(lambda: (window.set_text_scale(1.25), settle(app)))
        window.set_text_scale(1.0)
        settle(app)
        rows.append((f"text size 100% → 125%, {built_now} built", f"{scale:.2f}s"))

    for n in sizes:
        bundle = bundles[n]
        fresh(window, app, bundle.story.id)
        took = timed(lambda b=bundle: build_all(window, app, b.nodes[0].id))
        rows.append((f"show the first message, {n * 2} messages", f"{took:.2f}s"))

    profile_text = ""
    if OPTS.profile:
        fresh(window, app, story_id)
        profiler = cProfile.Profile()
        profiler.enable()
        build_all(window, app, first_id)
        profiler.disable()
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).sort_stats("tottime").print_stats(18)
        profile_text = out.getvalue()

    window.close()
    platform = "display" if OPTS.display else "offscreen"
    header = (
        f"## {OPTS.label} ({platform}, {OPTS.exchanges * 2} messages, {words:,} words, "
        f"median of {OPTS.repeats})"
    )
    table = "\n".join(["| measure | time |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows])
    print(header)
    print(table)
    if profile_text:
        print(profile_text)
    with open(HERE / "results.md", "a") as results:
        results.write(f"{header}\n\n{table}\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
