"""What the review of 3.5.0b1 found in what endpoints and servers may answer
(Nov 22 2026), one test a fault: each fails on 0f289da, but those marked as
guards. Every server is a MockTransport or one on 127.0.0.1 started here;
every key is made up."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
import tracemalloc

import httpx
import pytest

from sealedlore.engine.recall import RecallItem
from sealedlore.engine.session import StorySession
from sealedlore.engine.speed_test import _reasoning_tokens as speed_reasoning_tokens
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
)
from sealedlore.models.lore import LoreEntry
from sealedlore.providers import local_embeddings, streaming
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import (
    COST_MOST,
    TOKENS_MOST,
    ChatRequest,
    ProviderError,
    StreamCompleted,
    parse_usage,
)
from sealedlore.providers.context_probe import detect_context
from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
from sealedlore.providers.images import list_image_models, list_own_models
from sealedlore.providers.local_embeddings import LocalEmbeddings
from sealedlore.providers.local_images import (
    LocalDrawing,
    LocalSettings,
    ModelAsTyped,
    comfy_options,
    list_models,
    local_settings,
)
from sealedlore.providers.media_http import MediaHttp
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.streaming import read_lines
from sealedlore.providers.videos import list_video_models
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.test_session import make_config, turn

KEY = "sk-test-PROBE"


def _request(model: str = "m") -> ChatRequest:
    return ChatRequest(
        model=model, messages=[PromptMessage(role="user", parts=(ContentPart(text="hi"),))]
    )


def _sse(*events: str) -> bytes:
    return "".join(f"data: {event}\n\n" for event in events).encode()


def _stream_client(body: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat(body: bytes) -> OpenAICompatibleProvider:
    config = ProviderConfig(name="t", base_url="https://chat.example/v1", api_key=KEY, model="m")
    return OpenAICompatibleProvider(config, client=_stream_client(body))


def _anthropic(body: bytes) -> AnthropicProvider:
    config = ProviderConfig(
        name="a",
        base_url="https://api.anthropic.com",
        api_key=KEY,
        model="claude-sonnet-4-6",
        api="anthropic",
    )
    return AnthropicProvider(config, client=_stream_client(body))


# --- N6: a reply's numbers are taken only when they are numbers a count or a
# --- charge can be, and JSON nothing can read is said as the endpoint's ------------

PASSAGE = '{"choices":[{"delta":{"content":"The passage."},"finish_reason":"stop"}]}'


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        True,
        -5,
        -0.5,
        "12",
        None,
        [3],
        pytest.param(10**400, id="400 digits"),
        1e300,
    ],
)
def test_a_count_or_a_cost_that_is_no_such_number_is_as_if_absent(value):
    usage = parse_usage(
        {
            "prompt_tokens": value,
            "completion_tokens": value,
            "cache_read_input_tokens": value,
            "prompt_tokens_details": {"cache_write_tokens": value},
            "cost": value,
        }
    )
    assert (usage.prompt_tokens, usage.completion_tokens) == (0, 0)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (0, 0)
    assert usage.cost == 0.0 and not usage.cost_reported, "estimated or unpriced, never this"


def test_what_an_endpoint_really_reports_is_read_as_it_was():
    usage = parse_usage(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 80.0,
            "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 150},
            "cost": 0,
        }
    )
    assert (usage.prompt_tokens, usage.completion_tokens) == (1200, 80)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (1000, 150)
    assert usage.cost == 0.0 and usage.cost_reported, "a stated zero is a cost: an included model"
    assert parse_usage({"input_tokens": 7, "total_cost": 0.25}).cost == 0.25
    assert parse_usage({"prompt_tokens": TOKENS_MOST, "cost": COST_MOST}).cost == COST_MOST
    assert parse_usage({"prompt_tokens": TOKENS_MOST + 1}).prompt_tokens == 0
    assert not parse_usage({"cost": COST_MOST * 2}).cost_reported
    # A name whose value is no count gives way to the next name for the same.
    assert parse_usage({"prompt_tokens": None, "input_tokens": 9}).prompt_tokens == 9
    assert parse_usage({"cost": "free", "total_cost": 0.5}).cost == 0.5


@pytest.mark.parametrize(
    "usage",
    [
        '{"prompt_tokens":1e999,"completion_tokens":5}',
        '{"prompt_tokens":NaN,"completion_tokens":5}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":true}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":-5}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":NaN}',
        '{"prompt_tokens":10,"completion_tokens":5,"completion_tokens_details":'
        '{"reasoning_tokens":1e999}}',
    ],
)
def test_a_passage_is_kept_whatever_numbers_its_usage_holds(usage):
    provider = _chat(_sse(PASSAGE, '{"choices":[],"usage":' + usage + "}", "[DONE]"))
    text, completed = provider.complete(_request())
    assert text == "The passage."
    assert completed.usage.completion_tokens == 5
    assert completed.usage.prompt_tokens in (0, 10)
    assert completed.usage.cost == 0.0 and not completed.usage.cost_reported
    assert 0 <= completed.reasoning_tokens <= TOKENS_MOST


def test_nanogpts_pricing_record_fills_in_only_a_cost_that_is_one():
    def cost_with(usage: str, pricing: str) -> tuple[float, bool]:
        chunk = '{"choices":[],"usage":' + usage + ',"x_nanogpt_pricing":' + pricing + "}"
        completed = _chat(_sse(PASSAGE, chunk, "[DONE]")).complete(_request())[1]
        return completed.usage.cost, completed.usage.cost_reported

    assert cost_with('{"prompt_tokens":10}', '{"cost":0.004}') == (0.004, True)
    # A cost the usage states wrongly doesn't keep the pricing record's out.
    assert cost_with('{"prompt_tokens":10,"cost":true}', '{"cost":0.004}') == (0.004, True)
    assert cost_with('{"prompt_tokens":10}', '{"cost":-1}') == (0.0, False)
    assert cost_with('{"prompt_tokens":10}', '{"cost":1e999}') == (0.0, False)


ANTHROPIC_EVENTS = (
    '{"type":"message_start","message":{"id":"msg_1","usage":%s}}',
    '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"The passage."}}',
    '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}',
    '{"type":"message_stop"}',
)


@pytest.mark.parametrize(
    "usage",
    [
        '{"input_tokens":1e999}',
        '{"input_tokens":10,"cache_read_input_tokens":-1e999}',
        '{"input_tokens":NaN,"cache_creation_input_tokens":true}',
    ],
)
def test_anthropics_usage_is_counted_whatever_numbers_it_holds(usage):
    events = [ANTHROPIC_EVENTS[0] % usage, *ANTHROPIC_EVENTS[1:]]
    text, completed = _anthropic(_sse(*events)).complete(_request("claude-sonnet-4-6"))
    assert text == "The passage."
    assert completed.usage.completion_tokens == 5
    assert completed.usage.prompt_tokens in (0, 10)


def test_a_number_of_thousands_of_digits_in_a_stream_is_a_malformed_event():
    """A guard: `outside_json` has said it as a JSONDecodeError since the
    review's files fixes; both stream parses must go on taking it so."""
    long = '{"choices":[],"x":' + "9" * 5000 + "}"
    with pytest.raises(ProviderError, match="malformed stream chunk"):
        _chat(_sse(PASSAGE, long, "[DONE]")).complete(_request())
    events = [ANTHROPIC_EVENTS[0] % ('{"input_tokens":' + "9" * 5000 + "}"), *ANTHROPIC_EVENTS[1:]]
    with pytest.raises(ProviderError, match="malformed stream event"):
        _anthropic(_sse(*events)).complete(_request("claude-sonnet-4-6"))


