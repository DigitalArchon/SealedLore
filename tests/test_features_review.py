"""The review of two features before 3.5.0b1 (Nov 23 2026): one character's
answer asked for again (engine/session_reactions.py `regenerate_reaction`),
and lore from the full entry (engine/lore_whole_back.py, its vectors made
ahead in engine/session_lore_vectors.py). Each test here failed on the code as
it was. The mock provider and mock embeddings: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine.model_tuning import sends_lore_from_full
from sealedlore.engine.session_lore_vectors import LORE_BEING_MADE

# --- which models are Claude ------------------------------------------------------

# Every spelling a Claude model is served under: Anthropic's own and NanoGPT's
# bare ids, OpenRouter's maker prefix, Bedrock's region and "anthropic." before
# the id, a router before that.
CLAUDE_SPELLINGS = [
    "claude-sonnet-4-6",
    "Anthropic::claude-opus-5-5",
    "anthropic/claude-sonnet-4.6:thinking",
    "openrouter/anthropic/claude-sonnet-4.6",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "Bedrock::us.anthropic.claude-opus-4-1-20250805-v1:0",
]
# Named for Claude, and not it: a model of someone else's, or one made from
# another model, on a server of one's own.
NOT_CLAUDE = [
    "Claude-Opus-Distill-Qwen3",
    "Local::Claude-Opus-Distill-Qwen3",
    "LM Studio::someone/Claude-Opus-Distill-Qwen3-GGUF",
    "someone/claude-sonnet-4-6",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "bedrock/meta.claude-like-70b",
    "claude-3.7-sonnet-reasoning-gemma3-12b",
]


@pytest.mark.parametrize("model", CLAUDE_SPELLINGS)
def test_claude_is_known_by_every_spelling_it_is_served_under(model: str) -> None:
    assert not sends_lore_from_full(model)


@pytest.mark.parametrize("model", NOT_CLAUDE)
def test_a_model_only_named_for_claude_is_not_claude(model: str) -> None:
    assert sends_lore_from_full(model)


# --- the character the author plays now ----------------------------------------------


def as_maela(text: str = "I look at the lock."):
    from sealedlore.engine.prompt import TurnRequest

    return TurnRequest(
        speaker_id="char-maela", user_text=text, controlled_character_id="char-maela"
    )


def storyteller_requests(session):
    return [r for r in session.provider.requests if r.model == session.story.defaults.main_model]


def test_the_answer_of_the_character_the_author_now_plays_isnt_asked_for_again(
    tmp_path,
) -> None:
    from tests.test_reactions import make_session, play, turn

    session = make_session(tmp_path, ["The door gives.", "MAELA-FIRST"])
    play(session, turn())
    passage = session.path()[-1]
    assert session.reaction_refusal(passage.id, "char-maela") is None
    session.hold("char-maela")  # the author takes Maela up
    refusal = session.reaction_refusal(passage.id, "char-maela")
    assert refusal is not None and "yours to play now" in refusal
    before = len(session.provider.requests)
    with pytest.raises(ValueError, match="yours to play now"):
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert len(session.provider.requests) == before


def test_a_blind_answer_by_whom_the_author_has_taken_up_isnt_sent_as_theirs(tmp_path) -> None:
    """Together: Maela answered the passage blind; the author then plays her,
    and their turn is what she does. Her model's answer stays under the
    passage, and isn't sent to the storyteller as what she means to do."""
    from tests.test_reactions import make_session, play, sent, turn

    session = make_session(tmp_path, ["The door gives.", "MAELA-PICKS-THE-LOCK", "They go on."])
    play(session, turn())
    passage = session.path()[-1]
    assert [a.text for a in passage.meta.reactions] == ["MAELA-PICKS-THE-LOCK"]
    session.hold("char-maela")
    play(session, as_maela())
    request = storyteller_requests(session)[-1]
    assert "I look at the lock." in sent(request)
    assert "MAELA-PICKS-THE-LOCK" not in sent(request)
    assert passage.meta.reactions[0].text == "MAELA-PICKS-THE-LOCK"  # kept, unsent


