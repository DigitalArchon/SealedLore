"""The run's call log (call_log.py, gui/call_log_view.py): every call made to
a model, where it went, how it travelled, its tokens, time and cost, and
never anything that was said. In memory only, for the run."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from sealedlore.call_log import (
    CALLS,
    PICTURE,
    PLAIN_HTTPS,
    PLAIN_LOCAL,
    TEXT,
    CallLog,
    route_words,
)
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCompleted, TextDelta
from sealedlore.providers.images import ImageClient
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

SECRET = "The vault code is 4471."
MESSAGES = [PromptMessage(role="user", parts=(ContentPart(text=SECRET),))]


def sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def provider(handler, base_url="https://nano-gpt.com/api/v1", name="NanoGPT"):
    config = ProviderConfig(name=name, base_url=base_url, api_key="sk-x")
    return OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def replied(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=sse(
            {"choices": [{"delta": {"content": "Hello."}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 1000},
                    "cost": 0.0021,
                },
            },
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_log():
    CALLS.clear()
    yield
    CALLS.clear()


def test_a_chat_call_is_kept_with_where_it_went_and_what_it_cost() -> None:
    route = {"order": ["fireworks"], "min_quantization": "fp8"}
    request = ChatRequest(model="z-ai/glm-5.3", messages=MESSAGES, extra_body={"provider": route})
    CALLS.label("scene_request", "z-ai/glm-5.3")
    text, _completed = provider(replied).complete(request)
    assert text == "Hello."
    [call] = CALLS.calls()
    assert call.kind == TEXT and call.model == "z-ai/glm-5.3" and call.status == "done"
    assert call.endpoint == "NanoGPT" and call.host == "nano-gpt.com"
    assert call.purpose == "scene_request"
    assert call.route == "fireworks · fp8+"
    assert call.tokens_in == 1200 and call.tokens_out == 40 and call.cache_read == 1000
    assert call.cost == pytest.approx(0.0021)
    assert call.protection == PLAIN_HTTPS and call.seconds is not None
    # Nothing that was said.
    assert SECRET not in repr(call) and "Hello." not in repr(call)


def test_a_failed_and_a_stopped_call_are_kept_too() -> None:
    def refused(_request):
        return httpx.Response(401, text="no")

    with pytest.raises(ProviderError):
        provider(refused).complete(ChatRequest(model="m", messages=MESSAGES))
    stream = provider(replied).stream(ChatRequest(model="m2", messages=MESSAGES))
    next(event for event in stream if isinstance(event, TextDelta))
    stream.close()
    failed, stopped = CALLS.calls()
    assert failed.status == "failed" and "401" in failed.detail
    assert stopped.status == "stopped"


def test_a_local_endpoint_says_it_travels_unencrypted() -> None:
    local = provider(replied, base_url="http://192.168.1.20:8080/v1", name="Home")
    local.complete(ChatRequest(model="llama", messages=MESSAGES))
    assert CALLS.calls()[-1].protection == PLAIN_LOCAL


def test_an_encrypted_models_call_says_so(monkeypatch) -> None:
    from sealedlore.providers import streaming

    def sealed(self, request):
        yield TextDelta(text="Sealed.")
        yield StreamCompleted(sealed=True, finish_reason="stop")

    monkeypatch.setattr(OpenAICompatibleProvider, "_stream", sealed)
    client = provider(replied)
    assert streaming  # the shared stream is what records, whatever the client
    client.complete(ChatRequest(model="private/glm-5-3", messages=MESSAGES))
    protection = CALLS.calls()[-1].protection
    assert protection.startswith("End-to-end encrypted (NanoGPT")
    assert "opened with the attested key" in protection


def test_a_label_goes_only_to_its_own_models_call() -> None:
    log = CallLog()
    log.label("summary_request", "model-a")
    assert log.start(TEXT, "model-b", endpoint="", address="", protection="").purpose == ""
    assert log.start(TEXT, "Name::model-a", endpoint="", address="", protection="").purpose == (
        "summary_request"
    )
    # Taken once.
    assert log.start(TEXT, "model-a", endpoint="", address="", protection="").purpose == ""


def test_the_log_keeps_only_the_newest() -> None:
    log = CallLog(kept=3)
    for number in range(5):
        log.start(TEXT, f"m{number}", endpoint="", address="", protection="")
    assert [c.model for c in log.calls()] == ["m2", "m3", "m4"]


def test_a_picture_is_kept_with_its_stated_cost() -> None:
    def made(_request):
        return httpx.Response(200, json={"data": [{"b64_json": "aGk="}], "cost": 0.009})

    client = ImageClient(
        "https://nano-gpt.com/api/v1",
        "sk-x",
        client=httpx.Client(transport=httpx.MockTransport(made)),
    )
    client.generate({"model": "flux", "prompt": SECRET})
    [call] = CALLS.calls()
    assert call.kind == PICTURE and call.model == "flux" and call.cost == pytest.approx(0.009)
    assert call.endpoint == "NanoGPT" and SECRET not in repr(call)


def test_route_words() -> None:
    assert route_words({}) == ""
    assert route_words({"only": ["deepinfra"], "allow_fallbacks": False}) == "only deepinfra"
    assert route_words({"sort": "latency"}) == "by latency"


# --- the window ------------------------------------------------------------------


def test_the_view_lists_calls_newest_first_with_estimated_costs(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.call_log_view import CallLogDialog
    from sealedlore.models.config import Config, ModelPrice

    QApplication.instance() or QApplication([])
    config = Config(model_prices={"z-ai/glm-5.3": ModelPrice(prompt=1.0, completion=2.0)})
    first = CALLS.start(
        TEXT,
        "z-ai/glm-5.3",
        endpoint="NanoGPT",
        address="https://nano-gpt.com",
        protection=PLAIN_HTTPS,
    )
    CALLS.finish(first, "done", tokens_in=1_000_000, tokens_out=0)
    CALLS.label("summary_request", "x")
    second = CALLS.start(
        TEXT, "x", endpoint="NanoGPT", address="https://nano-gpt.com", protection=PLAIN_HTTPS
    )
    CALLS.finish(second, "done", tokens_in=10, tokens_out=5, cost=0.0)
    view = CallLogDialog(lambda: config)
    rows = [
        [view.table.item(r, c).text() for c in range(view.table.columnCount())]
        for r in range(view.table.rowCount())
    ]
    assert rows[0][1] == "Chapter summaries" and rows[0][7] == "$0"
    assert rows[1][2] == "z-ai/glm-5.3" and rows[1][7] == "~$1.0000"
    assert rows[1][8] == "HTTPS only"
    assert "2 calls" in view.totals.text() and "~$1.0000 estimated" in view.totals.text()
    assert view.as_text().splitlines()[0].startswith("Time\tFor")
    view.deleteLater()


def test_a_storys_calls_say_what_they_were_for(tmp_path, story, cast, estimator) -> None:
    from sealedlore.engine.prompt import TurnRequest
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.endpoints import EndpointsProvider
    from sealedlore.storage.repository import StoryBundle

    config = Config(
        providers=[ProviderConfig(name="NanoGPT", api_key="k", model="z-ai/glm-5.3")],
        active_provider_name="NanoGPT",
        scene_reads="every_turn",
        plot_reads=False,
        look_picks="manual",
        suggest_characters=False,
    )

    def make(settings):
        return OpenAICompatibleProvider(
            settings, client=httpx.Client(transport=httpx.MockTransport(replied))
        )

    session = StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        EndpointsProvider(config, make=make),
        root=tmp_path,
        estimator=estimator,
        learn_corrections=False,
    )
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="I lift the torch.",
        controlled_character_id="char-serrik",
        npc_scope="all",
        agency_mode="contested",
    )
    for _event in session.send(turn):
        pass
    purposes = [call.purpose for call in CALLS.calls()]
    assert purposes[:2] == ["request", "scene_request"]
    assert all(call.endpoint == "NanoGPT" for call in CALLS.calls())
