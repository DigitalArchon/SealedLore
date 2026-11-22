"""An always-on entry sent condensed goes in full with a turn that is about it
(engine/lore_whole_back.py, engine/session_lore_condense.py): ranked against
the turn with the rest of the lore, by similarity fused with keyword scoring;
in the tail, beside the condensed version, which stays in the system block.
The mock provider and mock embeddings: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine import lore_whole_back
from sealedlore.engine.lore_whole_back import whole_back
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.retrieval import Injection, RetrievalReport, render_entry
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import EmbeddingProviderConfig
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.storage.repository import StoryBundle
from sealedlore.storage.tavern_lore import lore_from_book
from tests.test_session import make_config


@pytest.fixture
def top(monkeypatch):
    """The switch, as the kit sets it."""

    def set_to(value: int) -> None:
        monkeypatch.setattr(lore_whole_back, "WHOLE_BACK_TOP", value)

    set_to(2)
    return set_to


def entry(title: str, sentence: str, *, priority: int = 0, **fields) -> LoreEntry:
    item = LoreEntry(
        title=title,
        content=" ".join([sentence] * 30) + " The last line says what only the whole knows.",
        always_on=True,
        priority=priority,
        **fields,
    )
    item.condensed = f"{title}, in short."
    item.condensed_from = lore_fingerprint(item.content)
    return item


def harbour() -> LoreEntry:
    return entry("The Harbour", "Ships moor in the harbour at high tide by the grey docks.")


def keep() -> LoreEntry:
    return entry("The Keep", "The keep's walls and towers stand over the valley.", priority=9)


def guild() -> LoreEntry:
    return entry("The Guild", "The thieves' guild meets beneath the market by candlelight.")


def wolves() -> LoreEntry:
    return LoreEntry(title="Wolves", content="They hunt in threes.", keywords=["wolf"])


def session_with(tmp_path, story, cast, lore, *, cap: int = 500, replies=()) -> StorySession:
    story.held_character_id = "char-serrik"
    config = make_config()
    config.scene_reads = "manual"
    # Chosen from rather than sent whole, and the always-on entries over their
    # room (`Config.lore_cap`): each is ~380 tokens, the Keep matters most.
    config.lore_whole_share = 0.0
    config.lore_selector = "similarity"
    config.lore_limit = "tokens"
    config.lore_token_cap = cap
    return StorySession(
        StoryBundle(story=story, cast=cast, lore=lore),
        config,
        MockChatProvider(list(replies) or ["A passage."] * 6),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def with_embeddings(session: StorySession) -> MockEmbeddings:
    session.config.embedding_provider = EmbeddingProviderConfig(model="mock-embed")
    backend = MockEmbeddings()
    session.embeddings = backend
    return backend


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def sent(session: StorySession) -> str:
    request = session.provider.requests[-1]
    return "\n".join(part.text for message in request.messages for part in message.parts)


def system_block(session: StorySession) -> str:
    request = session.provider.requests[-1]
    return "\n".join(m.text for m in request.messages if m.role == "system")


def tail(session: StorySession) -> str:
    return session.provider.requests[-1].messages[-1].text


GUILD_TURN = "I ask the thieves of the guild when they meet beneath the market."


# --- the rule ------------------------------------------------------------------------


def test_a_turn_about_an_entry_sent_condensed_brings_its_whole_text_back(
    tmp_path, story, cast, top
):
    lore = [harbour(), keep(), guild(), wolves()]
    session = session_with(tmp_path, story, cast, lore)
    assert set(session.lore_layout().condensed) == {lore[0].id, lore[2].id}  # the Keep stays
    list(session.send(turn(GUILD_TURN)))
    report = session.last_retrieval
    (back,) = report.in_full
    assert back.name == "The Guild" and back.entry.id == lore[2].id and back.rank == 1
    assert back.entry.title == "The Guild (in full)"
    # Whole in the tail, condensed still in the system block.
    assert "## The Guild (in full)\n" + lore[2].content in tail(session)
    assert "The Guild, in short." in system_block(session)
    assert lore[2].content not in system_block(session)
    assert report.tokens == sum(i.tokens for i in report.injected)


def test_a_turn_about_something_else_brings_nothing_back(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    list(session.send(turn("I hum quietly to myself.")))
    assert session.last_retrieval.in_full == ()
    assert "(in full)" not in sent(session)


def test_an_entry_sent_whole_takes_a_place_among_the_turns_best_matches(tmp_path, story, cast, top):
    """Ranked among the condensed alone, a card whose lore is all always-on
    would bring the first of them back whatever the turn was about."""
    top(1)
    lore = [harbour(), keep(), guild()]
    session = session_with(tmp_path, story, cast, lore)
    list(session.send(turn("I climb the keep's walls to the tallest of its towers.")))
    assert session.last_retrieval.in_full == ()  # the Keep is first, and sent whole already
    top(2)
    list(session.send(turn("I climb the keep's walls and look down at the harbour ships.")))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Harbour" and back.rank == 2


def test_by_similarity_where_there_are_embeddings(tmp_path, story, cast, top):
    lore = [harbour(), keep(), guild()]
    session = session_with(tmp_path, story, cast, lore)
    with_embeddings(session)
    list(session.send(turn(GUILD_TURN)))
    (back,) = [i for i in session.last_retrieval.in_full if i.name == "The Guild"]
    assert back.score is not None and back.rank == 1
    assert (
        back.describe()
        == f"in full this turn: the turn's match no. 1 (similarity {back.score:.2f})"
    )


def test_the_names_are_filled_in_the_whole_text(tmp_path, story, cast, top):
    ledger = entry("The Ledger", "{{user}} keeps the harbour ledger of every hull.")
    session = session_with(tmp_path, story, cast, [ledger, keep(), guild()])
    list(session.send(turn("I open the harbour ledger and read the hulls.")))
    assert [i.name for i in session.last_retrieval.in_full][0] == "The Ledger"
    assert "Serrik Vaun keeps the harbour ledger" in tail(session)
    assert "{{user}}" not in sent(session)


# --- what it never changes -----------------------------------------------------------


def test_the_system_block_is_the_same_with_and_without(tmp_path, story, cast, top):
    blocks = {}
    for k in (0, 2):
        top(k)
        own = story.model_copy(deep=True)
        session = session_with(tmp_path / str(k), own, cast, [harbour(), keep(), guild()])
        list(session.send(turn(GUILD_TURN)))
        blocks[k] = system_block(session)
        list(session.send(turn("I look down at the harbour ships.")))
        assert system_block(session) == blocks[k]
    assert blocks[0] == blocks[2]


def test_off_it_sends_nothing_new(tmp_path, story, cast, top):
    top(0)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild(), wolves()])
    backend = with_embeddings(session)
    list(session.send(turn(GUILD_TURN)))
    assert session.last_retrieval.in_full == () and "(in full)" not in sent(session)
    # Nothing ranked for it: the turn's own selection's calls alone.
    asked = [texts for texts, as_query in backend.calls if as_query]
    assert asked == [(GUILD_TURN,)]


def test_nothing_comes_back_when_nothing_is_condensed(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()], cap=5_000)
    assert session.lore_layout().condensed == ()
    list(session.send(turn(GUILD_TURN)))
    assert session.last_retrieval.in_full == ()


def test_the_preview_never_embeds(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    backend = with_embeddings(session)
    session.assemble(turn(GUILD_TURN))
    assert backend.calls == []
    report = session.retrieve(turn(GUILD_TURN), session.path(), allow_network=False)
    assert [i.name for i in report.in_full] == ["The Guild"]  # by words alone
    assert report.in_full[0].score is None and backend.calls == []
    # Once a turn has embedded it, the preview ranks by the vectors made.
    list(session.send(turn(GUILD_TURN)))
    made = len(backend.calls)
    report = session.retrieve(turn(GUILD_TURN), session.path()[:-2], allow_network=False)
    assert report.in_full[0].score is not None and len(backend.calls) == made


def test_regenerate_brings_back_the_same(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    with_embeddings(session)
    list(session.send(turn(GUILD_TURN)))
    first, first_tail = session.last_retrieval.in_full, tail(session)
    list(session.regenerate())
    again = session.last_retrieval.in_full
    assert [(i.entry.id, i.rank, i.tokens) for i in again] == [
        (i.entry.id, i.rank, i.tokens) for i in first
    ]
    assert tail(session) == first_tail


# --- where it runs ---------------------------------------------------------------------


def test_a_private_scene_ranks_by_words_alone(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    backend = with_embeddings(session)
    private = MockChatProvider(["In private."] * 3)
    session.enter_private(keep="memory", provider=private, model="local/model")
    list(session.send(turn(GUILD_TURN)))
    assert backend.calls == []
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.score is None
    text = "\n".join(part.text for m in private.requests[-1].messages for part in m.parts)
    assert "## The Guild (in full)" in text


def test_a_chat_has_no_lore_to_bring_back(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    session.story.mode = "chat"
    list(session.send(turn(GUILD_TURN)))
    assert session.last_retrieval is None and "(in full)" not in sent(session)


def test_under_sillytaverns_rules_too_and_only_then_embedded(tmp_path, story, cast, top):
    book = {
        "entries": {
            str(i): {
                "uid": i,
                "comment": title,
                "key": [],
                "constant": True,
                "order": order,
                "content": " ".join([sentence] * 60),
            }
            for i, (title, sentence, order) in enumerate(
                (
                    ("The Harbour", "Ships moor in the harbour at high tide.", 300),
                    ("The Guild", "The thieves' guild meets beneath the market.", 10),
                    ("The Keep", "The keep's walls stand over the valley.", 200),
                )
            )
        }
    }
    lore = lore_from_book(book).entries
    for item in lore:
        item.condensed = f"{item.title}, in short."
        item.condensed_from = lore_fingerprint(item.content)
    story.defaults.lore_firing = "sillytavern"
    session = session_with(tmp_path, story, cast, lore)
    backend = with_embeddings(session)
    session.story.defaults.context_token_budget = 6_000  # its budget: 1,500
    assert session.lore_layout().condensed == (lore[1].id,)  # order 10: condensed first
    list(session.send(turn(GUILD_TURN)))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.score is not None  # embedded for it
    assert "## The Guild (in full)" in tail(session)
    assert any(not as_query for _texts, as_query in backend.calls)
    # With nothing condensed, nothing is embedded under SillyTavern's rules.
    session.story.defaults.context_token_budget = 60_000
    backend.calls.clear()
    list(session.send(turn(GUILD_TURN)))
    assert session.lore_layout().condensed == () and backend.calls == []


# --- the room ----------------------------------------------------------------------------


def test_an_entry_too_big_for_its_room_goes_in_its_best_parts(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()], cap=300)
    list(session.send(turn(GUILD_TURN)))
    (back, *_rest) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.fitted and back.tokens <= 300
    assert "in part" in back.describe()


def test_the_first_whole_the_next_in_what_is_left(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()], cap=700)
    list(session.send(turn("The thieves of the guild meet the harbour ships at the docks.")))
    first, second = session.last_retrieval.in_full
    assert not first.fitted and second.fitted
    assert first.tokens + second.tokens <= 700


def test_their_room_is_not_taken_from_the_chosen_lore(tmp_path, story, cast, top):
    """The chosen lore is fitted to its room (half of it at most an entry),
    the whole texts brought back to a room of their own."""
    pack = LoreEntry(
        title="Wolves", content="They hunt in threes along the ridge. " * 50, keywords=["wolf"]
    )
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild(), pack], cap=500)
    list(session.send(turn("A wolf howls as I ask the thieves of the guild when they meet.")))
    report = session.last_retrieval
    (chosen,) = [i for i in report.injected if i.reason != "in_full"]
    (back,) = report.in_full
    assert chosen.name == "Wolves" and chosen.fitted and chosen.tokens <= 250
    assert back.name == "The Guild" and not back.fitted
    assert chosen.tokens + back.tokens > 500


def test_whole_back_keeps_to_the_first_k_and_to_the_condensed():
    whole = LoreEntry(id="w", title="Whole", content="word " * 200)
    ranked = [("other", 0.7), ("w", 0.6)]
    args = dict(room=400, query="word", count_tokens=fallback_counter, label=lambda t: t + "!")
    assert whole_back([whole], ranked, top=1, **args) == ([], [])
    sent, left = whole_back([whole], ranked, top=2, **args)
    assert left == [] and sent[0].rank == 2 and sent[0].entry.title == "Whole!"
    assert sent[0].tokens == fallback_counter(render_entry(sent[0].entry))
    assert whole_back([whole], ranked, top=0, **args) == ([], [])


def test_the_lore_page_says_what_went_in_full_and_why():
    entry_ = LoreEntry(title="The Guild (in full)", content="x")
    injection = Injection(entry=entry_, reason="in_full", tokens=5, rank=2, title="The Guild")
    assert injection.name == "The Guild"
    assert injection.describe() == "in full this turn: the turn's match no. 2"
    report = RetrievalReport(injected=(injection,))
    assert report.in_full == (injection,)