def test_after_the_turn_the_one_who_took_it_never_answers_it(tmp_path) -> None:
    from tests.test_reactions import MAELA_MODEL, a_story, make_session, play

    story = a_story()
    story.defaults.round_order = "after"
    session = make_session(tmp_path, ["The door gives."], story=story)
    session.hold("char-maela")
    play(session, as_maela())
    turn_node = session.path()[-2]
    assert turn_node.meta.round_after and turn_node.meta.reactions == []
    assert [r.model for r in session.provider.requests if r.model == MAELA_MODEL] == []


def test_the_others_answers_still_go_with_the_turn(tmp_path) -> None:
    """Only the answer of the character the turn is played as is left out."""
    from tests.test_answer_regenerate import two_answer
    from tests.test_reactions import play, sent, turn

    session, provider = two_answer(
        tmp_path, ["MAELA-WAITS"], ["IDRIS-DRAWS"], replies=["The door gives.", "On."]
    )
    play(session, turn())
    session.hold("char-maela")
    play(session, as_maela())
    request = storyteller_requests(session)[-1]
    text = sent(request)
    assert "IDRIS-DRAWS" in text and "MAELA-WAITS" not in text


# --- an older answer asked for again, with what she remembered then ----------------


def test_an_older_answer_is_asked_for_with_only_the_memories_the_round_then_had(
    tmp_path, monkeypatch
) -> None:
    """Memories written at later rounds cover stretches the round at that
    passage still sent word for word: asked again, the request is the round's
    there, field for field, not one that remembers what it then saw."""
    from sealedlore.engine import reactions as reactions_module
    from tests.test_answer_regenerate import asked, two_answer
    from tests.test_reactions import MAELA_MODEL, play, turn

    monkeypatch.setattr(reactions_module, "HISTORY_TOKENS", 60)
    monkeypatch.setattr(reactions_module, "RECOLLECT_MESSAGES", 2)
    replies = [f"PASSAGE-{i} " + "word " * 20 for i in range(6)]
    answers = [f"ANSWER-{i}" for i in range(6)] + ["ANSWER-AGAIN"]
    session, provider = two_answer(tmp_path, answers, replies=replies)
    passages = []
    for i in range(6):
        provider.responses = [replies[i], f"I REMEMBER {i}."]  # the passage, then a memory
        play(session, turn(f"Turn {i}."))
        passages.append(session.path()[-1])
    assert len(session.bundle.memories) >= 3  # later rounds wrote more of them
    rounds = asked(provider, MAELA_MODEL)
    for target in (2, 3):
        before = len(provider.requests)
        list(session.regenerate_reaction(passages[target].id, "char-maela"))
        [again] = provider.requests[before:]  # hers alone: nothing to remember first
        assert again == rounds[target]
    # The latest is asked as the round asked it, every memory with it.
    before = len(provider.requests)
    list(session.regenerate_reaction(passages[-1].id, "char-maela"))
    [again] = provider.requests[before:]
    assert again == rounds[-1]


# --- asked again after the passage was edited ------------------------------------------


def replies(session, *texts: str) -> None:
    """The mock's next replies, in order, from the first (it takes them by
    its count of calls)."""
    session.provider.responses = list(texts)
    session.provider._call_count = 0


