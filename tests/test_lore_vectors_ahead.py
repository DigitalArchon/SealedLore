"""The vectors lore from the full entry is ranked by are made ahead of the
turns (engine/session_lore_vectors.py): after a passage, when a story opens
and once its lore changes, in the background on the embeddings backend's
detached copy, taken in by the story's one writer. A turn before they are in
never waits: it ranks the parts by words and the Lore page says so. Never for
a Claude storyteller, in a chat or in a private scene. A gated mock
embeddings backend; no network."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sealedlore.engine.session_lore_vectors import LORE_BEING_MADE, PARTS_BEING_MADE
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import EmbeddingProviderConfig
from sealedlore.models.lore import lore_fingerprint
from sealedlore.providers.base import ProviderError
from sealedlore.providers.embeddings import EmbeddingResult
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.tavern_lore import lore_from_book
from tests.test_lore_whole_back import (
    GLM,
    GUILD,
    HARBOUR,
    HESK_TURN,
    KEEP,
    guild,
    harbour,
    keep,
    session_with,
    turn,
    with_embeddings,
    wolves,
)

WAIT = 10


class GatedEmbeddings(MockEmbeddings):
    """Embeds what a background job asks only once `gate` is open; anything
    asked on the thread that made it (a turn's, here) goes through at once
    and is noted, so a turn that waited would fail rather than hang."""

    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.held: list[tuple[str, ...]] = []
        self.copies = 0
        self._home = threading.get_ident()
        self._failing = error

    def detached(self) -> GatedEmbeddings:
        self.copies += 1
        return self

    def embed(self, texts, *, as_query: bool = False):
        if threading.get_ident() == self._home:
            return super().embed(texts, as_query=as_query)
        # A background job's: kept apart from `calls`, which are the turns'.
        self.held.append(tuple(texts))
        assert self.gate.wait(WAIT), "the gate was never opened"
        if self._failing is not None:
            raise self._failing
        vectors = tuple(self._vector(text) for text in texts)
        return EmbeddingResult(vectors=vectors, model=self.model, dimensions=self.dimensions)

    def documents_here(self) -> list[str]:
        """What was embedded as lore (not a turn's text) on the home thread."""
        return [text for texts, as_query in self.calls if not as_query for text in texts]


def gated(session, **kw) -> GatedEmbeddings:
    session.config.embedding_provider = EmbeddingProviderConfig(model="mock-embed")
    backend = GatedEmbeddings(**kw)
    session.embeddings = backend
    return backend


def lore():
    return [harbour(), keep(), guild()]


def parts_of(session) -> set[str]:
    return {item.id for item in session.part_items()}


def finished(session) -> None:
    job = session._vectors_job
    assert job is not None and job.done.wait(WAIT)


# --- the engine ------------------------------------------------------------------------


def test_lore_without_vectors_counts_the_parts(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore())
    with_embeddings(session)
    missing = {item.id for item in session.lore_without_vectors()}
    assert len(parts_of(session)) == len(HARBOUR) + len(GUILD)
    assert parts_of(session) <= missing
    # The entries, but for the whole texts of those sent condensed, which
    # nothing ranks: their parts are ranked instead.
    condensed = set(session.lore_layout().condensed)
    assert condensed == {entry.id for entry in session.bundle.lore if entry.title != "The Keep"}
    assert {entry.id for entry in session.bundle.lore} - condensed <= missing
    assert not condensed & missing
    # The entries alone once they are made, by a turn's own selection as ever.
    session.ensure_lore_vectors()
    assert {item.id for item in session.lore_without_vectors()} == parts_of(session)
    # Made on purpose (Re-embed lorebook, the dialog): the parts too.
    session.ensure_lore_vectors(embed_parts=True)
    assert session.lore_without_vectors() == []


def test_the_parts_are_made_ahead_and_a_turn_meanwhile_ranks_by_words(
    tmp_path, story, cast
) -> None:
    session = session_with(tmp_path, story, cast, lore())
    backend = gated(session)
    session.ensure_lore_vectors()  # the entries, as a story's first turn makes them
    backend.calls.clear()
    assert session.lore_vectors_wanted() and session.start_lore_vectors()
    assert backend.copies == 1 and session.lore_vectors_out()
    assert not session.lore_vectors_ready()
    # A turn now doesn't wait for them: by words, and said so.
    list(session.send(turn(HESK_TURN)))
    report = session.last_retrieval
    (back,) = report.in_full
    assert back.name == "The Guild" and back.score is None
    assert report.in_full_note == PARTS_BEING_MADE
    assert backend.documents_here() == []  # nothing of the lore embedded in the turn
    # Once made, the next turn takes them in at its start and ranks by them.
    backend.gate.set()
    finished(session)
    held = {text for texts in backend.held for text in texts}
    assert {f"The Guild\n{part}" for part in GUILD} <= held
    list(session.send(turn(HESK_TURN)))
    report = session.last_retrieval
    assert not session.lore_vectors_out()
    assert report.in_full_note == "" and report.in_full[0].score is not None
    assert {i.id for i in session.lore_without_vectors()} == set()


def test_after_a_passage_they_are_set_going_without_holding_it(tmp_path, story, cast) -> None:
    """Under SillyTavern's rules nothing else embeds the lore: the parts held
    the first turn up to a minute when the turn made them."""
    book = {
        "entries": {
            str(i): {
                "uid": i,
                "comment": title,
                "key": [],
                "constant": True,
                "order": order,
                "content": "\n\n".join(paragraphs * 6),
            }
            for i, (title, paragraphs, order) in enumerate(
                (("The Harbour", HARBOUR, 300), ("The Guild", GUILD, 10), ("The Keep", KEEP, 200))
            )
        }
    }
    entries = lore_from_book(book).entries
    for item in entries:
        item.condensed = f"{item.title}, in short."
        item.condensed_from = lore_fingerprint(item.content)
    story.defaults.lore_firing = "sillytavern"
    session = session_with(tmp_path, story, cast, entries)
    session.story.defaults.context_token_budget = 6_000
    backend = gated(session)
    list(session.send(turn(HESK_TURN)))
    report = session.last_retrieval
    assert report.in_full and report.in_full_note == PARTS_BEING_MADE
    # Nothing embedded in the turn, not even its own text: the lore went out
    # after the passage, on the background copy, and send() didn't wait.
    assert backend.calls == []
    assert session.lore_vectors_out() and backend.copies == 1
    backend.gate.set()
    finished(session)
    assert session.adopt_lore_vectors() == []
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full[0].score is not None


def test_an_entrys_own_vector_missing_says_so_too(tmp_path, story, cast) -> None:
    """Every item ranked with the parts needs its vector, or the parts would
    be ranked against entries scored by words alone."""
    session = session_with(tmp_path, story, cast, [*lore(), wolves()])
    with_embeddings(session)
    session.ensure_lore_vectors(embed_parts=True)
    report = session.retrieve(turn(HESK_TURN), session.path(), allow_network=False)
    assert report.in_full_note == ""
    session.bundle.lore[3].content = "They hunt in threes, and in winter in packs of nine."
    report = session.retrieve(turn(HESK_TURN), session.path(), allow_network=False)
    assert report.in_full_note == LORE_BEING_MADE
    assert report.in_full and report.in_full[0].score is None


def test_none_for_claude_a_chat_or_a_private_scene(tmp_path, story, cast) -> None:
    claude = session_with(
        tmp_path / "c",
        story.model_copy(deep=True),
        cast,
        lore(),
        model="Anthropic::claude-opus-5-5",
    )
    backend = gated(claude)
    assert not claude.lore_vectors_wanted() and not claude.start_lore_vectors()
    assert claude.part_items() == []
    assert {i.id for i in claude.lore_without_vectors()} == {e.id for e in claude.bundle.lore}
    list(claude.send(turn(HESK_TURN)))
    assert not claude.lore_vectors_out()
    assert all("#" not in text for text in backend.documents_here())

    chat = session_with(tmp_path / "chat", story.model_copy(deep=True), cast, lore())
    chat.story.mode = "chat"
    gated(chat)
    assert not chat.lore_vectors_wanted() and not chat.start_lore_vectors()
    assert chat.part_items() == []

    scene = session_with(tmp_path / "s", story.model_copy(deep=True), cast, lore())
    backend = gated(scene)
    assert scene.start_lore_vectors()
    job = scene._vectors_job
    # Entering a private scene stops a job out, once its request is back.
    scene.enter_private(keep="memory", provider=MockChatProvider(["In."] * 3), model="local/m")
    assert job.stop.is_set()
    assert not scene.lore_vectors_wanted() and not scene.start_lore_vectors()
    backend.gate.set()
    finished(scene)
    list(scene.send(turn(HESK_TURN)))
    assert backend.documents_here() == []
    assert scene.last_retrieval.in_full_note == ""  # words, as a scene's always are


def test_a_failure_is_a_notice_and_the_turn_goes_on(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore())
    backend = gated(session, error=ProviderError("HTTP 503"))
    session.ensure_lore_vectors()
    assert session.start_lore_vectors()
    backend.gate.set()
    finished(session)
    notices = []
    for event in session.send(turn(HESK_TURN)):
        text = getattr(event, "text", None)
        if isinstance(text, str) and "couldn't be made" in text:
            notices.append(text)
    assert notices and "HTTP 503" in notices[0]
    report = session.last_retrieval
    assert report.in_full and "couldn't be made (HTTP 503)" in report.in_full_note
    assert session.last_result is not None and session.last_result.text


def test_closing_the_story_stops_the_job_and_keeps_what_was_made(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore())
    backend = gated(session)
    session.ensure_lore_vectors()
    assert session.start_lore_vectors()
    job = session._vectors_job
    session.close(wait_seconds=0)
    assert job.stop.is_set() and not session.lore_vectors_out()
    # Its thread ends once its request is back, and nothing more is sent.
    backend.gate.set()
    assert job.done.wait(WAIT)
    assert len(backend.held) == 1

    again = session_with(tmp_path / "2", story.model_copy(deep=True), cast, lore())
    backend = gated(again)
    again.ensure_lore_vectors()
    assert again.start_lore_vectors()
    backend.gate.set()
    finished(again)
    again.close()
    assert again.lore_without_vectors() == []  # made, and taken in as it closed


def test_reembedding_the_lorebook_makes_the_parts_too(tmp_path, story, cast) -> None:
    session = session_with(tmp_path, story, cast, lore())
    with_embeddings(session)
    vectors = session.reembed_lore()
    assert parts_of(session) <= set(vectors) and session.lore_without_vectors() == []


# --- the window --------------------------------------------------------------------------


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def window_on(tmp_path: Path, story, cast, entries, *, model: str = GLM):
    from sealedlore.gui.main_window import MainWindow

    story.held_character_id = "char-serrik"
    story.defaults.main_model = model
    save_story_bundle(StoryBundle(story=story, cast=list(cast), lore=entries), root=tmp_path / "d")
    window = MainWindow(root=tmp_path / "d", use_mock=True)
    # Counted as `session_with` counts, whatever tiktoken has cached: with its
    # vocabulary the Harbour fitted whole and the dialog's count was another.
    window.estimator = TokenEstimator(counter=fallback_counter)
    window.config.lore_whole_share = 0.0
    window.config.lore_limit = "tokens"
    window.config.lore_token_cap = 200
    window.open_story(story.id)
    return window


def test_opening_a_story_has_them_made_and_the_window_takes_them_in(
    app, tmp_path, story, cast
) -> None:
    window = window_on(tmp_path, story, cast, lore())
    session = window.session
    assert window._lore_vectors_soon.isActive()  # a moment after it opened
    backend = gated(session)
    window._lore_vectors_soon.stop()
    window._lore_vectors_if_needed()
    # The window is free meanwhile: the job is out on its thread.
    assert session.lore_vectors_out() and window._lore_vectors_timer.isActive()
    window._poll_lore_vectors()
    assert session.lore_vectors_out()  # not made yet: nothing taken in
    backend.gate.set()
    finished(session)
    window._poll_lore_vectors()
    assert not session.lore_vectors_out() and not window._lore_vectors_timer.isActive()
    assert session.lore_without_vectors() == []
    kept = load_story_bundle(story.id, root=tmp_path / "d")
    assert len(kept.embeddings) == len(session.bundle.embeddings) > 0
    window.close()


def test_a_claude_story_has_none_made(app, tmp_path, story, cast) -> None:
    window = window_on(tmp_path, story, cast, lore(), model="Anthropic::claude-sonnet-4-6")
    gated(window.session)
    window._lore_vectors_if_needed()
    assert not window.session.lore_vectors_out()
    window.close()


def test_condensing_an_entry_has_its_parts_made_a_moment_after(
    app, tmp_path, story, cast, monkeypatch
) -> None:
    entry = guild()
    entry.content = "\n\n".join(GUILD * 2)  # long enough to be worth condensing
    entry.condensed = entry.condensed_from = None
    window = window_on(tmp_path, story, cast, [harbour(), keep(), entry])
    window._lore_vectors_soon.stop()
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider(["Hesk's guild."]))
    session = window.session
    backend = gated(session)
    window.condense_lore_entry(entry.id)
    assert session.bundle.lore[2].condensed_now() == "Hesk's guild."
    assert window._lore_vectors_soon.isActive()
    window._lore_vectors_soon.stop()
    window._lore_vectors_if_needed()  # as the timer would
    assert session.lore_vectors_out() and backend.calls == []
    backend.gate.set()
    finished(session)
    window._poll_lore_vectors()
    assert parts_of(session) and session.lore_without_vectors() == []
    held = {text for texts in backend.held for text in texts}
    assert f"The Guild\n{GUILD[1]}" in held
    window.close()


def test_the_lore_page_says_when_they_were_ranked_by_words(app) -> None:
    from sealedlore.engine.retrieval import RetrievalReport
    from sealedlore.gui.lore_panel import LorePanel

    panel = LorePanel()
    panel.show_retrieval(RetrievalReport(in_full_note=PARTS_BEING_MADE))
    rows = [panel.injected.item(i).text() for i in range(panel.injected.count())]
    assert rows == [
        "Lore from the full entries — ranked by words: the parts' vectors are being made"
    ]


def test_the_embed_now_dialog_embeds_the_parts_with_the_entries(
    app, tmp_path, story, cast, monkeypatch
) -> None:
    from sealedlore.gui import window_lorebooks

    window = window_on(tmp_path, story, cast, lore())
    session = window.session
    window.config.embedding_provider = EmbeddingProviderConfig(model="mock-embed")
    session.embeddings = MockEmbeddings()
    said: list[str] = []

    def run(parent, job_session, words, job, result, **kw):
        said.append(words)
        for _ in job():
            pass

    monkeypatch.setattr(window_lorebooks, "run_with_progress", run)
    parts = len(session.part_items())
    assert parts
    window.embed_lore_now(least=1)
    # The Keep alone: the Harbour and the Guild go condensed, and their parts
    # are embedded in place of their whole texts.
    assert set(session.lore_layout().condensed) == {
        entry.id for entry in session.bundle.lore if entry.title != "The Keep"
    }
    assert said == [
        f"Embedding 1 lore entry and {parts} parts of the always-on entries sent "
        "condensed, so each turn can find what it needs…"
    ]
    assert session.lore_without_vectors() == []
    window.close()
