"""Simple chat (engine/chat.py): a plain conversation with the story model.

The author's system prompt, the conversation as written, and archival: no
rules, cast, lore, scene, plot or side model. Kept-in-full messages ride
after the summary of their part."""

from __future__ import annotations

import json
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
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.models.story import Story
from sealedlore.providers.base import ProviderError, TextDelta
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


# Found in a dumped payload (its newlines escaped): the opening words.


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


# --- a private part of a chat ---------------------------------------------------------

MARKER = "ZXQ-PRIVATE-MARKER"


def enter(session: StorySession, keep: str = "memory") -> MockChatProvider:
    private = MockChatProvider([f"A quiet answer that holds {MARKER} close."])
    session.enter_private(keep=keep, provider=private, model="local/model")
    return private


def test_a_chats_private_part_can_end_with_nothing_carried_over(tmp_path):
    session, main = chat_session(tmp_path, ["Noted."])
    say(session, "What is a torus?")
    leaf = session.story.active_leaf_id
    enter(session)
    say(session, f"Between us: {MARKER}.")
    session.discard_private()
    assert session.story.active_leaf_id == leaf and not session.in_private
    say(session, "And a sphere?")
    assert MARKER not in str(main.payloads[-1])
    assert len(session.path()) == 4


def test_a_long_private_part_condenses_itself_with_a_chats_wording(tmp_path):
    config = make_config(private_budget=400)
    session, _main = chat_session(tmp_path, ["Noted."], config=config)
    say(session, "What is a torus?")
    private = enter(session, "disk")
    private.responses = ["A reply. " * 30]
    for i in range(3):
        say(session, f"Private question {i}. " + "Detail. " * 30)
    private.responses = ["They went over three private questions in turn."]
    say(session, "One more.")
    span = session.open_span
    assert span.scene_summary == "They went over three private questions in turn."
    condense = next(
        request
        for request in private.requests
        if request.messages[0].text.startswith("You keep the running summary")
    )
    assert "conversation" in condense.messages[0].text and "story" not in condense.messages[0].text
    assert "User: Private question 0" in condense.messages[1].text
    # The condensed part stands where its messages stood, under a chat's heading.
    assert "Earlier in this private part of the conversation (condensed):" in str(
        private.payloads[-1]
    )


def test_a_tee_chat_has_no_private_part(tmp_path):
    """It is private throughout: there is no other model to keep a part from."""
    session, _main = chat_session(tmp_path, ["Hi."])
    session.story.defaults.main_model = "TEE/deepseek-v3"
    with pytest.raises(ValueError, match="private already"):
        session.enter_private(keep="memory", provider=MockChatProvider(), model="m")
    assert not session.in_private


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
    def __init__(self, signed: bool = False, *, unchecked: bool = False) -> None:
        self.signed = signed
        # As TeeClient raises when no record can be read (Chutes signs none).
        self.unchecked = unchecked
        self.asked: list[str | None] = []

    def check_reply(self, response_id, **_record):
        self.asked.append(response_id)
        if self.unchecked:
            raise ProviderError("this model's provider signs no replies")
        return "verified" if self.signed else "failed"


def test_a_tee_chats_replies_are_marked_by_their_signature(tmp_path):
    session, _provider = chat_session(tmp_path, ["Signed.", "Not signed."])
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.private_tee = FakeTee(signed=True)
    say(session, "Hello")
    assert session.path()[-1].meta.tee == "verified"
    # The reply's record, signed by the attested key, shows the enclave had it.
    assert session.path()[-2].meta.tee == "received", "the author's message reached the enclave"
    session.private_tee = FakeTee(signed=False)
    say(session, "Again")
    assert session.path()[-1].meta.tee == "failed"
    # Nothing shows this one reached it: said so, never "received".
    assert session.path()[-2].meta.tee == "unproven"


def test_a_tee_chats_message_is_received_only_as_far_as_a_reply_shows(tmp_path):
    """The author, Nov 11 2026: "always make clear to the user what is actually
    happening". A reply with no signed record (Chutes signs none; a model may be
    served elsewhere) leaves the author's message unproven; once one take's
    record shows it received, a later take that shows nothing doesn't take that
    back: the same text was received."""
    session, _provider = chat_session(tmp_path, ["Unsigned.", "Signed.", "Unsigned again."])
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.private_tee = FakeTee(unchecked=True)
    say(session, "Hello")
    mine, reply = session.path()[-2:]
    assert (mine.meta.tee, reply.meta.tee) == ("unproven", "unchecked")
    session.private_tee = FakeTee(signed=True)
    list(session.regenerate())
    assert (mine.meta.tee, session.path()[-1].meta.tee) == ("received", "verified")
    session.private_tee = FakeTee(unchecked=True)
    list(session.regenerate())
    assert (mine.meta.tee, session.path()[-1].meta.tee) == ("received", "unchecked")