def test_asked_again_after_an_edit_she_is_given_only_what_she_was_there_for(tmp_path) -> None:
    """Edited since the round, a passage that moves counts whole until its
    views are read again: the round reads them first, and so does asking one
    of them again, as a paid call said in the status bar."""
    from sealedlore.engine.reactions import apply_views
    from sealedlore.engine.session import SessionNotice
    from tests.test_passage_views import ASKS, BOTH, MOVING, TOWER
    from tests.test_reactions import MAELA_MODEL, make_session, play, sent, turn

    session = make_session(tmp_path, ["The door gives.", "Hm."])
    session.story.setup.starting_scene = BOTH
    play(session, turn())
    node = session.path()[-1]
    node.content = MOVING
    node.meta.scene = TOWER
    assert apply_views('{"Maela Orr": [1]}', node, [session.cast[1]])
    # Read as the round left it: nothing more is asked.
    replies(session, "AGAIN-1")
    before = len(session.provider.requests)
    list(session.regenerate_reaction(node.id, "char-maela"))
    [first] = session.provider.requests[before:]
    assert first.model == MAELA_MODEL and "TOWER-SECRET" not in sent(first)

    session.edit_node(node.id, MOVING.replace("argue over", "argue about"))
    replies(session, '{"Maela Orr": [1]}', "AGAIN-2")
    before = len(session.provider.requests)
    events = list(session.regenerate_reaction(node.id, "char-maela"))
    views, again = session.provider.requests[before:]
    assert ASKS in sent(views) and again.model == MAELA_MODEL
    assert "TOWER-SECRET" not in sent(again) and "argue about the map" in sent(again)
    assert any(
        isinstance(e, SessionNotice) and e.text.startswith("Working out who saw what")
        for e in events
    )
    assert [a.text for a in node.meta.reactions] == ["AGAIN-2"]


def test_asked_again_after_an_edit_the_record_reads_the_passage_again(tmp_path) -> None:
    from sealedlore.engine.session import SessionNotice
    from tests.test_public_record import character_calls, record_calls
    from tests.test_reactions import make_session, play, sent, turn

    session = make_session(
        tmp_path,
        [
            "The door gives. Across the town the bells ring noon.",
            '{"facts": ["The town bells rang at noon."]}',
            "MAELA-ONE",
        ],
        record=True,
    )
    play(session, turn())
    passage = session.path()[-1]
    assert len(record_calls(session)) == 1
    # Asked again with nothing changed: the record as the round left it.
    replies(session, "MAELA-TWO")
    list(session.regenerate_reaction(passage.id, "char-maela"))
    assert len(record_calls(session)) == 1

    session.edit_node(passage.id, "The door gives. Across the town the bells ring midnight.")
    replies(session, '{"facts": ["The town bells rang at midnight."]}', "MAELA-THREE")
    events = list(session.regenerate_reaction(passage.id, "char-maela"))
    keeper = record_calls(session)[-1]
    assert len(record_calls(session)) == 2 and "ring midnight" in sent(keeper)
    assert "The town bells rang at midnight." in sent(character_calls(session)[-1])
    assert passage.meta.public_record == ["The town bells rang at midnight."]
    assert any(
        isinstance(e, SessionNotice) and e.text.startswith("Keeping the public record")
        for e in events
    )


# --- the vectors lore from the full entry is ranked by --------------------------------

LIMIT_WORDS = 600  # stands in for bge-m3's 8k context on NanoGPT (HTTP 400 past it)


class ContextLimited:
    """Mock embeddings that refuse, as NanoGPT does, a request holding a text
    past its context (HTTP 400), on a background job's thread as on the
    turn's. `documents` is every text asked for as lore, in order."""

    def __init__(self) -> None:
        import threading

        from sealedlore.providers.mock import MockEmbeddings

        self._mock = MockEmbeddings()
        self.model = self._mock.model
        self.dimensions = self._mock.dimensions
        self.documents: list[str] = []
        self.requests = 0
        self.lock = threading.Lock()

    def detached(self):
        return self

    def embed(self, texts, *, as_query: bool = False):
        from sealedlore.providers.base import ProviderError

        with self.lock:
            if not as_query:
                self.documents += list(texts)
                self.requests += 1
        if any(len(text.split()) > LIMIT_WORDS for text in texts):
            raise ProviderError(
                "https://nano-gpt.com/api/v1/embeddings returned HTTP 400", status_code=400
            )
        return self._mock.embed(texts, as_query=as_query)


