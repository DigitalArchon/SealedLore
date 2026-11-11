"""Every reply an attested TEE/ model writes is checked against the record its key
signs, not only the passages and a private scene's summary: an aside, a chapter, a
merge, a card condensed, a scene's condensed part, a picture's prompt, anything else
that reaches the model (engine/session_private.py `_watch_tee`, through the
providers' `tee_watch`). Marked where a mark is shown (passages, asides, the
summary); logged and, when it fails, said otherwise; a failed one has the model
attested again before its next request. private/ replies are sealed and proved by
their seal: left as they are."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from sealedlore.models.private import Attestation
from sealedlore.providers.base import ChatRequest, StreamCompleted
from sealedlore.providers.tee import TeeClient
from sealedlore.storage.repository import read_api_log
from tests.test_chat import chat_session, say
from tests.test_private import MARKER, _long_scene, build, enter, play, turn

MODEL = "TEE/glm-5.3"


class RecordingTee:
    """The attested enclave's TeeClient as far as the session uses it: each
    check answers from `marks` (the last standing for the rest), and is kept."""

    def __init__(self, model: str, marks: list[str]) -> None:
        self.model = model
        self.marks = list(marks)
        self.checked: list[dict] = []
        self.doubted = 0

    def check_reply(self, response_id, **record):
        self.checked.append(record)
        return self.marks.pop(0) if len(self.marks) > 1 else self.marks[0]

    def doubt(self) -> None:
        self.doubted += 1

    def close(self) -> None:
        pass


def tee_chat(tmp_path: Path, replies: list[str], marks: list[str]):
    session, provider = chat_session(tmp_path, replies)
    session.story.defaults.main_model = MODEL
    tee = RecordingTee(MODEL, marks)
    session.private_tee = tee
    return session, provider, tee


def tee_checks(session) -> list[dict]:
    return [
        e for e in read_api_log(session.story.id, root=session.root) if e["kind"] == "tee_check"
    ]


def test_a_tee_chats_chapter_is_checked_logged_and_its_failure_said(tmp_path):
    """A TEE chat's every role is its own model: its chapters were never checked."""
    session, provider, tee = tee_chat(tmp_path, ["A.", "B."], ["verified", "verified", "failed"])
    say(session, "One")
    say(session, "Two")
    assert len(tee.checked) == 2, "each passage checked once, where it is marked"
    assert session.path()[-1].meta.tee == "verified"
    provider.responses = ["They talked of one and two."]
    session.archive(session.path()[:2])
    assert session.bundle.summaries and len(tee.checked) == 3
    assert [(e["model"], e["mark"]) for e in tee_checks(session)] == [(MODEL, "failed")]
    assert tee.doubted == 1, "attested again before the next request"
    (said,) = session.take_tee_notices()
    assert f"A reply from {MODEL} failed its TEE check" in said
    assert session.take_tee_notices() == [], "said once"


def test_any_call_on_the_attested_model_is_checked_and_no_other(tmp_path):
    from sealedlore.messages import ContentPart, PromptMessage

    session, provider, tee = tee_chat(tmp_path, ["A reply."], ["signed"])
    hello = (PromptMessage(role="user", parts=(ContentPart(text="Hi."),)),)
    session.provider.complete(ChatRequest(model=MODEL, messages=hello))
    assert len(tee.checked) == 1 and tee_checks(session)[-1]["mark"] == "signed"
    session.provider.complete(ChatRequest(model="anthropic/claude-sonnet-4.6", messages=hello))
    assert len(tee.checked) == 1, "nothing attested to check another model's reply with"
    # A private/ model's reply is sealed, proved by its seal: left as it is.
    count = len(tee.checked)
    session._watch_tee(ChatRequest(model=MODEL, messages=hello), StreamCompleted(sealed=True))
    assert len(tee.checked) == count and tee.doubted == 0