def test_the_speed_tests_reasoning_count_is_one_or_none():
    def tokens(raw: dict) -> int | None:
        return speed_reasoning_tokens(StreamCompleted(raw_usage=raw))

    assert tokens({"completion_tokens_details": {"reasoning_tokens": 40}}) == 40
    assert tokens({"reasoning_tokens": 12.0}) == 12
    assert tokens({"reasoning_tokens": float("inf")}) is None
    assert tokens({"completion_tokens_details": {"reasoning_tokens": float("nan")}}) is None
    assert tokens({}) is None


def test_a_logged_cost_that_is_no_cost_is_unpriced_not_free():
    entries = [
        {"id": "r1", "kind": "response", "usage": {"prompt_tokens": 5, "cost": float("nan")}},
        {"id": "r2", "kind": "response", "usage": {"prompt_tokens": 5, "cost": True}},
        {"id": "r3", "kind": "response", "usage": {"prompt_tokens": 5, "cost": -2}},
        {"id": "r4", "kind": "response", "usage": {"prompt_tokens": 5, "cost": 0}},
    ]
    total = usage_report(entries).total
    assert total.calls == 4 and total.cost == 0.0 and math.isfinite(total.cost)
    assert total.unpriced == 3, "only the stated zero is a charge of nothing"


def _probe(handler) -> object:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return detect_context("http://127.0.0.1:11434/v1", KEY, "m", client=client)


