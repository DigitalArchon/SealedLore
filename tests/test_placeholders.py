"""{{user}} and {{char}} kept as written and filled as each request goes out
(engine/placeholders.py, engine/session_placeholders.py): whatever a model is
sent, and whatever keywords are matched against, has the names in; the
story's own text never changes. The mock provider: no network."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.placeholders import NO_CHAR, NO_USER, Names, has_placeholders
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle
from tests.test_session import make_config

SERRIK, MAELA = "char-serrik", "char-maela"


def session_with(tmp_path: Path, story, cast, lore=(), replies=()) -> StorySession:
    story.held_character_id = SERRIK
    config = make_config()
    config.scene_reads = "manual"
    return StorySession(
        StoryBundle(story=story, cast=cast, lore=list(lore)),
        config,
        MockChatProvider(list(replies) or ["A passage."] * 6),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str, held: str = SERRIK) -> TurnRequest:
    return TurnRequest(speaker_id=held, user_text=text, controlled_character_id=held)


def sent(session: StorySession) -> str:
    request = session.provider.requests[-1]
    return "\n".join(part.text for message in request.messages for part in message.parts)


def test_names_fill_every_spelling_and_nothing_else():
    names = Names(user="Rowan", char="Marcus")
    assert names.fill("{{char}} owes {{ User }} and <BOT> owes <USER>.") == (
        "Marcus owes Rowan and Marcus owes Rowan."
    )
    assert names.fill("{{char}} smiles.", char="Elena") == "Elena smiles."
    assert names.fill("{{original}} stays") == "{{original}} stays"
    assert Names().fill("{{user}} and {{char}}") == f"{NO_USER} and {NO_CHAR}"
    plain = LoreEntry(title="Wolves", content="They hunt in threes.")
    assert Names(user="Rowan").entry(plain) is plain  # nothing to fill: the same object


def test_what_is_sent_has_the_names_in_and_the_story_keeps_them(tmp_path, story, cast):
    cast[1].full_description = "{{char}} owes {{user}} a debt from the south road."
    story.world_bible = "{{user}} is known at every gate."
    story.setup.placeholders.char_id = MAELA
    lore = [LoreEntry(title="The Gate", always_on=True, content="{{char}} keeps the gate.")]
    session = session_with(tmp_path, story, cast, lore)
    list(session.send(turn("I knock.")))
    text = sent(session)
    assert "Maela Orr owes Serrik Vaun a debt" in text
    assert "Serrik Vaun is known at every gate." in text
    assert "Maela Orr keeps the gate." in text
    assert not has_placeholders(text)
    # The story's own text is as written.
    assert session.cast[1].full_description.startswith("{{char}} owes {{user}}")
    assert session.story.world_bible.startswith("{{user}}")
    assert session.bundle.lore[0].content == "{{char}} keeps the gate."


def test_user_stays_the_storys_player_when_the_author_plays_someone_else(tmp_path, story, cast):
    cast[1].full_description = "{{char}} owes {{user}} a debt."
    session = session_with(tmp_path, story, cast)
    list(session.begin(SERRIK))
    assert session.story.setup.placeholders.user_id == SERRIK  # set when it begins
    session.hold(MAELA)
    list(session.send(turn("I look at Serrik.", held=MAELA)))
    # Maela's own card still names Serrik as the one she owes, not herself.
    assert "Maela Orr owes Serrik Vaun a debt." in sent(session)


def test_char_in_an_entry_that_came_with_a_card_is_that_card(tmp_path, story, cast):
    story.setup.placeholders.char_id = MAELA
    lore = [
        LoreEntry(
            title="Hers", always_on=True, content="{{char}} picks locks.", of_character=MAELA
        ),
        LoreEntry(
            title="His", always_on=True, content="{{char}} counts coins.", of_character="char-idris"
        ),
        LoreEntry(title="The story's", always_on=True, content="{{char}} rules the town."),
    ]
    session = session_with(tmp_path, story, cast, lore)
    list(session.send(turn("I wait.")))
    text = sent(session)
    assert "Maela Orr picks locks." in text
    assert "Captain Idris counts coins." in text
    assert "Maela Orr rules the town." in text


def test_a_keyword_with_a_placeholder_matches_the_name(tmp_path, story, cast):
    big = " ".join(["filler"] * 3000)  # too big to go whole: chosen by keyword
    lore = [
        LoreEntry(
            title="Serrik's debt", keywords=["{{user}}'s debt"], content="It is owed. " + big
        ),
        LoreEntry(title="Other", keywords=["unrelated"], content="Elsewhere. " + big),
    ]
    session = session_with(tmp_path, story, cast, lore)
    session.story.defaults.context_token_budget = 20_000
    assert session.lore_layout().mode != "whole"
    list(session.send(turn("I think about Serrik Vaun's debt.")))
    picked = [i.entry.title for i in session.last_retrieval.injected]
    assert "Serrik's debt" in picked and "Other" not in picked


def test_a_condensed_version_stays_current_once_the_names_are_in():
    entry = LoreEntry(title="The Valley", always_on=True, content="{{user}}'s valley.")
    entry.condensed = "{{user}}: snowbound."
    entry.condensed_from = lore_fingerprint(entry.content)
    filled = Names(user="Rowan").entry(entry)
    assert filled.content == "Rowan's valley." and filled.condensed_now() == "Rowan: snowbound."
    card = Character(
        name="Elena", full_description="{{char}} heals.", condensed="{{char}}: a medic."
    )
    from sealedlore.engine.card_condense import fingerprint

    card.condensed_from = fingerprint(card)
    copy = Names().character(card)
    assert copy.full_description == "Elena heals." and copy.condensed_from == fingerprint(copy)


def test_a_chat_is_sent_as_written(tmp_path, story, cast):
    story.mode = "chat"
    story.chat_prompt = "You are {{char}}."
    session = session_with(tmp_path, story, cast)
    assert session.assembly_options().names is None


def test_the_review_and_follow_up_are_told_whom_they_name_only_when_used(story, cast):
    from sealedlore.engine.authoring import placeholders_view

    bundle = StoryBundle(story=story, cast=cast)
    assert placeholders_view(bundle) == {}  # every other request as it was
    cast[1].full_description = "{{char}} owes {{user}}."
    story.setup.placeholders.user_id = SERRIK
    story.setup.placeholders.char_name = "The Sundering"
    assert placeholders_view(bundle) == {
        "placeholders": {"{{user}}": "Serrik Vaun", "{{char}}": "The Sundering"}
    }


def test_a_person_taken_out_of_an_entry_keeps_whom_its_char_meant(tmp_path, story, cast):
    from sealedlore.engine.lore_people import FoundPerson

    text = "Lily is {{char}}'s sister, and owes {{user}} her life."
    entry = LoreEntry(title="Lily", content=text, of_character=MAELA)
    session = session_with(tmp_path, story, cast, [entry])
    lily = FoundPerson(
        name="Lily",
        aliases=[],
        summary="{{char}}'s sister.",
        text=text,
        entry_id=entry.id,
        entry_title="Lily",
        start=0,
        end=len(text),
        always_on=False,
        whole_entry=True,
    )
    took = session.take_lore_people([(lily, "supporting")])
    (card,) = took.supporting
    # Her own card's {{char}} would be Lily herself: written out as Maela.
    assert card.full_description == "Lily is Maela Orr's sister, and owes {{user}} her life."
    assert card.summary == "Maela Orr's sister."
