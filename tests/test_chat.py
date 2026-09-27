"""Simple chat (engine/chat.py): a plain conversation with the story model.

The author's system prompt, the conversation as written, and archival: no
rules, cast, lore, scene, plot or side model. Kept-in-full messages ride
after the summary of their part."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.chat import (
    KEPT_NOTE,
    SECTION_CHAT_PROMPT,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.image import ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    scenario_from_bundle,
)

PROMPT = "You are a patient maths tutor. Answer briefly."
CHAT_SUMMARISER = DEFAULT_TEXTS.fill("chat.summariser", kept=KEPT_NOTE)
STORY_MARKERS = ("REMEMBER", "TURN DIRECTIVES", "THIS SCENE", "# CAST", "[", "engine of a")


def make_config(**changes) -> Config:
    return Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        **changes,
    )


def chat_session(
    tmp_path: Path, replies: list[str], *, prompt: str = PROMPT, config: Config | None = None
) -> tuple[StorySession, MockChatProvider]:
    story = Story(title="Tutor", mode="chat", chat_prompt=prompt)
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    # A chat has none of these, but a stray one must still never reach it.
    bundle = StoryBundle(
        story=story,
        cast=[Character(id="c1", name="Nils Carrow")],
        lore=[LoreEntry(title="Thorn Gate", content="Signed in blood.", keywords=["gate"])],
    )
    save_story_bundle(bundle, root=tmp_path)
    provider = MockChatProvider(replies)
    session = StorySession(
        bundle,
        config or make_config(),
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    return session, provider


def say(session: StorySession, text: str) -> None:
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text=text)))


def texts(request) -> list[tuple[str, str]]:
    return [(message.role, message.text) for message in request.messages]


def test_a_chat_prompt_is_the_system_prompt_then_the_conversation_as_written(tmp_path):
    session, provider = chat_session(tmp_path, ["Two.", "Four."])
    say(session, "What is 1 + 1? Go on, the gate is open.")
    say(session, "And 2 + 2?")
    assert all(node.meta.tee is None for node in session.path()), "no mark off a TEE"
    sent = texts(provider.requests[-1])
    assert sent == [
        ("system", PROMPT),
        ("user", "What is 1 + 1? Go on, the gate is open."),
        ("assistant", "Two."),
        ("user", "And 2 + 2?"),
    ]
    for _role, text in sent:
        assert not any(marker in text for marker in STORY_MARKERS), text
    names = [section.name for section in session.last_prompt.sections]
    assert names[0] == SECTION_CHAT_PROMPT
    # The caching invariant holds for chats too.
    assert provider.requests[0].messages[0].text == provider.requests[1].messages[0].text


def test_a_chat_without_a_system_prompt_sends_no_system_message(tmp_path):
    session, provider = chat_session(tmp_path, ["Hi."], prompt="   ")
    say(session, "Hello")
    assert texts(provider.requests[-1]) == [("user", "Hello")]


def test_nothing_but_turns_and_summaries_reach_any_model(tmp_path):
    """Scene reads, the ledger, character scans and lore picks all on, a
    budget small enough to archive: a chat still calls nothing else."""
    config = make_config(
        scene_reads="every_turn",
        story_ledger=True,
        suggest_characters=True,
        lore_selector="picker",
        archive_chunk_turns=2,
    )
    replies = []
    for i in range(12):
        replies += [f"Answer number {i}, " + "and a little more. " * 20]
    replies.insert(8, "They discussed the first sums.")  # the summariser's turn, if due
    session, provider = chat_session(tmp_path, replies * 2, config=config)
    session.story.defaults.context_token_budget = 900
    for i in range(8):
        say(session, f"Question {i}: " + "tell me more. " * 10)
    kinds = []
    for request in provider.requests:
        system = request.messages[0].text if request.messages[0].role == "system" else ""
        kinds.append(
            "summary"
            if system == CHAT_SUMMARISER
            else "turn"
            if system.startswith(PROMPT)
            else system
        )
    assert set(kinds) <= {"turn", "summary"}, kinds
    assert kinds.count("turn") == 8 and "summary" in kinds
    assert session.bundle.summaries, "nothing was archived"


def test_the_first_message_is_kept_in_full_after_its_part_is_summarised(tmp_path):
    session, provider = chat_session(tmp_path, ["A.", "B.", "C.", "Summary of the start."])
    say(session, "My name is Ada and I study topology.")
    say(session, "What is a torus?")
    say(session, "Thanks.")
    first = session.path()[0]
    assert first.meta.keep_full and not session.path()[2].meta.keep_full

    provider.responses = ["They met; Ada asked about a torus."]
    session.archive(session.path()[:4])
    summariser = provider.requests[-1]
    assert summariser.messages[0].text == CHAT_SUMMARISER
    chunk = summariser.messages[-1].text
    assert f"User {KEPT_NOTE}: My name is Ada" in chunk and "Assistant: A." in chunk

    provider.responses = ["D."]
    say(session, "Remind me my name?")
    system = provider.requests[-1].messages[0].text
    assert system.startswith(PROMPT)
    part = system.index("They met; Ada asked about a torus.")
    kept = system.index("Kept in full — User:\nMy name is Ada and I study topology.")
    assert part < kept
    assert "What is a torus?" not in system  # summarised, not kept

    session.set_keep_full(first.id, False)
    provider.responses = ["E."]
    say(session, "Again?")
    assert "My name is Ada" not in provider.requests[-1].messages[0].text


def test_what_only_a_story_has_is_refused(tmp_path):
    session, _provider = chat_session(tmp_path, ["Hi."])
    with pytest.raises(ValueError, match="simple chat"):
        list(session.begin(None))
    with pytest.raises(ValueError, match="simple chat"):
        list(session.ask("Why?"))
    with pytest.raises(ValueError, match="simple chat"):
        session.review_settings("too long")
    with pytest.raises(ValueError, match="simple chat"):
        session.enter_private(keep="memory", provider=MockChatProvider(), model="m")


def test_a_chat_survives_a_scenario_and_a_new_playthrough(tmp_path):
    session, _provider = chat_session(tmp_path, ["Hi."])
    session.story.reference_images.append(ImageRef(file="images/refs/r1.png", caption="me"))
    scenario = scenario_from_bundle(session.bundle, {})
    back = bundle_from_scenario(scenario)
    assert (back.story.mode, back.story.chat_prompt) == ("chat", PROMPT)
    assert [r.caption for r in back.story.reference_images] == ["me"]
    again = fresh_playthrough(session.bundle, "Tutor again")
    assert again.story.chat and again.story.chat_prompt == PROMPT


def test_a_tee_chat_keeps_its_summaries_on_its_own_model(tmp_path):
    session, _provider = chat_session(tmp_path, ["Hi."])
    session.story.defaults.summarization_model = "cheap/summariser"
    assert session.summarization_model == "cheap/summariser"
    session.story.defaults.main_model = "TEE/deepseek-v3"
    assert session.summarization_model == "TEE/deepseek-v3"


# --- TEE and memory-only chats ------------------------------------------------------


class FakeTee:
    def __init__(self, signed: bool) -> None:
        self.signed = signed
        self.asked: list[str | None] = []

    def verify_reply(self, response_id):
        self.asked.append(response_id)
        return self.signed


def test_a_tee_chats_replies_are_marked_by_their_signature(tmp_path):
    session, _provider = chat_session(tmp_path, ["Signed.", "Not signed."])
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.private_tee = FakeTee(signed=True)
    say(session, "Hello")
    assert session.path()[-1].meta.tee == "verified"
    assert session.path()[-2].meta.tee == "attested", "the author's message, sent to the enclave"
    session.private_tee = FakeTee(signed=False)
    say(session, "Again")
    assert session.path()[-1].meta.tee == "failed"


def test_a_tee_chat_never_moves_to_a_model_that_isnt(tmp_path):
    session, _provider = chat_session(tmp_path, ["Hi."])
    session.check_chat_model("anthropic/claude-sonnet-4.6")  # not a TEE chat: anything goes
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.check_chat_model("TEE/deepseek-v3")
    with pytest.raises(ValueError, match="TEE"):
        session.check_chat_model("anthropic/claude-sonnet-4.6")


def listing(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_memory_only_chat_leaves_no_trace_on_disk(tmp_path):
    """Played through archival and a merge's worth of turns: the data folder
    is byte for byte as it was, and the cost is still known."""
    story = Story(title="Incognito", mode="chat", chat_prompt=PROMPT, chat_keep="memory")
    story.defaults.main_model = "TEE/gemma-3-27b"
    story.defaults.context_token_budget = 900
    before = listing(tmp_path)
    replies = [f"Reply {i}: " + "some words here. " * 20 for i in range(40)]
    provider = MockChatProvider(replies)
    session = StorySession(
        StoryBundle(story=story),
        make_config(archive_chunk_turns=2),
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    session.private_tee = FakeTee(signed=True)
    for i in range(8):
        say(session, f"Secret {i}: " + "more words. " * 10)
    assert session.bundle.summaries, "nothing was archived"
    assert listing(tmp_path) == before
    kinds = {entry["kind"] for entry in session.memory_log}
    assert {"request", "response", "summary_request"} <= kinds
    # Every call stayed on the chat's own model.
    assert {request.model for request in provider.requests} == {"TEE/gemma-3-27b"}


def test_a_chat_tail_ends_every_request_and_never_enters_the_history(tmp_path):
    session, provider = chat_session(tmp_path, ["Bonjour.", "Ça va."])
    session.story.chat_tail = "Reply in French."
    say(session, "Hello")
    say(session, "How are you?")
    sent = texts(provider.requests[-1])
    assert sent[-1] == ("user", "How are you?\n\nReply in French.")
    assert sent[1] == ("user", "Hello"), "earlier messages are as written"
    assert all("Reply in French" not in node.content for node in session.path())
    names = [section.name for section in session.last_prompt.sections]
    assert names[-1] == "tail.chat_tail"

    session.story.chat_tail = "   "
    provider.responses = ["Fine."]
    say(session, "Again")
    assert texts(provider.requests[-1])[-1] == ("user", "Again"), "blank sends nothing"


def test_a_chat_tail_travels_with_the_chat_but_not_to_the_picture_writer(tmp_path):
    session, provider = chat_session(tmp_path, ["Hi."])
    session.story.chat_tail = "Stay terse."
    back = bundle_from_scenario(scenario_from_bundle(session.bundle, {}))
    assert back.story.chat_tail == "Stay terse."
    assert fresh_playthrough(session.bundle, "Again").story.chat_tail == "Stay terse."

    say(session, "Hello")
    provider.responses = ["not json: only the request matters here"]
    with pytest.raises(ValueError):
        session.write_image_prompt("the cat", anchor_id=None, max_refs=0, fixed=None, model=None)
    assert "Stay terse." not in provider.requests[-1].messages[-1].text