@pytest.mark.parametrize(
    "body",
    [
        b"[1,2,3]",
        b'"models"',
        b"7",
        b'{"data":{"id":"m"}}',
        b'{"data":[{"id":"m","context_length":1e999}]}',
        b'{"data":[{"id":"m","context_length":NaN}]}',
        b'{"data":[{"id":"m","context_length":true}]}',
        b'{"data":[{"id":"m","context_length":-4096}]}',
        b'{"data":[{"id":"m","top_provider":[1]}]}',
        b'{"data":[{"id":"m","context_length":' + b"9" * 5000 + b"}]}",
        b'{"models":[{"name":5,"context_length":4096}]}',
        b'{"models":[{"key":"m","loaded_instances":[7,{"config":[1]}]}]}',
        b'{"models":[{"key":"m","loaded_instances":5}]}',
        b'{"model_info":[1],"parameters":{"num_ctx":1}}',
        b'{"default_generation_settings":[4096]}',
        b'{"default_generation_settings":{"n_ctx":1e999}}',
    ],
)
def test_a_server_that_answers_detect_oddly_has_not_said(body):
    """Whatever JSON every one of the servers' ways of asking gets back, the
    answer is a context size or none: never an exception past Detect."""
    assert _probe(lambda request: httpx.Response(200, content=body)) is None


def test_detect_still_reads_what_the_servers_really_say():
    def listing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "m", "context_length": 32768}]})
        return httpx.Response(404)

    assert _probe(listing).tokens == 32768

    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "m:latest", "context_length": 0}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"parameters": "num_ctx 8192\nstop x"})
        return httpx.Response(404)

    assert _probe(ollama).tokens == 8192

    def llama_cpp(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096.0}})
        return httpx.Response(404)

    assert _probe(llama_cpp).tokens == 4096


# --- N1: an embeddings answer that isn't vectors of numbers is the endpoint's
# --- failure, and the turn goes through on keywords ---------------------------------

BAD_VECTORS = [
    pytest.param(b"[0.1, null, 0.3]", id="null"),
    pytest.param(b'[0.1, "x", 0.3]', id="string"),
    pytest.param(b"[[0.1, 0.2, 0.3]]", id="nested"),
    pytest.param(b"[0.1, NaN, 0.3]", id="NaN"),
    pytest.param(b"[0.1, Infinity, 0.3]", id="Infinity"),
    pytest.param(b"[0.1, -1e999, 0.3]", id="-1e999"),
    pytest.param(b"[0.1, true, 0.3]", id="true"),
    pytest.param(b"[0.1, " + b"9" * 400 + b", 0.3]", id="400 digits"),
    pytest.param(b'{"0": 0.1}', id="object"),
]


def _embeddings(handler) -> OpenAICompatibleEmbeddings:
    settings = EmbeddingProviderConfig(base_url="http://127.0.0.1:8080/v1", model="bge-m3")
    return OpenAICompatibleEmbeddings(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _rows(vector: bytes, usage: bytes = b"{}"):
    """An embeddings server answering every text with `vector`, written as
    it stands (JSON lets a server write NaN and 1e999; httpx's json= doesn't)."""

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["input"])
        rows = b",".join(b'{"index":%d,"embedding":%s}' % (i, vector) for i in range(count))
        body = b'{"data":[' + rows + b'],"usage":' + usage + b"}"
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return handler