def big_book_session(tmp_path, story, cast, *, extra=()):
    """Three always-on entries six times over, the Guild (the least
    important) sent condensed, under SillyTavern's rules, where nothing but
    the job embeds the lore; its whole text is past the endpoint's context."""
    from sealedlore.models.config import EmbeddingProviderConfig
    from sealedlore.models.lore import lore_fingerprint
    from sealedlore.storage.tavern_lore import lore_from_book
    from tests.test_lore_whole_back import GUILD, HARBOUR, KEEP, session_with

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
    session = session_with(tmp_path, story, cast, [*entries, *extra])
    session.story.defaults.context_token_budget = 6_000
    session.config.embedding_provider = EmbeddingProviderConfig(model="mock-embed")
    backend = ContextLimited()
    session.embeddings = backend
    guild = next(e for e in session.bundle.lore if e.title == "The Guild")
    assert session.lore_layout().condensed == (guild.id,)
    assert len(guild.content.split()) > LIMIT_WORDS
    return session, backend, guild


def after_passages(session, count: int, said: list[str] | None = None) -> list[str]:
    """`count` passages, the job each sets going let finish, and the last one
    taken in (as the window's poll would); the Lore page's note on each turn.
    `said`: what taking them in said, each time."""
    from tests.test_lore_whole_back import HESK_TURN, turn

    notes = []
    for _ in range(count):
        for event in session.send(turn(HESK_TURN)):
            text = getattr(event, "text", None)
            if said is not None and isinstance(text, str) and "vectors" in text:
                said.append(text)
        notes.append(session.last_retrieval.in_full_note)
        job = session._vectors_job
        if job is not None:
            assert job.done.wait(10)
    if said is not None:
        said += session.adopt_lore_vectors()
    else:
        session.adopt_lore_vectors()
    return notes


def test_the_whole_text_of_an_entry_sent_condensed_is_never_embedded(tmp_path, story, cast) -> None:
    """Nothing ranks it (its parts are ranked instead), so a whole text past
    the endpoint's context no longer stops every part's vector being made."""
    session, backend, guild = big_book_session(tmp_path, story, cast)
    notes = after_passages(session, 2)
    assert guild.content not in backend.documents
    parts = {item.id for item in session.part_items()}
    assert parts and parts <= set(session.ensure_lore_vectors(embed=False))
    assert notes[-1] == "" and session.last_retrieval.in_full[0].score is not None
    # Nor is it among what the embed-now dialog counts.
    assert guild.id not in {item.id for item in session.lore_without_vectors()}


def test_a_text_the_endpoint_refuses_is_skipped_and_never_sent_again(tmp_path, story, cast) -> None:
    """One entry the turn's ranking reads is past the endpoint's context: the
    job leaves it out and makes the rest, the turns rank the rest by meaning,
    the Lore page says which couldn't be made, and it isn't sent again after
    every passage, only once its text changes."""
    from sealedlore.models.lore import LoreEntry

    long = LoreEntry(title="The Tide Tables", content="tide " * 700, keywords=["tide"])
    session, backend, _guild = big_book_session(tmp_path, story, cast, extra=[long])
    said: list[str] = []
    after_passages(session, 1, said)
    tried = sum("tide tide" in text for text in backend.documents)
    assert tried and len(said) == 1 and "“The Tide Tables”" in said[0]
    notes = after_passages(session, 3, said)
    assert sum("tide tide" in text for text in backend.documents) == tried  # never again
    assert len(said) == 1  # said once
    parts = {item.id for item in session.part_items()}
    have = set(session.ensure_lore_vectors(embed=False))
    assert parts <= have and long.id not in have
    report = session.last_retrieval
    assert report.in_full and report.in_full[0].score is not None
    assert "“The Tide Tables”" in notes[-1] and "HTTP 400" in notes[-1]
    assert "by words alone" in notes[-1]
    # Its text changed: tried again.
    long_entry = next(e for e in session.bundle.lore if e.id == long.id)
    long_entry.content = "The tides turn twice a day at the harbour mouth."
    notes = after_passages(session, 2)
    assert long.id in set(session.ensure_lore_vectors(embed=False))
    assert notes == [LORE_BEING_MADE, ""]


