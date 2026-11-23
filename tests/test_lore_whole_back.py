"""The parts of an always-on entry sent condensed that the turn is about go
with the turn from its whole text (engine/lore_whole_back.py,
engine/session_lore_condense.py): each part ranked against the turn with the
rest of the lore, by similarity fused with keyword scoring; those within the
first K in the tail under the entry's title, its condensed version staying in
the system block. The mock provider and mock embeddings: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine import lore_whole_back
from sealedlore.engine.lore_whole_back import (
    PART_LEAST_WORDS,
    PART_MOST_WORDS,
    in_condensed,
    joined,
    parts_back,
    piece_id,
    pieces,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_edits import set_text
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.retrieval import GAP, Injection, RetrievalReport, content_hash
from sealedlore.engine.session import StorySession
from sealedlore.engine.session_lore_vectors import PARTS_BEING_MADE
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

    set_to(3)
    return set_to


# Each paragraph about one thing, in words no other shares, and long enough to
# stand as a part of its own (`PART_LEAST_WORDS`).
GUILD = [
    "The thieves' guild meets beneath the market by candlelight, in a cellar under the "
    "fishmongers' stalls where the floor is always wet and the candles never stay lit.",
    "Its master is a one-eyed woman called Hesk, who keeps the guild's ledger of every debt "
    "in a cipher of her own and never lets that book out of her sight, not even asleep.",
    "Dues are paid in silver on the night of the new moon; a member who cannot pay works the "
    "debt off on the docks, unloading salt barrels until the tally is square again.",
    "Members greet each other with a tap of two fingers to the brow, a sign the city watch "
    "has never learned to read, and they never speak a fellow member's true name aloud.",
]
HARBOUR = [
    "Ships moor in the harbour at high tide beside the grey docks, where the cranes swing "
    "cargo from their holds and the gulls quarrel over whatever falls into the water.",
    "The harbourmaster, a stout man named Orrin Pell, rings a brass bell at each turn of the "
    "tide and fines any captain whose hull still blocks the channel when it sounds.",
    "A lighthouse of black stone stands on the breakwater, and its keeper burns whale oil "
    "that the fishing fleet brings him every spring in return for a safe light home.",
]
KEEP = [
    "The keep's walls and towers stand over the valley, cut from the same pale granite as "
    "the cliffs beneath them, and no army has ever breached its outer gate by force.",
    "Its tallest tower holds the old armoury, where the lords of the valley once kept their "
    "banners, their siege engines and the iron chains they hung across the river.",
]


def entry(title: str, paragraphs: list[str], *, priority: int = 0, **fields) -> LoreEntry:
    item = LoreEntry(
        title=title,
        content="\n\n".join(paragraphs),
        always_on=True,
        priority=priority,
        **fields,
    )
    item.condensed = f"{title}, in short."
    item.condensed_from = lore_fingerprint(item.content)
    return item


def guild() -> LoreEntry:
    return entry("The Guild", GUILD)


def harbour() -> LoreEntry:
    return entry("The Harbour", HARBOUR)


def keep() -> LoreEntry:
    return entry("The Keep", KEEP, priority=9)


def wolves() -> LoreEntry:
    return LoreEntry(title="Wolves", content="They hunt in threes.", keywords=["wolf"])


# A storyteller the parts are sent to: GLM 5.3 was measured better with them
# (Claude models aren't sent them: engine/model_tuning.py).
GLM = "z-ai/glm-5.3"


def session_with(
    tmp_path, story, cast, lore, *, cap: int = 200, replies=(), model: str = GLM
) -> StorySession:
    story.held_character_id = "char-serrik"
    config = make_config(model)
    config.scene_reads = "manual"
    # Chosen from rather than sent whole, and the always-on entries over their
    # room (`Config.lore_cap`): the Keep matters most and stays whole.
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


def made_ahead(session: StorySession) -> None:
    """The lore's vectors made ahead of the turn and taken in, as the window
    has them made once the story opens (the mock embeds then and there)."""
    assert session.start_lore_vectors()
    assert session.adopt_lore_vectors() == []


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


HESK_TURN = "I ask who Hesk is, and whether that one-eyed woman keeps the ledger in a cipher."
TITLE = "## The Guild (from the full entry)\n"


# --- the rule ------------------------------------------------------------------------


def test_a_turn_about_one_part_of_an_entry_sent_condensed_brings_that_part(
    tmp_path, story, cast, top
):
    top(1)
    lore = [harbour(), keep(), guild(), wolves()]
    session = session_with(tmp_path, story, cast, lore)
    assert set(session.lore_layout().condensed) == {lore[0].id, lore[2].id}  # the Keep stays
    list(session.send(turn(HESK_TURN)))
    report = session.last_retrieval
    (back,) = report.in_full
    assert back.name == "The Guild" and back.entry.id == lore[2].id
    assert back.matches == ((1, None),) and back.rank == 1
    assert back.entry.title == "The Guild (from the full entry)"
    # That part alone, under its entry's title, with gap marks where the rest
    # of the entry is; the condensed version still in the system block.
    assert TITLE + f"{GAP}\n\n{GUILD[1]}\n\n{GAP}" in tail(session)
    for other in (GUILD[0], GUILD[2], GUILD[3]):
        assert other not in sent(session)
    assert "The Guild, in short." in system_block(session)
    assert GUILD[1] not in system_block(session)
    assert report.tokens == sum(i.tokens for i in report.injected)
    assert back.whole == fallback_counter(f"## The Guild\n{lore[2].content}")


def test_a_turn_about_something_else_brings_other_parts_or_none(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    list(session.send(turn("I hum quietly to myself.")))
    assert session.last_retrieval.in_full == ()
    assert "(from the full entry)" not in sent(session)
    top(1)
    list(session.send(turn("When does the harbourmaster ring his brass bell for the tide?")))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Harbour" and HARBOUR[1] in tail(session)
    assert not any(paragraph in sent(session) for paragraph in GUILD)


def test_the_parts_of_one_entry_go_together_in_its_own_order(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    list(
        session.send(
            turn("I pay my dues in silver at the new moon, and ask Hesk to strike my debt.")
        )
    )
    guilds = [i for i in session.last_retrieval.in_full if i.name == "The Guild"]
    (back,) = guilds  # one heading, however many of its parts
    ranks = [rank for rank, _ in back.matches]
    assert ranks == sorted(ranks) and len(ranks) >= 2
    text = tail(session)
    assert text.count(TITLE) == 1
    # Hesk's and the dues' parts, next to each other in the entry: no gap
    # between them, one before (the first part isn't sent).
    assert f"{TITLE}{GAP}\n\n{GUILD[1]}\n\n{GUILD[2]}" in text
    assert back.describe().startswith(f"from the full entry: {len(ranks)} parts, the turn's")


def test_an_entry_sent_whole_takes_a_place_among_the_turns_best_matches(tmp_path, story, cast, top):
    """Ranked among the parts alone, a card whose lore is all always-on would
    bring the first of them back whatever the turn was about."""
    top(1)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    list(session.send(turn("I climb the keep's granite walls to the tallest of its towers.")))
    assert session.last_retrieval.in_full == ()  # the Keep is first, and sent whole already
    top(2)
    list(session.send(turn("From the keep's granite walls I watch the lighthouse keeper.")))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Harbour" and back.rank == 2
    assert HARBOUR[2] in tail(session)


def test_a_part_the_condensed_version_holds_word_for_word_is_not_sent_again(
    tmp_path, story, cast, top
):
    top(1)
    lore = [harbour(), keep(), guild()]
    lore[2].condensed = f"The guild, in short. {GUILD[1]}"  # Hesk's part, as written
    session = session_with(tmp_path, story, cast, lore)
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == ()  # it ranks first, and is there already
    assert system_block(session).count(GUILD[1]) == 1


def test_by_similarity_where_there_are_embeddings(tmp_path, story, cast, top):
    top(1)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    with_embeddings(session)
    made_ahead(session)
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full_note == ""
    (back,) = session.last_retrieval.in_full
    ((rank, score),) = back.matches
    assert back.name == "The Guild" and rank == 1 and score is not None
    assert back.describe() == (
        f"from the full entry: 1 part, the turn's match no. 1 (similarity {score:.2f}); "
        f"{back.tokens} of its {back.whole} tokens"
    )


def test_each_parts_vector_is_made_once_and_kept_with_the_entries(tmp_path, story, cast, top):
    lore = [harbour(), keep(), guild()]
    session = session_with(tmp_path, story, cast, lore)
    backend = with_embeddings(session)
    made_ahead(session)
    documents = [text for texts, as_query in backend.calls if not as_query for text in texts]
    parts = [piece for item in (lore[0], lore[2]) for piece in pieces(item)]
    assert len(parts) == len(HARBOUR) + len(GUILD)
    # Each part with its entry's title, as an entry is embedded.
    assert {f"{p.entry.title}\n{p.text}" for p in parts} <= set(documents)
    backend.calls.clear()
    list(session.send(turn(HESK_TURN)))
    assert all(as_query for _texts, as_query in backend.calls)  # the turn's alone
    cached = {row.content_hash for row in session.bundle.embeddings}
    assert {content_hash(f"{p.entry.title}\n{p.text}") for p in parts} <= cached
    assert all(row.kind == "lore" for row in session.bundle.embeddings)
    # Kept when the entries' vectors are seen to on their own, and never made again.
    rows = len(session.bundle.embeddings)
    session.ensure_lore_vectors()
    assert len(session.bundle.embeddings) == rows
    backend.calls.clear()
    list(session.send(turn("When does the harbourmaster ring his brass bell?")))
    assert all(as_query for _texts, as_query in backend.calls)


def test_the_names_are_filled_in_the_parts(tmp_path, story, cast, top):
    top(1)
    ledger = entry(
        "The Ledger",
        [
            "{{user}} keeps the harbour ledger of every hull that has ever moored here, "
            "each with its tonnage, its master's name and the dues it paid in silver.",
            *HARBOUR[1:],
        ],
    )
    session = session_with(tmp_path, story, cast, [ledger, keep(), guild()])
    list(session.send(turn("I open the harbour ledger and read the tonnage of every hull.")))
    assert [i.name for i in session.last_retrieval.in_full] == ["The Ledger"]
    assert "Serrik Vaun keeps the harbour ledger" in tail(session)
    assert "{{user}}" not in sent(session)


# --- what it never changes -----------------------------------------------------------


def test_the_system_block_is_the_same_with_and_without(tmp_path, story, cast, top):
    blocks = {}
    for k in (0, 3):
        top(k)
        own = story.model_copy(deep=True)
        session = session_with(tmp_path / str(k), own, cast, [harbour(), keep(), guild()])
        list(session.send(turn(HESK_TURN)))
        blocks[k] = system_block(session)
        list(session.send(turn("I look down at the harbour ships.")))
        assert system_block(session) == blocks[k]
    assert blocks[0] == blocks[3]


def test_off_it_sends_nothing_new_and_embeds_no_part(tmp_path, story, cast, top):
    top(0)
    lore = [harbour(), keep(), guild(), wolves()]
    session = session_with(tmp_path, story, cast, lore)
    backend = with_embeddings(session)
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == () and "(from the full entry)" not in sent(session)
    # The turn's own selection's calls alone: the turn, and each entry whole.
    assert [texts for texts, as_query in backend.calls if as_query] == [(HESK_TURN,)]
    documents = [text for texts, as_query in backend.calls if not as_query for text in texts]
    assert sorted(documents) == sorted(f"{e.title}\n{e.content}" for e in lore)
    assert session._part_texts() == {}


def test_nothing_comes_back_when_nothing_is_condensed(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()], cap=5_000)
    assert session.lore_layout().condensed == ()
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval.in_full == ()
    assert session._part_texts() == {}


def test_the_preview_never_embeds(tmp_path, story, cast, top):
    top(1)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    backend = with_embeddings(session)
    session.assemble(turn(HESK_TURN))
    assert backend.calls == []
    report = session.retrieve(turn(HESK_TURN), session.path(), allow_network=False)
    assert [i.name for i in report.in_full] == ["The Guild"]  # by words alone
    assert report.in_full[0].score is None and backend.calls == []
    # Once they are made, and a turn has embedded its text, the preview ranks
    # by the vectors made.
    made_ahead(session)
    list(session.send(turn(HESK_TURN)))
    made = len(backend.calls)
    report = session.retrieve(turn(HESK_TURN), session.path()[:-2], allow_network=False)
    assert report.in_full[0].score is not None and len(backend.calls) == made


def test_regenerate_brings_back_the_same(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    with_embeddings(session)
    made_ahead(session)
    list(session.send(turn(HESK_TURN)))
    first, first_tail = session.last_retrieval.in_full, tail(session)
    list(session.regenerate())
    again = session.last_retrieval.in_full
    assert [(i.entry.id, i.matches, i.tokens) for i in again] == [
        (i.entry.id, i.matches, i.tokens) for i in first
    ]
    assert tail(session) == first_tail


# --- where it runs ---------------------------------------------------------------------


def test_a_private_scene_ranks_by_words_alone(tmp_path, story, cast, top):
    top(1)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    backend = with_embeddings(session)
    private = MockChatProvider(["In private."] * 3)
    session.enter_private(keep="memory", provider=private, model="local/model")
    list(session.send(turn(HESK_TURN)))
    assert backend.calls == []
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.matches == ((1, None),)
    text = "\n".join(part.text for m in private.requests[-1].messages for part in m.parts)
    assert TITLE + f"{GAP}\n\n{GUILD[1]}" in text


def test_a_chat_has_no_lore_to_bring_back(tmp_path, story, cast, top):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    session.story.mode = "chat"
    list(session.send(turn(HESK_TURN)))
    assert session.last_retrieval is None and "(from the full entry)" not in sent(session)
    assert session._part_texts() == {}


def test_under_sillytaverns_rules_too_and_only_then_embedded(tmp_path, story, cast, top):
    top(1)
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
                (
                    ("The Harbour", HARBOUR, 300),
                    ("The Guild", GUILD, 10),
                    ("The Keep", KEEP, 200),
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
    # Nothing is embedded in the turn: by words, the first time.
    list(session.send(turn(HESK_TURN)))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.score is None
    assert session.last_retrieval.in_full_note == PARTS_BEING_MADE
    assert TITLE in tail(session) and GUILD[1] in tail(session)
    # Made after its passage, for the next turn.
    assert any(not as_query for _texts, as_query in backend.calls)
    list(session.send(turn(HESK_TURN)))
    (back,) = session.last_retrieval.in_full
    assert back.name == "The Guild" and back.score is not None  # embedded for it
    # With nothing condensed, nothing is embedded under SillyTavern's rules.
    session.story.defaults.context_token_budget = 60_000
    backend.calls.clear()
    list(session.send(turn(HESK_TURN)))
    assert session.lore_layout().condensed == () and backend.calls == []


# --- the room ----------------------------------------------------------------------------


def test_their_room_is_the_chosen_lores_up_to_its_most(tmp_path, story, cast, top, monkeypatch):
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()], cap=200)
    list(session.send(turn("I pay my dues in silver at the new moon and ask Hesk about it.")))
    assert 0 < sum(i.tokens for i in session.last_retrieval.in_full) <= 200
    before = sum(len(i.matches) for i in session.last_retrieval.in_full)
    monkeypatch.setattr(lore_whole_back, "ROOM_MOST", 60)
    list(session.send(turn("I pay my dues in silver at the new moon and ask Hesk about it.")))
    # Whatever the room for chosen lore, the parts take no more than their most.
    after = session.last_retrieval.in_full
    assert sum(i.tokens for i in after) <= 60 < 200
    assert sum(len(i.matches) for i in after) < before


def test_their_room_is_not_taken_from_the_chosen_lore(tmp_path, story, cast, top):
    """The chosen lore is fitted to its room (half of it at most an entry),
    the parts to a room of their own."""
    pack = LoreEntry(
        title="Wolves", content="They hunt in threes along the ridge. " * 30, keywords=["wolf"]
    )
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild(), pack], cap=200)
    list(session.send(turn("A wolf howls as I ask Hesk whether she keeps the ledger.")))
    report = session.last_retrieval
    (chosen,) = [i for i in report.injected if i.reason != "in_full"]
    assert chosen.name == "Wolves" and chosen.fitted and chosen.tokens <= 150
    assert report.in_full and chosen.tokens + sum(i.tokens for i in report.in_full) > 150


def count(text: str) -> int:
    return fallback_counter(text)


def label(title: str) -> str:
    return f"{title} (from the full entry)"


def test_parts_back_fits_best_first_a_part_cut_short_then_none():
    guild_ = guild()
    ranked = [(piece_id(guild_.id, index), None) for index in (2, 0, 3)]
    whole = count(render(guild_, {2: GUILD[2]}))
    sent = parts_back([guild_], ranked, top=3, room=whole + 60, count_tokens=count, label=label)
    (back,) = sent
    # The first whole; the second cut short at a sentence, marked; the third
    # left out. Each where it stands in the entry, gap marks between.
    assert back.matches == ((1, None), (2, None))
    assert back.entry.content.startswith(GUILD[0][:20])
    assert f"{GAP}\n\n{GUILD[2]}\n\n{GAP}" in back.entry.content
    assert back.tokens <= whole + 60
    assert parts_back([guild_], ranked, top=0, room=500, count_tokens=count, label=label) == []
    assert parts_back([guild_], ranked, top=3, room=0, count_tokens=count, label=label) == []


def render(item: LoreEntry, chosen: dict[int, str]) -> str:
    return f"## {label(item.title)}\n" + joined(chosen, len(pieces(item)))


def test_parts_back_keeps_to_the_first_k_and_to_the_parts():
    guild_ = guild()
    ranked = [("something-whole", 0.7), (piece_id(guild_.id, 3), 0.6)]
    args = dict(room=1500, count_tokens=count, label=label)
    assert parts_back([guild_], ranked, top=1, **args) == []
    (back,) = parts_back([guild_], ranked, top=2, **args)
    assert back.matches == ((2, 0.6),) and back.score == 0.6
    assert back.entry.content == f"{GAP}\n\n{GUILD[3]}"  # the last part: no gap after it


# --- the parts ---------------------------------------------------------------------------


def test_an_entry_is_cut_into_its_paragraphs_a_heading_with_the_part_it_heads():
    item = LoreEntry(title="T", content="SHIPS:\n\n" + "\n\n".join(HARBOUR))
    found = pieces(item)
    assert [p.text for p in found] == [f"SHIPS:\n\n{HARBOUR[0]}", *HARBOUR[1:]]
    assert [p.index for p in found] == [0, 1, 2]
    assert found[1].id == piece_id(item.id, 1)
    # A short last part goes with the one before it.
    found = pieces(LoreEntry(title="T", content=f"{HARBOUR[0]}\n\nThe end."))
    assert [p.text for p in found] == [f"{HARBOUR[0]}\n\nThe end."]
    assert PART_LEAST_WORDS == 20


def test_a_part_over_its_most_is_cut_at_its_sentences_into_pieces_of_a_size():
    sentence = "The harbour wall runs from the old quay to the lighthouse rock. "  # 12 words
    long_text = (sentence * 60).strip()  # 720 words, one paragraph
    found = pieces(LoreEntry(title="T", content=long_text))
    assert [len(p.text.split()) for p in found] == [240, 240, 240]
    assert 3 * 240 > 2 * PART_MOST_WORDS
    assert all(p.text.endswith(".") for p in found)
    assert " ".join(p.text for p in found).split() == long_text.split()
    # A scrap left at the end goes with the piece before it.
    sentence = "The harbour wall runs out from the old quay to the lighthouse rock and back. "
    long_text = (sentence * 49).strip()  # 735 words: three of 240 and 15 over
    words = [len(p.text.split()) for p in pieces(LoreEntry(title="T", content=long_text))]
    assert words == [240, 240, 255]
    # Lines too: a profile written as attribute lines is cut between them.
    lines = "\n".join(f"Line {i}: " + "word " * 30 for i in range(20))
    found = pieces(LoreEntry(title="T", content=lines))
    assert all(p.text.startswith("Line ") for p in found)
    assert "\n".join(p.text for p in found).split() == lines.split()


def test_in_condensed_compares_word_for_word():
    item = guild()
    (_, hesk, *_) = pieces(item)
    assert not in_condensed(hesk)
    item.condensed = "In short. " + " ".join(GUILD[1].split())
    assert in_condensed(pieces(item)[1])
    item.condensed = "In short. " + GUILD[1].replace("Hesk", "Hesky")
    assert not in_condensed(pieces(item)[1])


# --- the label and the Lore page ---------------------------------------------------------


def test_the_label_is_a_prompt_text_of_its_own(tmp_path, story, cast, top):
    assert DEFAULT_TEXTS.fill("turn.lore.from_full", title="X") == "X (from the full entry)"
    top(1)
    session = session_with(tmp_path, story, cast, [harbour(), keep(), guild()])
    set_text(session.story.prompt_edits, "turn.lore.from_full", "{title}, as written")
    list(session.send(turn(HESK_TURN)))
    assert "## The Guild, as written\n" in tail(session)
    # The measured heading above it is the same as ever.
    assert DEFAULT_TEXTS["turn.lore"] in tail(session)


def test_the_lore_page_says_what_came_from_the_full_entry():
    entry_ = LoreEntry(title="The Guild (from the full entry)", content="x")
    injection = Injection(
        entry=entry_,
        reason="in_full",
        tokens=50,
        rank=1,
        whole=400,
        title="The Guild",
        matches=((1, 0.61), (3, 0.55)),
    )
    assert injection.name == "The Guild"
    assert injection.describe() == (
        "from the full entry: 2 parts, the turn's matches no. 1 and 3 (similarity 0.61, 0.55); "
        "50 of its 400 tokens"
    )
    three = Injection(
        entry=entry_, reason="in_full", tokens=5, matches=((1, None), (2, None), (4, None))
    )
    assert three.describe() == "from the full entry: 3 parts, the turn's matches no. 1, 2 and 4"
    report = RetrievalReport(injected=(injection,))
    assert report.in_full == (injection,)