@pytest.mark.parametrize("vector", BAD_VECTORS)
def test_a_vector_that_isnt_finite_numbers_is_the_endpoints_failure(vector):
    with pytest.raises(ProviderError, match="embedding"):
        _embeddings(_rows(vector)).embed(["text"])


def test_vectors_of_numbers_are_read_as_before():
    result = _embeddings(_rows(b"[0.5, -2, 3e-2]")).embed(["one", "two"])
    assert result.vectors == ((0.5, -2.0, 0.03), (0.5, -2.0, 0.03))
    assert all(type(value) is float for value in result.vectors[0])


@pytest.mark.parametrize(
    "usage",
    [
        b'{"prompt_tokens":1e999}',
        b'{"prompt_tokens":NaN,"cost":NaN}',
        b'{"total_tokens":-1e999,"cost":true}',
        b'{"prompt_tokens":true,"cost":-3}',
        b'{"prompt_tokens":[1],"cost":"0.1"}',
    ],
)
def test_an_embeddings_usage_nothing_can_count_costs_no_vectors(usage):
    result = _embeddings(_rows(b"[0.1, 0.2]", usage)).embed(["text"])
    assert result.vectors == ((0.1, 0.2),)
    assert result.usage.prompt_tokens == 0 and result.usage.cost == 0.0


def test_an_embeddings_usage_that_can_be_counted_still_is():
    result = _embeddings(_rows(b"[0.1]", b'{"total_tokens":12,"cost":0.001}')).embed(["a"])
    assert result.usage.prompt_tokens == 12 and result.usage.cost == 0.001


def _session_with_lore(tmp_path, story, cast, handler) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    story.defaults.context_token_budget = 8_000
    lore = [
        LoreEntry(
            id=f"e{i}",
            title=f"Entry {i}",
            content=f"Entry {i}. " + "The keep is old. " * 120,
            keywords=[f"kw{i}"],
        )
        for i in range(12)
    ]
    bundle = StoryBundle(story=story, cast=cast, lore=lore)
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    config.embedding_provider = EmbeddingProviderConfig(
        base_url="http://127.0.0.1:8080/v1", model="bge-m3"
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(["The door gives with a groan."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        embeddings=_embeddings(handler),
        learn_corrections=False,
    )


@pytest.mark.parametrize("vector", [b"[0.1, null, 0.3]", b"[0.1, NaN, 0.3]"])
def test_a_turn_goes_through_on_keywords_when_the_vectors_cant_be_read(
    tmp_path, story, cast, vector
):
    """llama.cpp and Ollama write a NaN as null: every turn of a story whose
    lore is selected was lost to a TypeError, where an HTTP 500 from the same
    server fell back to keywords."""
    session = _session_with_lore(tmp_path, story, cast, _rows(vector))
    assert session.lore_layout().mode == "select"
    list(session.send(turn()))
    assert [node.kind for node in session.nodes] == ["user", "assistant"]
    assert "embedding" in (session.last_retrieval.fallback_reason or "")
    assert session.bundle.embeddings == [], "nothing that isn't a vector is kept"


def test_recall_ranks_on_keywords_when_the_vectors_cant_be_read(tmp_path, story, cast):
    session = _session_with_lore(tmp_path, story, cast, _rows(b"[0.1, null, 0.3]"))
    items = [RecallItem(id="c1", kind="chapter", label="Chapter 1", text="The keep.", position=0)]
    similarity, reason = session._recall_similarity("the keep", items)
    assert similarity == {} and reason.startswith("keywords only:") and "embedding" in reason


class _NoModel:
    """A built-in model's stand-ins: one token a text, and hidden states of
    nothing at all, whose length is zero."""

    class _Encoded:
        ids = [0, 2]

    def encode_batch(self, texts):
        return [self._Encoded() for _ in texts]

    def run(self, _outputs, feeds):
        import numpy

        batch, width = feeds["input_ids"].shape
        return [numpy.zeros((batch, width, 4), dtype=numpy.float32)]


def test_the_built_in_model_gives_no_vector_that_isnt_numbers(tmp_path, monkeypatch):
    """A vector of zeros divided by its own length is NaN in every place:
    kept, it would be saved with the story and score NaN against every turn."""
    stand_in = _NoModel()
    monkeypatch.setattr(local_embeddings, "installed", lambda _root=None: True)
    monkeypatch.setattr(local_embeddings, "_model", lambda _folder: (stand_in, stand_in))
    with pytest.raises(ProviderError, match="built-in embeddings model"):
        LocalEmbeddings(tmp_path).embed(["text"])


# --- N2: a stream's lines are found without building what a compressed
# --- stream of line ends inflates to ----------------------------------------------


def _streamed(pieces, headers: dict | None = None) -> httpx.Response:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=iter(pieces), headers=headers or {})
        )
    )
    return client.send(client.build_request("GET", "https://x.test/"), stream=True)


