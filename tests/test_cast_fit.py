"""Which cast cards go whole, condensed or short (engine/cast_fit.py), and
the cast's room (Config.cast_cap). Pure: no session, no network."""

from __future__ import annotations

from sealedlore.engine.card_condense import (
    CONDENSE_LEAST_WORDS,
    CONDENSE_MOST_WORDS,
    build_condense_messages,
    fingerprint,
    parse_condensed,
    target_words,
)
from sealedlore.engine.cast_fit import condense_order, fit_cast, has_condensed
from sealedlore.engine.rules import render_cast_block, render_cast_sheet
from sealedlore.engine.tokens import fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config

count = fallback_counter


def card(cid: str, words: int, **fields) -> Character:
    return Character(
        id=cid,
        name=fields.pop("name", f"Person {cid}"),
        summary=f"Person {cid}, of Doomsville.",
        full_description=" ".join(["scavenger"] * words),
        **fields,
    )


def condensed(character: Character, text: str = "Condensed.") -> Character:
    character.condensed = text
    character.condensed_from = fingerprint(character)
    return character


def test_the_room_is_a_share_of_the_budget_unless_fixed():
    config = Config()
    assert config.cast_limit == "share"
    assert config.cast_cap(60_000) == 9_000
    assert config.cast_cap(16_000) == 2_400
    config.cast_limit = "tokens"
    config.cast_token_cap = 8_000
    assert config.cast_cap(16_000) == config.cast_cap(200_000) == 8_000


def test_a_cast_that_fits_goes_whole():
    cast = [card("a", 50), card("b", 80)]
    fit = fit_cast(cast, 10_000, count)
    assert not fit.over and set(fit.forms.values()) == {"whole"}
    assert fit.sent_tokens == fit.whole_tokens == count(render_cast_block(cast))


def test_the_largest_card_gives_way_first_and_only_as_many_as_needed():
    small, big, bigger = card("small", 60), card("big", 900), card("bigger", 1_500)
    for character in (small, big, bigger):
        condensed(character)
    cast = [small, big, bigger]
    whole = count(render_cast_block(cast))
    fit = fit_cast(cast, whole - 200, count)
    assert fit.forms == {"small": "whole", "big": "whole", "bigger": "condensed"}
    assert not fit.still_over


def test_who_the_storyteller_writes_decides_before_size():
    # The author's own (never voiced) give way before the storyteller's own.
    theirs = card("theirs", 1_500, is_player_available=False)
    played = card("played", 700)
    authors = card("authors", 600, author_only=True)
    cast = [theirs, played, authors]
    assert [c.id for c in condense_order(cast, count)] == ["authors", "played", "theirs"]


def test_a_card_kept_whole_never_gives_way():
    main = condensed(card("main", 2_000, keep_whole=True))
    other = condensed(card("other", 100))
    fit = fit_cast([main, other], 300, count)
    assert fit.forms["main"] == "whole"
    assert main not in condense_order([main, other], count)
    assert fit.still_over


def test_a_card_condensing_would_not_shrink_is_passed_over():
    tiny = card("tiny", 10)
    big = card("big", 1_200)
    fit = fit_cast([tiny, big], count(render_cast_block([tiny, big])) - 50, count)
    # 150 words at the least is more than ten: only the big one waits.
    assert fit.waiting == ("big",)
    assert fit.forms["tiny"] == "whole"


def test_a_condensed_text_from_before_an_edit_is_not_used():
    character = condensed(card("a", 1_000), "Old condensed text.")
    assert has_condensed(character)
    character.full_description += " Lost an eye in the winter."
    assert not has_condensed(character)
    fit = fit_cast([character], 600, count)
    assert fit.waiting == ("a",)
    assert "Old condensed text." not in render_cast_block([character], fit.forms)


def test_waiting_cards_go_whole_in_turns_and_short_in_a_private_scene():
    character = card("a", 1_000, voice_notes="Clipped.")
    public = fit_cast([character], 600, count)
    private = fit_cast([character], 600, count, waiting_form="short")
    assert public.waiting == private.waiting == ("a",)
    assert public.forms["a"] == "whole" and private.forms["a"] == "short"
    assert "scavenger" not in render_cast_block([character], private.forms)
    assert "Voice: Clipped." in render_cast_block([character], private.forms)


def test_past_its_room_even_condensed_a_card_goes_short_where_that_is_smaller():
    # One with long example messages is larger short than condensed.
    talker = condensed(
        card("talker", 1_000, voice_notes=" ".join(["Hello there."] * 400)), "Talks a lot."
    )
    quiet = condensed(card("quiet", 1_100), "Says little, and that " + "very " * 60 + "slowly.")
    fit = fit_cast([talker, quiet], 40, count)
    assert fit.forms == {"talker": "condensed", "quiet": "short"}
    assert fit.still_over