def test_every_client_reports_its_replies_its_siblings_and_copies_too():
    """The watch is the providers' own, as the route and reasoning watches are:
    each endpoint's client, the sibling a read runs on beside a passage, and the
    detached copy background archival and merges use."""
    from sealedlore.models.config import Config, ProviderConfig
    from sealedlore.providers.endpoints import EndpointsProvider
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider

    body = (
        'data: {"id": "r1", "choices": [{"delta": {"content": "Hi"}}]}\n\n'
        'data: {"id": "r1", "choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    seen: list = []

    def make(settings):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=body))
        return OpenAICompatibleProvider(settings, client=httpx.Client(transport=transport))

    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
    )
    router = EndpointsProvider(config, make=make)
    watch = lambda request, completed: seen.append((request.model, completed))  # noqa: E731
    router.tee_watch = watch
    router.complete(ChatRequest(model=MODEL, messages=[]))
    ((model, completed),) = seen
    assert model == MODEL and completed.response_id == "r1"
    assert completed.body_hash.startswith("sha256:")
    # Copies carry it (none is sent anything here: a twin's client is a real one).
    assert router.sibling().tee_watch is watch and router.detached().tee_watch is watch
    plain = make(config.providers[0])
    plain.tee_watch = watch
    assert plain.sibling().tee_watch is watch and plain.detached().tee_watch is watch


def test_an_asides_mark_is_shown_with_it_and_a_failure_with_no_mark_said(tmp_path):
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.transcript import AsideWidget

    QApplication.instance() or QApplication([])
    widget = AsideWidget("Who is listening?", "Nobody.", aside_id="a1", tee="failed")
    assert widget.header.text().endswith("TEE ⚠ check failed")
    assert "doesn't match its record" in widget.toolTip()
    plain = AsideWidget("q", "a", aside_id="a2")
    assert plain.header.text() == "Question  ·  out of character, not part of the story"
    widget.deleteLater()
    plain.deleteLater()


def test_a_private_scenes_aside_condensed_part_and_picture_prompt_are_checked(tmp_path):
    session, main, private, held = _long_scene(tmp_path, keep="disk")
    tee = RecordingTee("local/model", ["verified"])
    session.private_tee = tee
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    span = session.open_span
    assert span.scene_summary, "the scene was condensed"
    turns = [n for n in session.span_nodes(span) if n.kind == "assistant"]
    assert all(n.meta.tee == "verified" for n in turns)
    condensed = tee_checks(session)
    assert condensed and all(e.get("private") for e in condensed), "the condensed part"

    private.responses = ["A quiet answer."]
    for _event in session.ask("Who is listening?"):
        pass
    assert session.bundle.asides[-1].tee == "verified"
    before = len(tee.checked)
    private.responses = ['{"references": [], "prompt": "A lamp by the window."}']
    tee.marks = ["failed"]
    session.write_image_prompt("")
    assert len(tee.checked) == before + 1 and tee.doubted == 1
    assert any("failed its TEE check" in said for said in session.take_tee_notices())


def test_a_memory_scene_logs_no_check_of_its_own(tmp_path):
    """Its log holds nothing but its calls' content-free entries: the checks
    are still made, and a failure still said."""
    session, main, private, held = _long_scene(tmp_path, keep="memory")
    tee = RecordingTee("local/model", ["failed"])
    session.private_tee = tee
    for i in range(6):
        play(session, turn(held, f"Private turn {i}: {MARKER} " + ("more " * 60)))
    assert session.open_span.scene_summary and tee.doubted
    kinds = {e["kind"] for e in read_api_log(session.story.id, root=tmp_path)}
    assert "tee_check" not in kinds
    assert session.take_tee_notices(), "the condensed part's failure is said"


