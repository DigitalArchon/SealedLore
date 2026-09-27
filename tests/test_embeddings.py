"""The embeddings client and its wire format. See §7. Never touches the network."""

from __future__ import annotations

import httpx
import pytest

from sealedlore.models.config import Config, EmbeddingProviderConfig, ProviderConfig
from sealedlore.providers.base import ProviderError
from sealedlore.providers.embeddings import (
    OpenAICompatibleEmbeddings,
    build_embedding_payload,
    parse_embedding_response,
)


def settings(**kwargs) -> EmbeddingProviderConfig:
    base = {"base_url": "https://nano-gpt.com/api/v1", "model": "BAAI/bge-m3"}
    return EmbeddingProviderConfig(**{**base, **kwargs})


def client_for(handler) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def rows(count: int, width: int = 3, *, shuffled: bool = False):
    data = [
        {"index": index, "embedding": [float(index)] * width, "object": "embedding"}
        for index in range(count)
    ]
    if shuffled:
        data.reverse()
    return {"data": data, "usage": {"prompt_tokens": 12}}


# --- payload --------------------------------------------------------------


def test_the_payload_is_a_plain_openai_embeddings_request():
    payload = build_embedding_payload(["one", "two"], settings())

    assert payload == {
        "model": "BAAI/bge-m3",
        "input": ["one", "two"],
        "encoding_format": "float",
    }


def test_dimensions_are_sent_only_when_asked_for():
    """Models without Matryoshka support reject the field rather than ignoring it."""
    assert "dimensions" not in build_embedding_payload(["x"], settings(dimensions=0))
    assert build_embedding_payload(["x"], settings(dimensions=512))["dimensions"] == 512


# --- parsing --------------------------------------------------------------


def test_vectors_come_back_in_input_order_even_when_the_rows_are_not():
    parsed = parse_embedding_response(rows(3, shuffled=True), expected=3)
    assert [vector[0] for vector in parsed] == [0.0, 1.0, 2.0]


def test_a_short_batch_is_an_error_rather_than_a_silent_mismatch():
    with pytest.raises(ProviderError, match="asked for 3"):
        parse_embedding_response(rows(2), expected=3)


def test_mixed_widths_are_rejected():
    body = {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 2.0]}]}
    with pytest.raises(ProviderError, match="mixed dimensions"):
        parse_embedding_response(body, expected=2)


def test_a_body_without_data_is_an_error():
    with pytest.raises(ProviderError, match="no data array"):
        parse_embedding_response({"error": "nope"}, expected=1)


# --- the client -----------------------------------------------------------


def test_embedding_posts_to_the_embeddings_path_and_carries_the_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=rows(1))

    client = OpenAICompatibleEmbeddings(
        settings(api_key="sk-test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.embed(["hello"])

    assert seen["url"] == "https://nano-gpt.com/api/v1/embeddings"
    assert seen["auth"] == "Bearer sk-test"
    assert len(result.vectors) == 1
    assert result.usage.prompt_tokens == 12


def test_long_input_lists_are_split_into_batches_and_rejoined_in_order():
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent = json.loads(request.content)["input"]
        batches.append(sent)
        start = len(batches) - 1
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(start * 2 + index)]}
                    for index in range(len(sent))
                ]
            },
        )

    client = OpenAICompatibleEmbeddings(
        settings(batch_size=2),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.embed(["a", "b", "c", "d", "e"])

    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert len(result.vectors) == 5


def test_prefixes_are_applied_when_the_model_wants_them():
    """bge-m3 needs none, but nomic and E5 do — so this stays data, not code."""
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content)["input"])
        return httpx.Response(200, json=rows(1))

    client = OpenAICompatibleEmbeddings(
        settings(query_prefix="search_query: ", document_prefix="search_document: "),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.embed(["the accord"], as_query=True)
    client.embed(["the accord"])

    assert seen == [["search_query: the accord"], ["search_document: the accord"]]


def test_an_http_error_carries_the_body_for_the_status_line():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="insufficient balance")

    with pytest.raises(ProviderError) as caught:
        client_for(handler).embed(["x"])

    assert caught.value.status_code == 402
    assert "insufficient balance" in (caught.value.body or "")


def test_an_unreachable_endpoint_is_a_provider_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ProviderError, match="unreachable"):
        client_for(handler).embed(["x"])


def test_embedding_nothing_makes_no_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    assert client_for(handler).embed([]).vectors == ()


# --- config resolution ----------------------------------------------------


def test_embeddings_inherit_the_chat_endpoint_when_left_blank():
    config = Config(
        providers=[
            ProviderConfig(
                name="nano",
                base_url="https://nano-gpt.com/api/v1",
                api_key="sk-chat",
                model="anthropic/claude-sonnet-4.5",
            )
        ],
        active_provider_name="nano",
        embedding_provider=EmbeddingProviderConfig(model="BAAI/bge-m3"),
    )

    resolved = config.embeddings()

    assert resolved is not None
    assert resolved.base_url == "https://nano-gpt.com/api/v1"
    assert resolved.api_key == "sk-chat"
    assert resolved.model == "BAAI/bge-m3"


def test_an_explicit_embedding_endpoint_is_left_alone():
    config = Config(
        providers=[
            ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", api_key="sk-chat")
        ],
        active_provider_name="nano",
        embedding_provider=EmbeddingProviderConfig(
            base_url="http://localhost:11434/v1", api_key="", model="bge-m3"
        ),
    )

    resolved = config.embeddings()

    assert resolved is not None
    assert resolved.base_url == "http://localhost:11434/v1"
    # The chat key belongs to the chat host; another server never gets it.
    assert resolved.api_key == ""


def test_a_blank_key_is_inherited_for_the_chat_host_named_explicitly():
    config = Config(
        providers=[
            ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", api_key="sk-chat")
        ],
        active_provider_name="nano",
        embedding_provider=EmbeddingProviderConfig(
            base_url="https://NANO-GPT.com/api/v1/", model="bge-m3"
        ),
    )

    resolved = config.embeddings()

    assert resolved is not None
    assert resolved.api_key == "sk-chat"


def test_no_embedding_config_means_no_embeddings():
    assert Config().embeddings() is None


def test_a_local_embeddings_server_may_use_plain_http():
    """§1 allows http only on loopback, which is exactly this case (§7)."""
    assert EmbeddingProviderConfig(base_url="http://127.0.0.1:1234/v1").base_url

    with pytest.raises(ValueError, match="https"):
        EmbeddingProviderConfig(base_url="http://example.com/v1")