def _in_pieces(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.mark.parametrize("end", [b"\n", b"\r\n", b"\r"])
def test_a_compressed_flood_of_line_ends_costs_little_to_read(end):
    """24 MB of line ends is 24 KB of gzip, and every kilobyte received was
    a megabyte of text split into a list of a million empty lines, each then
    handed on: 0.7 GB and 22 s for 96 KB, the reviewer measured."""
    flood = end * (24 * 1024 * 1024 // len(end))
    packed = gzip.compress(flood + b"data: last\n")
    assert len(packed) < 64 * 1024
    response = _streamed(_in_pieces(packed, 1024), {"content-encoding": "gzip"})
    digest = hashlib.sha256()
    tracemalloc.start()
    started = time.monotonic()
    try:
        lines = list(read_lines(response, digest))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert lines == ["", "data: last"], "a run of empty lines is one"
    assert digest.hexdigest() == hashlib.sha256(flood + b"data: last\n").hexdigest()
    assert peak < 6 * 1024 * 1024, f"{peak / 1e6:.0f} MB held to read a megabyte at a time"
    assert time.monotonic() - started < 2.0


def test_a_flood_of_line_ends_in_one_piece_is_read_a_slice_at_a_time():
    """Whatever size a piece arrives in, no more of it is made text, or a
    list of lines, than a slice at a time."""
    flood = b"\n" * (8 * 1024 * 1024)
    tracemalloc.start()
    try:
        lines = list(read_lines(_streamed([flood, b"\r", b"\ndata: x"])))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert lines == ["", "data: x"]
    assert peak < 2 * 1024 * 1024, f"{peak / 1e6:.0f} MB beyond the piece itself"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64, 100_000])
def test_lines_come_as_iter_lines_gives_them_but_for_runs_of_empty_ones(size, monkeypatch):
    """Each line as before, split at CRLF, CR or LF wherever the pieces and
    the slices fall; a run of empty lines comes as one (to a reader of
    events a blank line ends an event, and more of them are nothing)."""
    monkeypatch.setattr(streaming, "READ_SLICE", 7)
    text = (
        "\n\ndata: one\r\ndata: two\rdata: thrée\n\ndata: four\r\n\r\n\r\n\n\r"
        ": comment\n\r\ndata: five\r\r\rdata: six\n\n\n\nlast"
    )
    whole = list(_streamed([text.encode()]).iter_lines())
    expected = [line for index, line in enumerate(whole) if line or index == 0 or whole[index - 1]]
    assert expected != whole and expected[0] == "" and expected[-1] == "last"
    digest = hashlib.sha256()
    assert list(read_lines(_streamed(_in_pieces(text.encode(), size)), digest)) == expected
    assert digest.hexdigest() == hashlib.sha256(text.encode()).hexdigest()


def test_a_stream_of_single_blank_lines_is_read_exactly_as_before():
    """What every real stream is: events a blank line apart."""
    text = 'data: {"a": 1}\n\ndata: {"b": 2}\r\n\r\ndata: [DONE]\n\n'
    for size in (1, 4, 9, 1000):
        response = _streamed(_in_pieces(text.encode(), size))
        assert list(read_lines(response)) == list(_streamed([text.encode()]).iter_lines())