def test_a_condensed_sheet_has_its_text_instead_of_description_and_voice():
    character = condensed(
        card("a", 300, voice_notes="Example lines.", personality="Wary."), "Wary scout, few words."
    )
    sheet = render_cast_sheet(character, "condensed")
    assert "Wary scout, few words." in sheet
    assert "scavenger" not in sheet and "Example lines." not in sheet
    assert "Person a, of Doomsville." in sheet  # the summary stays
    whole = render_cast_sheet(character)
    assert "Wary scout, few words." not in whole and "Personality: Wary." in whole


def test_the_condensed_length_is_a_third_within_bounds():
    assert target_words(card("a", 60)) == CONDENSE_LEAST_WORDS
    assert target_words(card("b", 900)) == 300
    assert target_words(card("c", 4_000)) == CONDENSE_MOST_WORDS


def test_the_condensing_request_names_the_author_and_carries_the_card():
    character = card("a", 100, voice_notes="<START> Hello.", aliases=["Red"])
    messages = build_condense_messages(character, player="John Jacobs", words=200)
    system, user = messages[0].text, messages[1].text
    assert "the author plays John Jacobs" in system and "at most 200 words" in system
    assert "Also known as: Red" in user and "<START> Hello." in user and "scavenger" in user
    unnamed = build_condense_messages(character)[0].text
    assert "the author plays another character" in unnamed


def test_a_reply_headed_with_the_name_loses_the_heading_and_an_empty_one_is_refused():
    character = card("a", 10, name="Jane Smith")
    assert parse_condensed("**Jane Smith**\nA scout.", character) == "A scout."
    try:
        parse_condensed("   ", character)
    except ValueError:
        pass
    else:
        raise AssertionError("an empty reply must be refused")


def test_the_fit_is_the_same_whatever_else_changes():
    # A function of the cast and its room alone: the cached system block.
    cast = [condensed(card("a", 1_500)), card("b", 1_200)]
    first = fit_cast(cast, 1_000, count)
    second = fit_cast(list(cast), 1_000, count)
    assert first == second


def test_a_supporting_card_carries_its_description_whole_or_condensed():
    from sealedlore.engine.characters import (
        SUPPORTING_WHOLE_WORDS,
        needs_condensing,
        render_supporting_block,
    )

    plain = Character(id="s0", name="Old Tobin", summary="The ferryman.", voice_notes="Gruff.")
    brief = card("s1", 40, voice_notes="Quiet.")
    long = card("s2", SUPPORTING_WHOLE_WORDS + 200, voice_notes="Hello there.")
    block = render_supporting_block([plain, brief, long])
    assert "The ferryman." in block and "Voice: Gruff." in block
    # A short description goes whole, beside the voice.
    assert block.count("scavenger") == 40 and "Voice: Quiet." in block
    # A long one waits for its condensed text, and meanwhile isn't sent.
    assert needs_condensing(long) and not needs_condensing(brief)
    assert "Voice: Hello there." in block
    condensed(long, "Tobin's rival: sly, owes everyone.")
    block = render_supporting_block([long])
    assert "Tobin's rival: sly, owes everyone." in block
    assert "scavenger" not in block and "Hello there." not in block
    assert not needs_condensing(long)


def test_a_summary_that_only_repeats_the_descriptions_opening_is_said_once():
    """A plot file's or a premise's card has its description's first sentence
    for a summary: sent whole, the sheet said that sentence twice."""
    repeated = Character(
        id="j",
        name="John",
        summary="A survivor, ex-military.",
        full_description="A survivor, ex-military. Stoic on the outside.",
    )
    sheet = render_cast_sheet(repeated)
    assert sheet.count("A survivor, ex-military.") == 1
    assert "Stoic on the outside." in sheet
    # Short, the summary is all there is of them; condensed, it stays as before.
    assert "A survivor, ex-military." in render_cast_sheet(repeated, "short")
    own = Character(
        id="e", name="Elena", summary="The town's medic.", full_description="Tired, kind, blunt."
    )
    assert "The town's medic." in render_cast_sheet(own)
    assert "Tired, kind, blunt." in render_cast_sheet(own)


def test_a_supporting_card_says_a_repeated_summary_once_too():
    from sealedlore.engine.characters import render_supporting_block

    card_ = Character(
        id="m",
        name="Marta",
        summary="A woman of fifty with a hatchet",
        full_description="A woman of fifty with a hatchet. She has been here eleven days.",
    )
    block = render_supporting_block([card_])
    assert block.count("A woman of fifty with a hatchet") == 1
    assert "eleven days" in block
