"""Embeddings made on this computer (providers/local_embeddings.py) and the
servers found on it (providers/local_servers.py).

Never the network and never the real 0.6 GB model: the download runs on an
httpx MockTransport against small stand-in files, the model on a stand-in
tokenizer and session. One opt-in test runs the real model where it is
installed (SEALEDLORE_REAL_EMBEDDINGS=<data folder>)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import numpy
import pytest

from sealedlore.call_log import CALLS, ON_THIS_COMPUTER
from sealedlore.models.config import (
    BUILTIN_EMBEDDING_MODEL,
    Config,
    EmbeddingProviderConfig,
    ProviderConfig,
)
from sealedlore.providers import local_embeddings as local
from sealedlore.providers.base import ProviderError
from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings
from sealedlore.providers.local_embeddings import (
    DownloadStopped,
    LocalEmbeddings,
    ModelFile,
    download,
    installed,
    make_embeddings,
    model_dir,
    remove,
)
from sealedlore.providers.local_servers import FoundServer, find_local_servers, found_words

NANO = "https://nano-gpt.com/api/v1"


def nano(**embeddings) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    provider = EmbeddingProviderConfig(**embeddings) if embeddings is not None else None
    return Config(providers=[chat], active_provider_name="NanoGPT", embedding_provider=provider)


# --- the settings -------------------------------------------------------------------


def test_built_in_embeddings_inherit_nothing_and_keep_a_model_id_of_their_own():
    resolved = nano(builtin=True, base_url="", model="BAAI/bge-m3").embeddings()
    assert resolved.builtin and resolved.model == BUILTIN_EMBEDDING_MODEL
    assert resolved.base_url == "" and resolved.api_key == ""
    assert isinstance(make_embeddings(resolved), LocalEmbeddings)
    # An endpoint is as it was: NanoGPT's bge-m3, the chat key on its own host.
    endpoint = nano(model="BAAI/bge-m3").embeddings()
    assert endpoint.base_url == NANO and endpoint.api_key == "nano-key"
    assert isinstance(make_embeddings(endpoint), OpenAICompatibleEmbeddings)


@pytest.mark.parametrize(
    ("settings", "here"),
    [
        ({"builtin": True}, True),
        ({"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3"}, True),
        ({"base_url": "http://localhost:1234/v1", "model": "bge-m3"}, True),
        ({"base_url": "http://192.168.1.20:11434/v1", "model": "bge-m3"}, False),
        ({"model": "BAAI/bge-m3"}, False),
    ],
)
def test_what_counts_as_lore_staying_on_this_computer(settings, here):
    assert nano(**settings).embeddings_stay_here() is here
    assert Config().embeddings_stay_here() is False  # off: nothing embedded at all


# --- the download -----------------------------------------------------------------------


def stand_ins(monkeypatch) -> dict[str, bytes]:
    """Small files in the model's place, pinned by their own sha256."""
    contents = {"model_int8.onnx": b"onnx" * 1000, "tokenizer.json": b'{"tok": 1}'}
    files = tuple(
        ModelFile(name, f"x/{name}", len(data), hashlib.sha256(data).hexdigest())
        for name, data in contents.items()
    )
    monkeypatch.setattr(local, "FILES", files)
    return contents


def hub(contents: dict[str, bytes], seen: list[httpx.Request], *, change: str = ""):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        data = contents[name]
        if name == change:
            data = data[:-1] + b"!"
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_model_is_downloaded_checked_and_kept(tmp_path: Path, monkeypatch):
    contents = stand_ins(monkeypatch)
    seen: list[httpx.Request] = []
    steps: list[tuple[int, int]] = []
    assert not installed(tmp_path)
    download(tmp_path, progress=lambda *step: steps.append(step), client=hub(contents, seen))
    assert installed(tmp_path)
    folder = model_dir(tmp_path)
    assert {p.name for p in folder.iterdir()} == set(contents)
    assert steps[-1] == (sum(map(len, contents.values())), sum(map(len, contents.values())))
    # Pinned to the commit, and asked of nothing but Hugging Face.
    assert all(r.url.host == "huggingface.co" and local.REVISION in r.url.path for r in seen)
    assert not any("authorization" in r.headers for r in seen)
    # Asked again, nothing is fetched again.
    download(tmp_path, client=hub(contents, seen := []))
    assert seen == []