class BreaksAfterText(MockChatProvider):
    """Some text, then the stream fails: what came is kept, and was never
    checked (a relay could send invented text, then an error)."""

    def stream(self, request):
        self.requests.append(request)
        yield TextDelta(text=" Text the relay made up.")
        raise ProviderError("the stream broke")


def tee_chat(tmp_path, replies, *, signed: bool):
    session, _provider = chat_session(tmp_path, replies)
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.private_tee = FakeTee(signed=signed)
    say(session, "Hello")
    return session, session.path()[-1]


def test_a_continued_tee_reply_is_only_as_proven_as_its_weaker_part(tmp_path):
    session, passage = tee_chat(tmp_path, ["A reply.", " More of it."], signed=False)
    assert passage.meta.tee == "failed"
    session.private_tee = FakeTee(signed=True)
    list(session.continue_passage())
    assert passage.content.endswith("More of it.")
    assert passage.meta.tee == "failed", "a verified rest doesn't make the first part verified"


def test_a_tee_reply_carried_on_with_text_never_checked_is_unchecked(tmp_path):
    session, passage = tee_chat(tmp_path, ["A genuine reply."], signed=True)
    assert passage.meta.tee == "verified"
    session.provider = BreaksAfterText()
    with pytest.raises(ProviderError):
        list(session.continue_passage())
    assert passage.content.endswith("made up.") and passage.meta.tee == "unchecked"


def test_an_edited_tee_message_loses_its_mark(tmp_path):
    session, reply = tee_chat(tmp_path, ["A genuine reply."], signed=True)
    turn = session.path()[-2]
    session.edit_node(reply.id, reply.content)
    assert reply.meta.tee == "verified", "saved unchanged, it is still the text that came"
    session.edit_node(reply.id, "Written by hand.")
    session.edit_node(turn.id, "Hello, said otherwise.")
    assert reply.meta.tee is None and turn.meta.tee is None


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