def test_a_line_end_split_between_pieces_is_one_line_end():
    assert list(read_lines(_streamed([b"data: a\r", b"\ndata: b\r", b"\r", b"\ndata: c\r"]))) == [
        "data: a",
        "data: b",
        "",
        "data: c",
    ]


def test_a_line_with_no_end_is_still_refused_in_slices(monkeypatch):
    monkeypatch.setattr(streaming, "MAX_LINE_CHARS", 300_000)
    with pytest.raises(ProviderError, match="a line of more than"):
        list(read_lines(_streamed([b"data: " + b"x" * 400_000])))
    (line,) = read_lines(_streamed([b"data: " + b"x" * 200_000 + b"\n"]))
    assert len(line) == 200_006


def test_a_whole_reply_through_a_compressed_flood_is_still_the_reply():
    stream = (
        b'data: {"choices":[{"delta":{"content":"The passage."},"finish_reason":"stop"}]}\n'
        + b"\n" * (4 * 1024 * 1024)
        + b"data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=iter(_in_pieces(gzip.compress(stream), 512)),
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        )

    config = ProviderConfig(name="t", base_url="https://chat.example/v1", api_key=KEY, model="m")
    provider = OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    started = time.monotonic()
    text, completed = provider.complete(_request())
    assert text == "The passage." and completed.finish_reason == "stop"
    assert completed.body_hash == "sha256:" + hashlib.sha256(stream).hexdigest()
    assert time.monotonic() - started < 2.0


# --- N3: a listing sends nothing while the endpoint's key, or the password in
# --- its address, is held back ----------------------------------------------------

HELD_ADDRESSES = [
    ("a1111", "http://admin:PROBE-PASSWORD@192.168.1.50:7860"),
    ("comfyui", "http://admin:PROBE-PASSWORD@192.168.1.50:8188"),
    ("openai", "http://admin:PROBE-PASSWORD@192.168.1.50:8000/v1"),
]