def test_a_refusal_in_a_request_of_several_leaves_out_only_the_one_refused(
    tmp_path, story, cast
) -> None:
    from sealedlore.models.lore import LoreEntry

    long = LoreEntry(title="The Tide Tables", content="tide " * 700, keywords=["tide"])
    short = LoreEntry(title="Wolves", content="They hunt in threes.", keywords=["wolf"])
    session, backend, _guild = big_book_session(tmp_path, story, cast, extra=[short, long])
    after_passages(session, 1)
    have = set(session.ensure_lore_vectors(embed=False))
    assert short.id in have and long.id not in have


def test_a_turn_leaves_to_the_job_out_what_it_is_making(tmp_path, story, cast) -> None:
    """Under SealedLore's rules a turn embeds the lore it chooses from; with a
    job out making those vectors (a story just opened), it embedded them as
    well, and both were paid for. Now it leaves them to the job, ranks by
    words meanwhile and says so, and never waits."""
    from sealedlore.engine.session_lore_vectors import LORE_VECTORS_COMING
    from tests.test_lore_vectors_ahead import finished, gated, lore
    from tests.test_lore_whole_back import HESK_TURN, session_with, turn, wolves

    session = session_with(tmp_path, story, cast, [*lore(), wolves()])
    backend = gated(session)
    assert session.start_lore_vectors()  # the window, a moment after the story opened
    list(session.send(turn(HESK_TURN)))
    assert backend.documents_here() == []  # the turn embedded none of the lore
    report = session.last_retrieval
    assert not report.used_embeddings and report.fallback_reason == LORE_VECTORS_COMING
    backend.gate.set()
    finished(session)
    made = {text for texts in backend.held for text in texts}
    list(session.send(turn(HESK_TURN)))  # takes the job's vectors in first
    assert backend.documents_here() == []
    rows = [(row.content_hash, row.model) for row in session.bundle.embeddings]
    assert len(rows) == len(set(rows)) == len(made)
    assert session.last_retrieval.used_embeddings


def test_a_turn_takes_in_what_the_job_has_made_so_far(tmp_path, story, cast) -> None:
    """A job out that has made the entries' vectors already: the turn ranks
    by them, without waiting for the rest or making them itself."""
    import threading

    from tests.test_lore_vectors_ahead import GatedEmbeddings, gated, lore
    from tests.test_lore_whole_back import HESK_TURN, session_with, turn, wolves

    class SecondHeld(GatedEmbeddings):
        """Lets a job's first request through, holds the second."""

        def __init__(self) -> None:
            super().__init__()
            self.first = threading.Event()

        def embed(self, texts, *, as_query: bool = False):
            from sealedlore.providers.embeddings import EmbeddingResult

            if threading.get_ident() != self._home and not self.first.is_set():
                self.held.append(tuple(texts))
                self.first.set()
                vectors = tuple(self._vector(text) for text in texts)
                return EmbeddingResult(vectors, self.model, self.dimensions)
            return super().embed(texts, as_query=as_query)

    session = session_with(tmp_path, story, cast, [*lore(), wolves()])
    backend = SecondHeld()
    gated(session)
    session.embeddings = backend
    # Two requests: the entries' texts, then the parts'.
    monkey_batches = session.embedding_batches

    def two(texts):
        entries = [t for t in texts if t.startswith(("Wolves", "The Keep", "The Harbour"))]
        rest = [t for t in texts if t not in entries]
        return [batch for batch in (entries, rest) if batch] or monkey_batches(texts)

    session.embedding_batches = two
    assert session.start_lore_vectors()
    assert backend.first.wait(10)
    job = session._vectors_job
    for _ in range(200):
        with job.lock:
            if job.vectors:
                break
        threading.Event().wait(0.01)
    list(session.send(turn(HESK_TURN)))
    assert backend.documents_here() == []
    report = session.last_retrieval
    assert report.used_embeddings and report.fallback_reason is None
    backend.gate.set()
    assert job.done.wait(10)
    session.adopt_lore_vectors()
    rows = [(row.content_hash, row.model) for row in session.bundle.embeddings]
    assert len(rows) == len(set(rows))
