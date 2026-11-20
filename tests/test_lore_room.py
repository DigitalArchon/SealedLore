"""The room a turn has for its chosen lore, and long entries sent in part
(engine/retrieval.py `fit_lore`, `Config.lore_cap`).

A fixed 2,000 tokens, each entry whole or not at all, left out the entries a
turn was about on a lorebook of long entries (a character's profile is some
1,400 tokens): on a 320-entry lorebook it dropped the Superd character when
the author asked about the Superd, with a fifth of a 60k budget used."""

from __future__ import annotations

from sealedlore.engine.lore_pick import choose_picked
from sealedlore.engine.retrieval import (
    ENTRY_MOST_SHARE,
    FIT_LEAST_TOKENS,
    GAP,
    Injection,
    entry_parts,
    fit_lore,
    fitted_entry,
    render_entry,
    select_lore,
)
from sealedlore.models.config import Config
from sealedlore.models.lore import LoreEntry


def tokens(text: str) -> int:
    return len(text.split())


PROFILE = "\n".join(
    [
        "Elena Ruiz is the farm's nurse, thirty-four, who kept the city clinic open "
        "until the last week.",
        "",
        "[Appearance]",
        "Dark hair cut short with kitchen scissors, a scar across her left palm, "
        + "always in the same grey wool coat " * 14,
        "",
        "[Medicine]",
        "She keeps the antibiotics in a tin box under the floor and counts every pill twice; "
        + "she has rationed the insulin since the autumn " * 14,
        "",
        "[The barn fire]",
        "She has not slept a whole night since the barn burned and the Ortiz boy died in it, "
        + "and she blames herself for the lantern " * 14,
    ]
)


def elena() -> LoreEntry:
    return LoreEntry(id="elena", title="Elena Ruiz", content=PROFILE, keywords=["Elena"])


def test_an_entry_is_read_in_its_parts():
    parts = entry_parts(PROFILE)
    assert len(parts) == 4
    assert parts[0].startswith("Elena Ruiz is the farm's nurse")
    assert parts[2].startswith("[Medicine]\nShe keeps the antibiotics")
    # A heading starts a part of its own even with no blank line before it.
    assert entry_parts("Intro line.\n[Next]\nIts text.") == ["Intro line.", "[Next]\nIts text."]


def test_in_part_it_keeps_its_opening_and_what_the_turn_is_about():
    part = fitted_entry(
        elena(), 140, "I ask Elena how much insulin and antibiotics are left", tokens
    )
    assert part is not None and part.id == "elena"
    text = part.content
    assert text.startswith("Elena Ruiz is the farm's nurse")
    # The part about medicine, cut to what is left, rather than one about
    # nothing the turn says.
    assert "[Medicine]\nShe keeps the antibiotics" in text
    assert "[Appearance]" not in text and "[The barn fire]" not in text
    assert text.count(GAP) >= 2
    assert tokens(render_entry(part)) <= 140
    # The entry itself is as it was.
    assert elena().content == PROFILE


def test_an_opening_too_long_for_the_room_is_cut_at_a_sentence():
    entry = LoreEntry(title="Long", content="One two three four. Five six seven eight. Nine ten.")
    part = fitted_entry(entry, 10, "", tokens)
    assert part is not None and part.content == f"One two three four. {GAP}"
    # No sentence fits: cut at a word; not even a word: nothing.
    part = fitted_entry(entry, 6, "", tokens)
    assert part is not None and part.content == f"One two {GAP}"
    assert fitted_entry(entry, 4, "", tokens) is None


def injection(entry: LoreEntry, reason: str = "keyword") -> Injection:
    return Injection(entry=entry, reason=reason, tokens=tokens(render_entry(entry)))


def small(name: str, words: int = 40) -> LoreEntry:
    return LoreEntry(id=name, title=name, content=" ".join([name] * words))


def test_each_goes_whole_while_it_fits_and_a_long_one_in_part():
    whole = tokens(render_entry(elena()))
    assert whole > 300
    room = 400
    sent, left = fit_lore(
        [injection(small("Hellsville")), injection(elena()), injection(small("The farm"))],
        room,
        query="insulin",
        count_tokens=tokens,
    )
    assert [i.entry.title for i in sent] == ["Hellsville", "Elena Ruiz", "The farm"] and not left
    hells, part, farm = sent
    assert not hells.fitted and not farm.fitted
    # No one entry takes more than half the room, so those after it come too.
    assert part.fitted and part.whole == whole and part.tokens <= room * ENTRY_MOST_SHARE
    assert "insulin" in part.entry.content
    assert "in part" in part.describe() and f"of its {whole} tokens" in part.describe()
    assert sum(i.tokens for i in sent) <= room