def test_a_memory_only_chat_on_any_model_leaves_no_trace_on_disk(tmp_path):
    """Memory only was a TEE chat's alone; the author opened it to every
    model (Sept 2026). A plain model's chat, through archival, a merge's worth
    of turns and a private part, leaves the data folder as it was, and the
    config learns nothing from it."""
    config = make_config(archive_chunk_turns=2)
    config_before = config.model_dump_json()
    story = Story(title="Incognito", mode="chat", chat_prompt=PROMPT, chat_keep="memory")
    story.defaults.main_model = "z-ai/glm-5.3"
    story.defaults.context_token_budget = 900
    before = listing(tmp_path)
    provider = MockChatProvider([f"Reply {i}: " + "some words here. " * 20 for i in range(40)])
    session = StorySession(
        StoryBundle(story=story),
        config,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    assert session.memory_only and not session.tee_chat
    for i in range(6):
        say(session, f"Secret {i}: " + "more words. " * 10)
    private = enter(session, "memory")
    say(session, f"Between us: {MARKER}.")
    private.responses = ["They agreed on something."]
    list(session.close_private(session.summarise_private()))
    for i in range(3):
        say(session, f"After {i}: " + "more words. " * 10)
    assert session.bundle.summaries, "nothing was archived"
    assert listing(tmp_path) == before
    assert config.model_dump_json() == config_before
    assert {"request", "response", "summary_request"} <= {e["kind"] for e in session.memory_log}


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
    """A chat's picture prompt is written on the chat as its model reads it,
    without the tail; the map section it once carried is gone (the map mode
    archived, Oct 24 2026: the call used a method that no longer existed)."""
    session, provider = chat_session(tmp_path, ["Hi."])
    session.story.chat_tail = "Stay terse."
    back = bundle_from_scenario(scenario_from_bundle(session.bundle, {}))
    assert back.story.chat_tail == "Stay terse."
    again = fresh_playthrough(session.bundle, "Again").story
    assert again.chat_tail == "Stay terse."

    say(session, "Hello")
    provider.responses = ["not json: only the request matters here"]
    with pytest.raises(ValueError):
        session.write_image_prompt("the cat", anchor_id=None, max_refs=0, fixed=None, model=None)
    assert "Stay terse." not in provider.requests[-1].messages[-1].text
    assert "Hello" in "".join(m.text for m in provider.requests[-1].messages)


def test_a_chats_markdown_export_names_the_user_as_the_chat_does(tmp_path):
    """The review of Oct 20 2026: every one of the author's messages was
    "**Unknown:**" (the name came from the cast, and a chat has none)."""
    from sealedlore.engine.export import markdown_user_name, render_markdown

    session, _provider = chat_session(tmp_path, ["Two.", "Four."])
    say(session, "What is 1 + 1?")
    say(session, "And 2 + 2?")
    story, cast = session.story, session.cast

    def exported() -> str:
        name = markdown_user_name(story, cast)
        return render_markdown(story.title, session.path(), cast, user_name=name)

    out = exported()
    assert "**You:** What is 1 + 1?" in out and "**You:** And 2 + 2?" in out
    assert "Unknown" not in out


def test_a_chat_is_never_sent_the_storys_stand_in_first_message(tmp_path):
    """A chat whose oldest kept message is the assistant's (the one before it
    cut for room, or summarised) gets a stand-in user message of its own,
    never "[Begin the story.]" (a maths tutor was sent it)."""
    story_line = DEFAULT_TEXTS["storyteller.opening_turn"]
    chat_line = DEFAULT_TEXTS["chat.opening_turn"]
    assert chat_line != story_line and "story" not in chat_line.lower()
    # Long questions and one-word replies: walking back from the newest, the
    # budget runs out on a question, so the window begins with a reply.
    question = "tell me more. " * 60

    def played(root, config, budget, replies):
        session, provider = chat_session(root, replies, config=config)
        session.story.defaults.context_token_budget = budget
        for i in range(6):
            say(session, f"Question {i}: {question}")
        return session, texts(provider.requests[-1])

    # Cut for room: auto-archive off, so the window only ever trims.
    session, sent = played(tmp_path / "cut", make_config(auto_archive=False), 700, ["ok."] * 20)
    assert session.last_prompt.excluded_node_ids, "the budget cut something"
    assert sent[0] == ("system", PROMPT)
    assert sent[1] == ("user", chat_line) and sent[2] == ("assistant", "ok.")
    assert all(story_line not in text for _role, text in sent)
    # Summarised, and still cut: a chunk is only ever four whole exchanges,
    # so between archivals the window grows past the budget and the oldest
    # messages fall out again, right after the summaries block. (The first
    # message is short: kept in full, it rides in that block.)
    replies = ["ok."] * 30
    replies.insert(8, "They discussed the first sums.")
    session, provider = chat_session(
        tmp_path / "summarised", replies, config=make_config(archive_chunk_turns=4)
    )
    session.story.defaults.context_token_budget = 700
    say(session, "Hi.")
    for i in range(1, 10):
        say(session, f"Question {i}: {question}")
    assert session.bundle.summaries, "something was summarised"
    requests = [texts(request) for request in provider.requests]
    assert all(story_line not in text for sent in requests for _role, text in sent)
    assert any(
        "EARLIER IN THIS CONVERSATION" in sent[0][1]
        and sent[1] == ("user", chat_line)
        and sent[2] == ("assistant", "ok.")
        for sent in requests
    ), "no request was both summarised and cut after the summary"


def test_a_private_part_s_picture_prompt_is_written_at_the_private_budget(tmp_path: Path):
    """The picture writer in a chat's private part had the main budget, so
    the private model was sent more of the chat than its own turns (44
    messages against 30): a story's scene already used the private budget
    (the mode audit, Oct 29 2026)."""
    long = "word " * 900
    config = make_config(auto_archive=False)
    session, _provider = chat_session(tmp_path, [long] * 40, config=config)
    # The mock's usage would teach the counts between the two prompts.
    session.learn_corrections = False
    for i in range(20):
        say(session, f"Question {i}: " + long)
    private = MockChatProvider([long, '{"prompt": "a cat", "references": []}'])
    session.enter_private(keep="disk", provider=private, model="private-model")
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="A private thing.")))
    assert session.last_prompt.excluded_node_ids, "the private budget left messages out"
    session.write_image_prompt("a cat")
    turn, picture = private.requests[0], private.requests[-1]
    assert len(picture.messages) <= len(turn.messages) + 2