def test_a_file_that_isnt_the_one_pinned_is_never_kept(tmp_path: Path, monkeypatch):
    contents = stand_ins(monkeypatch)
    with pytest.raises(ProviderError, match="sha256 doesn't match"):
        download(tmp_path, client=hub(contents, [], change="tokenizer.json"))
    folder = model_dir(tmp_path)
    assert not (folder / "tokenizer.json").exists()
    assert not list(folder.glob("*.part"))
    assert not installed(tmp_path)


def test_a_download_stopped_keeps_nothing_half_made(tmp_path: Path, monkeypatch):
    contents = stand_ins(monkeypatch)
    with pytest.raises(DownloadStopped):
        download(tmp_path, stopped=lambda: True, client=hub(contents, []))
    assert not list(model_dir(tmp_path).glob("*"))


def test_remove_takes_it_off_this_computer(tmp_path: Path, monkeypatch):
    contents = stand_ins(monkeypatch)
    download(tmp_path, client=hub(contents, []))
    remove(tmp_path)
    assert not installed(tmp_path) and not model_dir(tmp_path).exists()


def test_the_pins_are_the_files_measured():
    """The model and tokenizer that were compared with NanoGPT's bge-m3."""
    assert local.REPO == "Xenova/bge-m3"
    assert local.REVISION == "4de13258303883538bd53b696b452bf8099f0858"
    by_name = {f.name: f for f in local.FILES}
    assert by_name["model_int8.onnx"].sha256.startswith("a206e10e995aa2a8")
    assert by_name["tokenizer.json"].sha256.startswith("6710678b12670bc4")


# --- embedding ------------------------------------------------------------------------


class Encoded:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids


class StandInTokenizer:
    def encode_batch(self, texts):
        # Longer texts, more tokens; <s> = 0 first, as XLM-RoBERTa's.
        return [Encoded([0] + [5 + (ord(c) % 50) for c in text] + [2]) for text in texts]


class StandInSession:
    """Hidden states whose first token's vector is the text's length and
    first token id, so order and pooling can be checked."""

    def __init__(self) -> None:
        self.feeds: list[dict] = []

    def run(self, _outputs, feeds):
        self.feeds.append(feeds)
        ids, mask = feeds["input_ids"], feeds["attention_mask"]
        batch, width = ids.shape
        hidden = numpy.zeros((batch, width, 4), dtype=numpy.float32)
        hidden[:, 0, 0] = mask.sum(axis=1)
        hidden[:, 0, 1] = 1.0
        return [hidden]


@pytest.fixture
def stand_in_model(tmp_path: Path, monkeypatch) -> StandInSession:
    contents = stand_ins(monkeypatch)
    download(tmp_path, client=hub(contents, []))
    session = StandInSession()
    monkeypatch.setattr(local, "_model", lambda _folder: (StandInTokenizer(), session))
    return session


def test_vectors_come_back_in_the_order_asked_normalised(tmp_path: Path, stand_in_model):
    texts = ["a much longer text than the others", "short", "middling text"]
    result = LocalEmbeddings(tmp_path).embed(texts)
    assert result.model == BUILTIN_EMBEDDING_MODEL and result.dimensions == 4
    for text, vector in zip(texts, result.vectors, strict=True):
        length = len(text) + 2
        norm = (length**2 + 1) ** 0.5
        assert vector[0] == pytest.approx(length / norm)
        assert numpy.linalg.norm(vector) == pytest.approx(1.0)
    # One text a run: nothing else in the run changes its vector.
    assert [feeds["input_ids"].shape[0] for feeds in stand_in_model.feeds] == [1, 1, 1]
    assert result.usage.cost == 0.0 and result.usage.cost_reported
    assert result.usage.prompt_tokens == sum(len(t) + 2 for t in texts)


def test_the_call_log_says_it_never_left_this_computer(tmp_path: Path, stand_in_model):
    LocalEmbeddings(tmp_path).embed(["a lantern"])
    call = CALLS.calls()[-1]
    assert call.protection == ON_THIS_COMPUTER and call.model == BUILTIN_EMBEDDING_MODEL
    assert call.host == "" and call.cost == 0.0