def test_only_with_too_little_room_left_is_an_entry_left_out():
    room = 2 * 210
    sent, left = fit_lore(
        [injection(small("A", 200)), injection(elena()), injection(small("B", 10))],
        room,
        query="",
        count_tokens=tokens,
    )
    # A whole (it fits its half), Elena cut to what is left, B in what remains.
    assert [i.entry.title for i in sent][:2] == ["A", "Elena Ruiz"] and sent[1].fitted
    assert sum(i.tokens for i in sent) <= room
    # A paragraph with no sentence in it is cut at a word, not left out.
    sent, left = fit_lore([injection(small("C", 600))], room, query="", count_tokens=tokens)
    assert sent[0].fitted and sent[0].tokens <= room * ENTRY_MOST_SHARE and not left
    # Less than the least left: out.
    sent, left = fit_lore(
        [injection(small("A", 156)), injection(small("B", 156)), injection(elena())],
        2 * FIT_LEAST_TOKENS + 20,
        query="",
        count_tokens=tokens,
    )
    assert [i.entry.title for i in sent] == ["A", "B"] and not any(i.fitted for i in sent)
    assert [i.entry.title for i in left] == ["Elena Ruiz"]


def test_selection_sends_the_entry_the_turn_names_where_the_old_cap_dropped_it():
    filler = [
        LoreEntry(id=f"m{i}", title=f"Magic {i}", content="magic " * 120, keywords=["magic"])
        for i in range(6)
    ]
    entries = [*filler, elena()]
    turn = "I ask Elena about the magic insulin"
    report = select_lore(entries, keyword_text=turn, token_cap=600, count_tokens=tokens)
    titles = [i.entry.title for i in report.injected]
    assert "Elena Ruiz" in titles
    assert report.tokens <= 600 and report.tokens == sum(i.tokens for i in report.injected)


def test_picks_are_fitted_the_same_way():
    report = choose_picked(
        [small("Hellsville"), elena()],
        ["elena", "Hellsville"],
        turn_text="the barn fire",
        token_cap=300,
        count_tokens=tokens,
    )
    first = report.injected[0]
    assert first.entry.title == "Elena Ruiz" and first.fitted
    assert "[The barn fire]" in first.entry.content


def test_the_room_is_a_share_of_the_budget_or_a_number():
    config = Config()
    assert config.lore_limit == "share" and config.lore_cap(60_000) == 6_000
    assert config.lore_cap(16_000) == 1_600
    config.lore_limit = "tokens"
    assert config.lore_cap(60_000) == config.lore_token_cap == 2_000


def test_the_session_gives_the_story_its_share_and_a_private_scene_its_own(
    tmp_path, story, monkeypatch
):
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import StoryBundle
    from tests.test_session import make_config

    story.defaults.context_token_budget = 40_000
    config = make_config()
    session = StorySession(
        StoryBundle(story=story),
        config,
        MockChatProvider(),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    assert session._lore_room() == 4_000
    config.private_budget = 20_000
    monkeypatch.setattr(StorySession, "in_private", property(lambda self: True))
    assert session._lore_room() == 2_000


def test_embedding_a_big_lorebook_sends_several_requests_at_once_in_order(tmp_path, story):
    """One request at a time, 320 entries took 39 s before a story's first
    turn; four at once, 10 s (Nov 20 2026). The vectors stay in order."""
    import threading
    import time

    from sealedlore.engine import session_recall
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.providers.embeddings import EmbeddingResult
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import StoryBundle
    from tests.test_session import make_config

    busy, most = [0], [0]
    lock = threading.Lock()

    class Slow:
        def embed(self, texts, *, as_query=False):
            with lock:
                busy[0] += 1
                most[0] = max(most[0], busy[0])
            time.sleep(0.05)
            with lock:
                busy[0] -= 1
            vectors = tuple((float(text.split()[0]),) for text in texts)
            return EmbeddingResult(vectors=vectors, model="m", dimensions=1)

    session = StorySession(
        StoryBundle(story=story),
        make_config(),
        MockChatProvider(),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        embeddings=Slow(),
    )
    texts = [f"{i} " + "word " * 2_000 for i in range(12)]
    vectors = session.embed_in_batches(texts)
    assert [v[0] for v in vectors] == [float(i) for i in range(12)]
    assert 1 < most[0] <= session_recall.EMBED_AT_ONCE


def test_a_keyword_names_its_plural_and_possessive_but_not_another_word():
    """SillyTavern matches a keyword as a substring unless told otherwise, so
    its lorebooks lean on "Adventurer" finding "Adventurers'"; whole words
    alone lost the entry to every plural (Nov 20 2026)."""
    from sealedlore.engine.retrieval import keyword_matches, mentions_entry

    entry = LoreEntry(title="Superd Race", content="x", keywords=["Adventurer", "accord"])
    for text in ("the Adventurers' Guild", "an adventurer", "the adventurer’s pack", "accords"):
        assert keyword_matches(text, entry), text
    for text in ("adventurous", "according to", "Adventurership"):
        assert not keyword_matches(text, entry), text
    assert mentions_entry("two Superd Races at war", entry)