def _recording(seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sd-models"):
            return httpx.Response(200, json=[{"title": "dreamshaper_8.safetensors"}])
        return httpx.Response(200, json={"data": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _config_with(**media) -> Config:
    chat = ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="m")
    return Config(providers=[chat], active_provider_name="nano", **media)


@pytest.mark.parametrize(("api", "address"), HELD_ADDRESSES)
def test_the_picture_listing_sends_nothing_while_a_password_is_held_back(api, address):
    """A password written into a picture server's address, plain http to
    another machine and not yet confirmed: drawing refused, and the listing
    (asked whenever a picker opens) sent it all the same."""
    media = _config_with(image_endpoint=MediaEndpoint(base_url=address, api=api)).images()
    assert "isn't sent until you confirm" in media.key_withheld
    seen: list[httpx.Request] = []
    with pytest.raises(ProviderError, match="isn't sent until you confirm"):
        list_image_models(
            media.base_url,
            api=media.api,
            api_key=media.api_key,
            sealed_api=media.sealed_api,
            local=local_settings(media),
            key_withheld=media.key_withheld,
            client=_recording(seen),
        )
    assert seen == []
    if media.local:
        with pytest.raises(ProviderError, match="isn't sent until you confirm"):
            list_own_models(
                media.base_url,
                api=media.api,
                api_key=media.api_key,
                key_withheld=media.key_withheld,
                client=_recording(seen),
            )
        assert seen == []


def test_the_video_listing_sends_nothing_while_a_password_is_held_back():
    endpoint = MediaEndpoint(
        base_url="http://admin:PROBE-PASSWORD@192.168.1.50:8000/v1", api_key="video-key"
    )
    media = _config_with(video_endpoint=endpoint).videos()
    assert media.key_withheld
    seen: list[httpx.Request] = []
    with pytest.raises(ProviderError, match="isn't sent until you confirm"):
        list_video_models(
            media.base_url,
            api=media.api,
            api_key=media.api_key,
            sealed_api=media.sealed_api,
            key_withheld=media.key_withheld,
            client=_recording(seen),
        )
    assert seen == []


def test_a_listing_nothing_is_held_back_from_is_asked_as_before():
    endpoint = MediaEndpoint(
        base_url="http://admin:PROBE-PASSWORD@192.168.1.50:7860",
        api="a1111",
        key_in_clear_for="http://192.168.1.50:7860",
    )
    media = _config_with(image_endpoint=endpoint).images()
    assert not media.key_withheld, "confirmed once in Settings"
    seen: list[httpx.Request] = []
    found = list_image_models(
        media.base_url,
        api=media.api,
        api_key=media.api_key,
        local=local_settings(media),
        key_withheld=media.key_withheld,
        client=_recording(seen),
    )
    assert [model.id for model in found] == ["dreamshaper_8.safetensors"]
    assert [request.url.path for request in seen] == ["/sdapi/v1/sd-models"]


# --- N4: a workflow whose {{model}} is a free-text input takes the model as typed --

TYPED_WORKFLOW = json.dumps(
    {
        "1": {"class_type": "DiffusersLoader", "inputs": {"repo_id": "{{model}}"}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "text": "{{prompt}}"}},
    }
)


def _comfy(spec: object, node: str = "DiffusersLoader") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/object_info/{node}":
            return httpx.Response(200, json={node: {"input": {"required": spec}}})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _drawing(client: httpx.Client, workflow: str = TYPED_WORKFLOW) -> LocalDrawing:
    address = "http://127.0.0.1:8188"
    http = MediaHttp(address, "", timeout_seconds=5, client=client)
    return LocalDrawing("comfyui", http, address, "", LocalSettings(workflow=workflow))


@pytest.mark.parametrize(
    "entry",
    [
        ["STRING", {"default": ""}],
        ["STRING", {"multiline": False}],
        ["STRING"],
        ["INT", {"default": 0}],
    ],
)
def test_a_free_text_model_input_has_no_list_and_takes_the_name_as_typed(entry):
    """`["STRING", {…}]` was read as its choices: the models offered were S, T,
    R, I, N and G, and the model the workflow was written for was refused as
    one "ComfyUI has no model called"."""
    client = _comfy({"repo_id": entry})
    assert comfy_options(client.get, "http://127.0.0.1:8188", "DiffusersLoader", "repo_id") is None
    with pytest.raises(ModelAsTyped) as said:
        list_models(
            "comfyui",
            "http://127.0.0.1:8188",
            "",
            LocalSettings(workflow=TYPED_WORKFLOW),
            get=client.get,
        )
    assert "DiffusersLoader" in str(said.value) and "as typed" in str(said.value)
    _drawing(client).check_model("black-forest-labs/FLUX.1-dev", with_picture=False)
    # The listing a picker is filled from has nothing to offer, quietly.
    found = list_image_models(
        "http://127.0.0.1:8188",
        api="comfyui",
        local=LocalSettings(workflow=TYPED_WORKFLOW),
        client=client,
    )
    assert found == []


@pytest.mark.parametrize(
    "entry",
    [
        [["dreamshaper_8.safetensors", "sdxl.safetensors"], {}],
        [["dreamshaper_8.safetensors", "sdxl.safetensors"]],
        ["COMBO", {"options": ["dreamshaper_8.safetensors", "sdxl.safetensors"]}],
    ],
)
def test_a_model_input_with_choices_is_still_held_to_them(entry):
    client = _comfy({"repo_id": entry})
    names = comfy_options(client.get, "http://127.0.0.1:8188", "DiffusersLoader", "repo_id")
    assert names == ["dreamshaper_8.safetensors", "sdxl.safetensors"]
    found = list_models(
        "comfyui",
        "http://127.0.0.1:8188",
        "",
        LocalSettings(workflow=TYPED_WORKFLOW),
        get=client.get,
    )
    assert [model.id for model in found] == names
    _drawing(client).check_model("sdxl.safetensors", with_picture=False)
    with pytest.raises(ProviderError, match="has no model called 'FLUX.1-dev'"):
        _drawing(client).check_model("FLUX.1-dev", with_picture=False)


def test_a_node_the_server_hasnt_got_is_still_said():
    client = _comfy({"other": ["STRING", {}]})
    with pytest.raises(ProviderError, match="no DiffusersLoader node with an input repo_id"):
        comfy_options(client.get, "http://127.0.0.1:8188", "DiffusersLoader", "repo_id")