def test_batches_hold_texts_of_like_length(tmp_path: Path, stand_in_model, monkeypatch):
    monkeypatch.setattr(local, "BATCH", 2)
    LocalEmbeddings(tmp_path).embed(["x" * 30, "y", "z" * 29, "w" * 2])
    widths = [feeds["input_ids"].shape[1] for feeds in stand_in_model.feeds]
    assert widths == [2 + 2, 30 + 2]  # the two short ones together, then the two long
    # The shorter padded with XLM-RoBERTa's pad id, masked out.
    first = stand_in_model.feeds[0]
    assert (first["input_ids"][0, -1], first["attention_mask"][0, -1]) == (local.PAD_ID, 0)


def test_before_the_model_is_downloaded_lore_falls_back_to_keywords_saying_why(tmp_path: Path):
    with pytest.raises(ProviderError, match="download it in Settings → Lore"):
        LocalEmbeddings(tmp_path).embed(["a lantern"])


def test_a_story_on_encrypted_models_with_lore_embedded_here_keeps_its_mark(tmp_path: Path):
    from tests.test_review_b14 import sealed_story

    here = sealed_story(
        tmp_path / "here", ["x"], embedding_provider=EmbeddingProviderConfig(builtin=True)
    )
    here.embeddings = LocalEmbeddings(tmp_path)
    assert here.story_sealed()
    elsewhere = sealed_story(
        tmp_path / "elsewhere", ["x"], embedding_provider=EmbeddingProviderConfig()
    )
    elsewhere.embeddings = OpenAICompatibleEmbeddings(elsewhere.config.embeddings())
    assert not elsewhere.story_sealed()


@pytest.mark.skipif(
    not os.environ.get("SEALEDLORE_REAL_EMBEDDINGS"),
    reason="the real model: SEALEDLORE_REAL_EMBEDDINGS=<a data folder holding it>",
)
def test_the_real_model_puts_a_sentence_nearer_its_paraphrase():
    root = Path(os.environ["SEALEDLORE_REAL_EMBEDDINGS"])
    assert installed(root)
    a, b, c = (
        LocalEmbeddings(root)
        .embed(
            [
                "The farmhouse stands alone in the snow.",
                "A lonely farm building surrounded by snow.",
                "The ship's engines failed over the ocean.",
            ]
        )
        .vectors
    )
    assert len(a) == 1024
    assert numpy.dot(a, b) > numpy.dot(a, c) + 0.1


# --- servers on this computer -------------------------------------------------------------


def test_servers_on_this_computer_are_found_with_their_embedding_models():
    asked: list[str] = []

    def get(url: str) -> httpx.Response:
        asked.append(url)
        request = httpx.Request("GET", url)
        if ":11434/" in url:
            body = {
                "data": [{"id": "llama3:8b"}, {"id": "nomic-embed-text"}, {"id": "bge-m3:latest"}]
            }
            return httpx.Response(200, json=body, request=request)
        if ":1234/" in url:
            raise httpx.ConnectError("refused", request=request)
        if ":8080/" in url:
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]}, request=request)
        return httpx.Response(404, request=request)

    found = find_local_servers(get=get)
    assert all(url.startswith("http://127.0.0.1:") and url.endswith("/v1/models") for url in asked)
    ollama, other = found
    assert ollama.name == "Ollama" and ollama.base_url == "http://127.0.0.1:11434/v1"
    assert ollama.embedding_models == ("bge-m3:latest", "nomic-embed-text")  # bge-m3 first
    assert other.embedding_models == ()
    words = found_words(found)
    assert "Ollama at http://127.0.0.1:11434/v1: bge-m3:latest, nomic-embed-text" in words
    assert "with no embedding model loaded" in words


def test_ollama_without_an_embedding_model_says_how_to_get_one():
    words = found_words([FoundServer("Ollama", "http://127.0.0.1:11434/v1", ("llama3:8b",))])
    assert "ollama pull bge-m3" in words
    assert found_words([]).startswith("No server answered")