def test_a_failed_reply_has_the_model_attested_again_before_its_next_request(tmp_path):
    """Passage or not: a reply that doesn't match its record doubts the enclave
    (TeeClient.doubt), and the next request attests it first (`_attest_again`)."""
    attested: list[int] = []
    marks = ["failed", "verified"]

    class Doubted(TeeClient):
        def _attest(self):
            attested.append(1)
            return Attestation(verified=True, level="full", signing_address="0xab")

        def check_reply(self, *_args, **_kwargs):
            return marks.pop(0) if len(marks) > 1 else marks[0]

    session, _main, held = build(tmp_path)
    enter(session)
    tee = Doubted(
        "https://nano-gpt.com/api/v1",
        "k",
        "local/model",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    tee.attest()
    session.private_tee = tee
    play(session, turn(held, "One."))
    assert session.span_nodes(session.open_span)[-1].meta.tee == "failed" and tee.due()
    play(session, turn(held, "Two."))
    assert len(attested) == 2, "attested again before the turn after the failed one"
    assert session.take_reattestation()[0] == "attested"
    assert session.span_nodes(session.open_span)[-1].meta.tee == "verified"


def test_the_scene_summary_that_fails_doubts_the_enclave_too(tmp_path):
    session, _main, held = build(tmp_path)
    private = enter(session)
    tee = RecordingTee("local/model", ["verified", "failed"])
    session.private_tee = tee
    play(session, turn(held, "One."))
    private.responses = ["They spoke and parted."]
    session.summarise_private()
    assert session.open_span.summary_tee == "failed" and tee.doubted == 1
    assert len(tee.checked) == 2, "checked where it is marked, once"


def test_an_old_story_with_unmarked_asides_still_loads(tmp_path):
    from sealedlore.models.aside import Aside

    aside = Aside.model_validate(json.loads('{"question": "q", "answer": "a"}'))
    assert aside.tee is None


class NamingTee(RecordingTee):
    """As RecordingTee, and names who ran the model when asked to
    (providers/tee.py `check` with `name_upstream`)."""

    def check(self, response_id, *, name_upstream=False, **record):
        from sealedlore.models.private import TeeUpstream
        from sealedlore.providers.tee import ReplyCheck

        self.checked.append({**record, "named": name_upstream})
        found = TeeUpstream(name="phala-244", scope="model_instance", gpus="asserted")
        return ReplyCheck(self.marks[0], found if name_upstream else None)


def test_a_questions_answer_names_its_provider_as_a_passage_does(tmp_path):
    session, _main, private, held = _long_scene(tmp_path, keep="disk")
    session.config.tee_name_upstreams = True
    tee = NamingTee("local/model", ["verified"])
    session.private_tee = tee
    play(session, turn(held, "A private turn."))
    passages = [n for n in session.span_nodes(session.open_span) if n.kind == "assistant"]
    assert passages[-1].meta.tee == "verified"
    assert passages[-1].meta.tee_upstream.name == "phala-244"
    private.responses = ["A quiet answer."]
    for _event in session.ask("Who is listening?"):
        pass
    aside = session.bundle.asides[-1]
    assert aside.tee == "verified" and aside.tee_upstream.name == "phala-244"
    # Where a mark shows, the name was asked for; the replies with no mark of
    # their own (`_watch_tee`) are checked with `check_reply`, never named.
    assert tee.checked and all(c["named"] for c in tee.checked)


def test_a_question_shows_who_ran_its_model_beside_its_mark():
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.transcript import AsideWidget
    from sealedlore.models.private import TeeUpstream

    QApplication.instance() or QApplication([])
    upstream = TeeUpstream(name="phala-244", scope="model_instance", gpus="asserted")
    widget = AsideWidget("q", "a", aside_id="a1", tee="verified", tee_upstream=upstream)
    assert widget.header.text().endswith("TEE verified  ·  run by Phala")
    assert "the machine that ran the model" in widget.toolTip()
    assert "RedPill's word" in widget.toolTip()
    # Beside a failed check the name would rest on nothing: not shown.
    failed = AsideWidget("q", "a", aside_id="a2", tee="failed", tee_upstream=upstream)
    assert "run by" not in failed.header.text()
    widget.deleteLater()
    failed.deleteLater()
