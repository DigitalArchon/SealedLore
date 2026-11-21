"""Stop while a lorebook's entries are embedded: it reaches the embeddings,
what was done is kept, and the dialog stays until the job has ended. Before,
Stop reached only the story model, the dialog vanished, the embedding ran on,
and quitting meanwhile waited for all of it behind a closed window (the
author, Nov 21 2026)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog, QPushButton

from sealedlore.engine import session_recall
from sealedlore.engine.session_recall import EmbeddingStopped
from sealedlore.gui.main_window import MainWindow
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story import Story
from sealedlore.providers.embeddings import EmbeddingResult
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle

ENTRIES = 40


class SlowEmbeddings:
    """An embeddings backend that takes its time over each request."""

    def __init__(self, seconds: float = 0.15) -> None:
        self.seconds = seconds
        self.requests = 0
        self._lock = threading.Lock()

    def embed(self, texts, *, as_query: bool = False) -> EmbeddingResult:
        with self._lock:
            self.requests += 1
        time.sleep(self.seconds)
        vectors = tuple((1.0, float(len(text))) for text in texts)
        return EmbeddingResult(vectors=vectors, model="slow", dimensions=2)

    def close(self) -> None:
        pass


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def one_text_a_request(monkeypatch):
    monkeypatch.setattr(session_recall, "EMBED_BATCH_TOKENS", 1)


def lore() -> list[LoreEntry]:
    return [
        LoreEntry(title=f"Farm {n}", content=f"Farm number {n} lies north of Hellsville.")
        for n in range(ENTRIES)
    ]


def test_stopped_embedding_keeps_what_was_done(tmp_path: Path, one_text_a_request):
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider

    story = Story(title="Hellsville")
    save_story_bundle(StoryBundle(story=story, lore=lore()), root=tmp_path)
    session = StorySession(
        load_story_bundle(story.id, root=tmp_path),
        Config(),
        MockChatProvider([]),
        root=tmp_path,
        learn_corrections=False,
    )
    backend = SlowEmbeddings(seconds=0.05)
    session.embeddings = backend
    with pytest.raises(EmbeddingStopped):
        session.ensure_lore_vectors(stopped=lambda: backend.requests >= 3)
    assert 0 < backend.requests < ENTRIES
    kept = load_story_bundle(story.id, root=tmp_path).embeddings
    assert 0 < len(kept) <= backend.requests
    # The rest are embedded later, and only the rest.
    before = backend.requests
    session.ensure_lore_vectors()
    assert backend.requests - before == ENTRIES - len(kept)


def test_stop_reaches_the_embedding_and_the_dialog_waits_for_it(
    app, tmp_path: Path, one_text_a_request
):
    story = Story(title="Hellsville")
    save_story_bundle(StoryBundle(story=story, lore=lore()), root=tmp_path / "data")
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    window.open_story(story.id)
    session = window.session
    backend = SlowEmbeddings()
    session.embeddings = backend
    session.lore_layout = lambda: SimpleNamespace(mode="select")
    seen: dict[str, object] = {}

    def press_stop() -> None:
        progress = next(w for w in app.topLevelWidgets() if isinstance(w, QProgressDialog))
        button = next(b for b in progress.findChildren(QPushButton) if b.text() == "Stop")
        button.click()
        seen["visible"] = progress.isVisible()
        seen["label"] = progress.labelText()

    QTimer.singleShot(300, window, press_stop)
    started = time.monotonic()
    window.embed_lore_now()
    elapsed = time.monotonic() - started
    assert seen["visible"] is True
    assert "Stopping…" in str(seen["label"])
    assert backend.requests < ENTRIES
    # Forty requests four at a time would be 1.5 s and more.
    assert elapsed < 1.2
    window.close()
