"""Condensed always-on lore (engine/lore_condense.py,
engine/session_lore_condense.py): written beside an entry on the author's
word, never in its place; sent only when always-on lore is more than the
lore budget holds, the fewest needed, least important first; carried by a
kept lorebook into the stories made from it. The mock provider: no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.lore_condense import (
    fit_standing,
    least_important_first,
    parse_condensed_lore,
    target_words,
    worth_condensing,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.models.lorebook import StoredLorebook
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.lorebooks import lore_of, scenario_from
from sealedlore.storage.repository import StoryBundle, load_story_bundle
from sealedlore.storage.tavern_lore import condensed_key, lore_from_book
from tests.test_session import make_config

WORDS = lambda text: len(text.split())  # noqa: E731 - a token a word


def entry(title: str, words: int, **fields) -> LoreEntry:
    return LoreEntry(title=title, content=" ".join([title.lower()] * words), **fields)


def condensed(item: LoreEntry, words: int) -> LoreEntry:
    item.condensed = " ".join(["short"] * words)
    item.condensed_from = lore_fingerprint(item.content)
    return item


# --- the fit ----------------------------------------------------------------------


def test_within_the_budget_everything_is_whole():
    standing = [condensed(entry("A", 100), 10), condensed(entry("B", 100), 10)]
    fit = fit_standing(standing, room=500, count_tokens=WORDS, least_first=["B", "A"])
    assert fit.condensed == () and fit.entries == tuple(standing) and not fit.over


def test_past_it_the_least_important_go_condensed_and_no_more_than_needed():
    a, b, c = (condensed(entry(t, 100), 10) for t in "ABC")
    least_first = [c.id, b.id, a.id]  # C matters least
    fit = fit_standing([a, b, c], room=250, count_tokens=WORDS, least_first=least_first)
    assert fit.condensed == (c.id,)  # 306 whole, 216 with C condensed: fits
    assert [e.content for e in fit.entries][:2] == [a.content, b.content]
    assert fit.entries[2].content == "short " * 9 + "short"
    fit = fit_standing([a, b, c], room=130, count_tokens=WORDS, least_first=least_first)
    assert fit.condensed == (b.id, c.id) and fit.sent == 102 + 12 + 12 and not fit.over
    # The originals are as they were.
    assert c.content.startswith("c c c") and c.condensed_now()


def test_with_too_few_condensed_versions_it_says_it_is_still_over():
    a = entry("A", 300)  # no condensed version
    b = condensed(entry("B", 100), 10)
    fit = fit_standing([a, b], room=100, count_tokens=WORDS, least_first=[a.id, b.id])
    assert fit.condensed == (b.id,) and fit.over and fit.whole == 404


def test_an_out_of_date_condensed_version_is_never_sent():
    a = condensed(entry("A", 300), 20)
    a.content += " and a change since"
    assert a.condensed_now() is None
    fit = fit_standing([a], room=10, count_tokens=WORDS, least_first=[a.id])
    assert fit.condensed == () and fit.over


def test_a_lorebook_small_enough_to_go_whole_is_never_condensed(tmp_path, story, cast):
    small = [condensed(entry(f"Entry{i}", 100, always_on=True), 10) for i in range(3)]
    session = session_with(tmp_path, story, cast, small)
    session.config.lore_limit = "tokens"
    session.config.lore_token_cap = 100
    assert session.lore_layout().mode == "whole" and session.lore_layout().condensed == ()
    assert not session.standing_fit().over


def test_switched_off_the_entries_go_whole_whatever_the_budget():
    a = condensed(entry("A", 300), 20)
    fit = fit_standing([a], room=10, count_tokens=WORDS, least_first=[a.id], use_condensed=False)
    assert fit.condensed == () and fit.over


def test_least_important_first_by_importance_then_lower_in_the_list():
    a, b, c, d = (entry(t, 5, priority=p) for t, p in zip("ABCD", (5, 0, 0, 9), strict=True))
    assert least_important_first([a, b, c, d], lambda e: e.priority) == [c.id, b.id, a.id, d.id]


def test_the_lengths_and_the_reply():
    assert not worth_condensing("a few words") and worth_condensing("w " * 200)
    # Half, with no cap (a third capped at 800 lost the longest entries'
    # detail), and room in the reply for it.
    assert target_words("w " * 3500) == 1750 and target_words("w " * 300) == 150
    assert target_words("w " * 90) == 60
    from sealedlore.engine.lore_condense import CONDENSE_MAX_TOKENS, reply_tokens

    assert reply_tokens("w " * 3500) > CONDENSE_MAX_TOKENS + 1750 * 1.4
    assert parse_condensed_lore("## The Keep\nAn empty fortress.", "The Keep") == (
        "An empty fortress."
    )
    with pytest.raises(ValueError):
        parse_condensed_lore("  ", "The Keep")


# --- a kept lorebook's --------------------------------------------------------------

BOOK = {
    "entries": {
        "0": {
            "uid": 0,
            "comment": "The Valley",
            "constant": True,
            "key": [],
            "content": "@@activate\nThe valley of {{user}} is cut off by snow all winter.",
        },
        "1": {"uid": 1, "comment": "Wolves", "key": ["wolf"], "content": "They hunt in threes."},
    }
}


def test_a_kept_lorebooks_condensed_versions_come_into_its_stories_beside_the_whole():
    raw = BOOK["entries"]["0"]
    book = StoredLorebook(
        name="Valley", book=BOOK, condensed={condensed_key(raw): "{{user}}'s valley: snowbound."}
    )
    (valley, wolves) = lore_of(book, fill=lambda t: t.replace("{{user}}", "Rowan")).entries
    assert valley.content == "The valley of Rowan is cut off by snow all winter."  # the whole
    assert valley.condensed_now() == "Rowan's valley: snowbound."
    assert wolves.condensed is None
    # A story keeps the placeholders, in the condensed version too, and fills
    # both as it sends (engine/placeholders.py).
    scenario, _notes = scenario_from(book, title="T", player="Rowan")
    kept = scenario.lore[0]
    assert kept.condensed_now() == "{{user}}'s valley: snowbound."
    from sealedlore.engine.placeholders import Names

    sent = Names(user="Rowan").entry(kept)
    assert sent.content == "The valley of Rowan is cut off by snow all winter."
    assert sent.condensed_now() == "Rowan's valley: snowbound."
    # A key is the entry's text as a story reads it: @@activate off, names not filled.
    assert condensed_key(raw) == lore_fingerprint(
        "The valley of {{user}} is cut off by snow all winter."
    )
    # An edit to the entry in Lorebooks leaves its old condensed version behind.
    edited = {"entries": {"0": {**raw, "content": "Something else."}}}
    (changed,) = lore_from_book(edited, condensed=book.condensed).entries
    assert changed.condensed is None


# --- in a story -------------------------------------------------------------------------


def session_with(tmp_path: Path, story, cast, lore, replies=()) -> StorySession:
    story.held_character_id = "char-serrik"
    config = make_config()
    config.scene_reads = "manual"
    return StorySession(
        StoryBundle(story=story, cast=cast, lore=lore),
        config,
        MockChatProvider(list(replies) or ["A passage."] * 4),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def sent(session: StorySession) -> str:
    request = session.provider.requests[-1]
    return "\n".join(part.text for message in request.messages for part in message.parts)


def test_a_story_sends_the_fewest_condensed_and_keeps_the_rest_whole(tmp_path, story, cast):
    big = [condensed(entry(f"Entry{i}", 600, always_on=True, priority=i), 30) for i in range(4)]
    session = session_with(tmp_path, story, cast, big)
    # Too big to go whole (a tenth of 20k), so chosen from, the four standing.
    session.story.defaults.context_token_budget = 20_000
    session.config.lore_limit = "tokens"
    session.config.lore_token_cap = 3_500  # four of ~1,050 tokens: over by one
    fit = session.standing_fit()
    assert fit.whole > fit.room and not fit.over
    assert fit.condensed == (big[0].id,)  # priority 0, the least important
    list(session.send(turn("I look around.")))
    text = sent(session)
    assert "short short" in text and text.count("entry3 entry3") > 0
    assert session.last_retrieval.condensed == (big[0].id,)
    # Off, all whole.
    session.story.defaults.condense_lore = False
    assert session.lore_layout().condensed == ()


def test_which_go_condensed_is_the_same_every_turn(tmp_path, story, cast):
    big = [condensed(entry(f"Entry{i}", 600, always_on=True), 30) for i in range(4)]
    session = session_with(tmp_path, story, cast, big)
    session.story.defaults.context_token_budget = 20_000
    session.config.lore_limit = "tokens"
    session.config.lore_token_cap = 2_000
    blocks = []
    for text in ("I look around.", "I say something else entirely.", "Nothing."):
        list(session.send(turn(text)))
        blocks.append(session.provider.requests[-1].messages[0].text)
    assert blocks[0] == blocks[1] == blocks[2]


def test_under_sillytaverns_rules_its_budget_and_its_order(tmp_path, story, cast):
    book = {
        "entries": {
            str(i): {
                "uid": i,
                "comment": f"Constant {i}",
                "key": [],
                "constant": True,
                "order": order,
                "content": " ".join([f"c{i}"] * 900),
            }
            for i, order in enumerate((300, 10, 200))
        }
    }
    lore = lore_from_book(book).entries
    for item in lore:
        condensed(item, 40)
    story.defaults.lore_firing = "sillytavern"
    session = session_with(tmp_path, story, cast, lore)
    session.story.defaults.context_token_budget = 6_000  # a quarter: 1,500
    assert session.standing_room() == 1_500  # three of ~680 tokens: over by one
    fit = session.standing_fit()
    assert fit.condensed == (lore[1].id,)  # order 10: read last, condensed first
    # In a private scene, the private model's budget.
    session.config.private_budget = 4_000
    assert session.standing_room(4_000) == 1_000
    assert len(session.standing_fit(budget=4_000).condensed) == 2


def test_writing_condensed_versions_keeps_the_whole_and_logs_the_calls(tmp_path, story, cast):
    long_one = entry("The Keep", 400, always_on=True)
    short_one = entry("The Gate", 20, always_on=True)
    hidden = entry("The Crown", 400, always_on=True, until_mentioned=True)
    session = session_with(
        tmp_path,
        story,
        cast,
        [long_one, short_one, hidden],
        replies=["## The Keep\nAn empty fortress."],
    )
    assert session.condense_candidates() == [long_one]  # the short one and the held-back aren't
    list(session.condense_lore(session.condense_candidates()))
    assert long_one.condensed_now() == "An empty fortress."
    assert long_one.content.startswith("the keep the keep")  # the whole, as it was
    assert len(session.provider.requests) == 1
    again = load_story_bundle(session.story.id, root=tmp_path)
    assert again.lore[0].condensed_now() == "An empty fortress."
    from sealedlore.storage.repository import read_api_log

    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds == ["lore_condense_request", "lore_condense_response"]
    # Nothing held back or hidden is ever sent.
    list(session.condense_lore([hidden]))
    assert len(session.provider.requests) == 1


def test_a_reply_no_shorter_or_cut_off_is_not_kept(tmp_path, story, cast):
    long_one = entry("The Keep", 200, always_on=True)
    session = session_with(tmp_path, story, cast, [long_one], replies=["the keep " * 300])
    said = [n.text for n in session.condense_lore([long_one])]
    assert long_one.condensed is None and "no shorter" in said[-1]


def test_condensing_is_refused_in_a_chat_and_a_private_scene(tmp_path, story, cast):
    long_one = entry("The Keep", 400, always_on=True)
    session = session_with(tmp_path, story, cast, [long_one])
    session.story.mode = "chat"
    with pytest.raises(ValueError, match="simple chat"):
        list(session.condense_lore([long_one]))
    session.story.mode = "story"
    session.enter_private(keep="memory", provider=MockChatProvider(["x"]), model="local/model")
    with pytest.raises(ValueError, match="private scene"):
        list(session.condense_lore([long_one]))


def test_the_authors_own_condensed_version_is_current_and_blank_takes_it_off(tmp_path, story, cast):
    long_one = entry("The Keep", 400, always_on=True)
    session = session_with(tmp_path, story, cast, [long_one])
    session.set_condensed_lore(long_one, "  My own.  ")
    assert long_one.condensed_now() == "My own."
    session.set_condensed_lore(long_one, "")
    assert long_one.condensed is None and long_one.condensed_from is None


def test_placeholders_are_named_only_for_an_entry_that_has_them():
    """Named in every request, they had GLM 5.3 write {{user}} 16 times into
    six condensed entries whose originals had none."""
    from sealedlore.engine.lore_condense import build_lore_condense_messages

    plain = build_lore_condense_messages("Keep", "the user stands at the gate " * 30)[0].text
    marked = build_lore_condense_messages("Keep", "{{user}} stands at the gate " * 30)[0].text
    assert "{{user}}" not in plain and "{{user}}" in marked


def test_a_placeholder_the_condenser_made_up_is_written_out(tmp_path, story, cast):
    from sealedlore.engine.lore_condense import unasked_placeholders

    assert unasked_placeholders("{{user}} and {{ char }} and {{user}}", "the user", "Serrik") == (
        "Serrik and the character and Serrik"
    )
    assert (
        unasked_placeholders("{{user}} waits", "{{user}} waits long", "Serrik") == "{{user}} waits"
    )
    keep = entry("The Keep", 400, always_on=True)
    session = session_with(
        tmp_path, story, cast, [keep], replies=["The keep, where {{user}} waits."]
    )
    list(session.condense_lore([keep]))
    assert keep.condensed_now() == "The keep, where Serrik Vaun waits."