def test_a_picture_from_a_private_part_offers_its_prompt_to_that_part_alone(tmp_path: Path):
    """A picture made in a chat's private part is offered to the picture
    writer afterwards, but the start of its prompt (written from the part)
    went with it as its caption: the chat's own model read the part's words.
    It goes now only to the part's own model, while the part is open; the
    picture itself is still offered."""
    session, main = chat_session(tmp_path, ["Public reply."])
    say(session, "Hello")
    private = enter(session, keep="disk")
    say(session, f"I tell you {MARKER}")
    span = session.open_span
    session.pictures.write("images/x.png", b"\x89PNG\r\n\x1a\n")
    session.pictures.add_images(
        [
            GeneratedImage(
                anchor_node_id=session.full_path()[-1].id,
                file="images/x.png",
                prompt=f"A note that reads {MARKER}",
                model="hidream",
                size="1024x1024",
                private_span=span.id,
            )
        ]
    )
    private.responses = ['{"prompt": "the note", "references": []}']
    session.write_image_prompt("")
    assert MARKER in private.requests[-1].messages[-1].text  # the part's own model may
    private.responses = ["They talked."]
    for _event in session.close_private(session.summarise_private()):
        pass

    main.responses = ['{"prompt": "a cat", "references": []}']
    sent = len(main.payloads)
    session.write_image_prompt("a cat")
    assert MARKER not in json.dumps(main.payloads[sent:])
    picture = next(c for c in session.image_ref_choices() if c.use.owner_kind == "picture")
    assert MARKER not in picture.use.caption


class Detachable(MockChatProvider):
    """A provider that can make calls beside the worker's, as the real one."""

    def detached(self):
        return self


def test_a_refused_tee_chat_sends_nothing_by_archive_rebuild_or_merge(tmp_path):
    """Send refused a TEE chat whose enclave was refused, or was being
    attested; Archive now, Rebuild, merges and the card prompt writer sent
    its messages to the model all the same."""
    from sealedlore.providers.tee import TeeRefused

    session, provider = chat_session(tmp_path, [f"Reply {i}." for i in range(8)] * 2)
    session.story.defaults.main_model = "TEE/gemma-3-27b"
    session.private_tee = FakeTee(signed=True)
    session.cast.append(Character(id="c2", name="Ada Lark", description="A tutor in grey."))
    for i in range(6):
        say(session, f"SECRET-{i} question")
    provider.responses = ["They talked about sums."]
    summary = session.archive(session.next_chunk(allow_partial=True))
    session.provider = Detachable(["unused"])
    assert not session.can_rebuild_in_background(), "rebuilt on the worker, behind the guard"
    sent = len(session.provider.requests)

    session.tee_block = "The attestation was refused: TEE/gemma-3-27b's quote failed verification"
    for call in (
        lambda: session.archive(session.next_chunk(allow_partial=True)),
        lambda: session.rebuild_summary(summary.id, force=True),
        lambda: session.start_rebuild([summary.id]),
        session.merge_now,
        lambda: session.write_card_prompts([session.cast[-1]]),
    ):
        with pytest.raises(TeeRefused, match="nothing was sent"):
            call()
    assert not session.prepare_archival(force=True)
    session.config.auto_archive = True
    assert not session.start_background_merge()
    assert len(session.provider.requests) == sent


def test_archiving_a_tee_chat_attests_its_model_again_once_due(tmp_path):
    """As every other call to the model: an attestation fifteen minutes old is
    renewed before the chapter's request goes."""
    from sealedlore.providers.tee import REATTEST_SECONDS
    from tests.test_private import AttestedTee, _attestation

    session, provider = chat_session(tmp_path, [f"Reply {i}." for i in range(8)])
    session.story.defaults.main_model = "TEE/x"
    clock = [1000.0]
    again = _attestation()
    tee = AttestedTee(session, clock, [_attestation(), again])
    for i in range(4):
        say(session, f"Question {i}")
    clock[0] += REATTEST_SECONDS
    sent = len(provider.requests)
    provider.responses = ["They talked."]
    session.archive(session.next_chunk(allow_partial=True))
    assert len(tee.attested) == 2 and session.story.chat_attestation is again
    assert len(provider.requests) == sent + 1
