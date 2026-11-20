"""Embeddings made on this computer: bge-m3, the default embeddings model,
run by ONNX Runtime (the author, Nov 20 2026: semantic lore and recall with
nothing sent anywhere).

The model is the int8 ONNX export of BAAI/bge-m3 (Xenova/bge-m3, pinned to a
commit), with the tokenizer BAAI ships (byte for byte the same file):
downloaded once into the data folder's `models/` from Hugging Face, each
file checked against its sha256 before it is kept (`download`). Nothing else
is ever fetched; the tokenizer library's own downloader is never called.

Measured against NanoGPT's bge-m3 on the long test story's 100 lore entries
and 80 logged turns (SealedLoreDocs design/lore.md, Nov 20 2026): the same
text's vectors agree at cosine 0.97-0.98, the 0.45 threshold decides alike
95.6% of the time, and retrieval against the judged relevance is the same at
top 5 (within 0.01) and a little lower at top 3 (0.00-0.03, about a standard
error over 80 turns). Its vectors are kept under a model id of their own
(BUILTIN_EMBEDDING_MODEL), never mixed with an endpoint's.

A vector is the first token's hidden state, normalised (bge-m3's dense
embedding), each text cut at bge-m3's 8,192 tokens. **One text at a time**
(BATCH): the int8 model quantises each run's activations by the whole run's
range, padding included, so a text batched with others came out at cosine
0.978-0.98 to itself alone, and a cached lore vector would drift from the
query's. Alone it is the same every time, and no slower (100 entries in
14.5 s, a turn in 0.02 s, on a four-core VM). The session is loaded once a
process, on first use (about 0.6 GB), and shared; ONNX Runtime and the
tokenizer are imported then, never at start-up.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sealedlore.call_log import CALLS, EMBEDDINGS, ON_THIS_COMPUTER
from sealedlore.models.config import BUILTIN_EMBEDDING_MODEL
from sealedlore.models.node import Usage
from sealedlore.providers.base import ProviderError, StreamCancelled
from sealedlore.providers.embeddings import EmbeddingResult
from sealedlore.providers.http import make_client

REPO = "Xenova/bge-m3"
REVISION = "4de13258303883538bd53b696b452bf8099f0858"
# The data folder's subfolder the model is kept in.
MODELS_DIR = "models"
FOLDER = "bge-m3-int8"
# bge-m3's longest input, in tokens.
MAX_TOKENS = 8192
# Texts in one run. One: the int8 model's quantisation depends on what else
# is in the run (see the module's note), so a text alone is the same vector
# every time.
BATCH = 1
# XLM-RoBERTa's padding id.
PAD_ID = 1
DOWNLOAD_TIMEOUT = 60.0


@dataclass(frozen=True)
class ModelFile:
    name: str  # as kept
    path: str  # in the repository
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{REPO}/resolve/{REVISION}/{self.path}"


FILES = (
    ModelFile(
        "model_int8.onnx",
        "onnx/model_int8.onnx",
        568_456_694,
        "a206e10e995aa2a833924bcd725ba5dd6c3425cd34bac3cf2b5677cd2a1c51d6",
    ),
    ModelFile(
        "tokenizer.json",
        "tokenizer.json",
        17_082_821,
        "6710678b12670bc442b99edc952c4d996ae309a7020c1fa0096dd245c2faf790",
    ),
)
DOWNLOAD_BYTES = sum(f.size for f in FILES)


def model_dir(root: Path | None = None) -> Path:
    from sealedlore.storage.paths import data_home

    return (root or data_home()) / MODELS_DIR / FOLDER


def installed(root: Path | None = None) -> bool:
    """Every file there, at its size (its sha256 was checked as it came)."""
    folder = model_dir(root)
    return all(
        (folder / f.name).is_file() and (folder / f.name).stat().st_size == f.size for f in FILES
    )


def size_words(count: int = DOWNLOAD_BYTES) -> str:
    return f"{count / 1_000_000:.0f} MB"


class DownloadStopped(StreamCancelled):
    """The download was stopped; nothing half-made is kept."""


def download(
    root: Path | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
    stopped: Callable[[], bool] = lambda: False,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch the model's files that aren't there yet, each to a part file
    hashed as it comes and kept only if its sha256 is the one pinned.
    `progress(done, total)` in bytes; `stopped` is asked between pieces.
    Raises ProviderError (DownloadStopped when stopped)."""
    folder = model_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    wanted = [f for f in FILES if not _ready(folder / f.name, f)]
    total = sum(f.size for f in wanted)
    done = 0
    owned = client is None
    # A download carries neither key nor body: it may follow Hugging Face's
    # redirect to its storage, under make_client's guard (https only, never
    # into this computer or its network).
    http = client or make_client(
        "https://huggingface.co", timeout=httpx.Timeout(DOWNLOAD_TIMEOUT), follow_redirects=True
    )
    try:
        for item in wanted:
            part = folder / f"{item.name}.part"
            digest = hashlib.sha256()
            try:
                with http.stream("GET", item.url) as reply:
                    if reply.status_code >= 400:
                        raise ProviderError(
                            f"Hugging Face answered HTTP {reply.status_code} for {item.path}"
                        )
                    with open(part, "wb") as out:
                        for piece in reply.iter_bytes(1 << 20):
                            if stopped():
                                raise DownloadStopped()
                            out.write(piece)
                            digest.update(piece)
                            done += len(piece)
                            if done > total + (1 << 20):
                                raise ProviderError(f"{item.path} is larger than it should be")
                            if progress is not None:
                                progress(done, total)
            except httpx.HTTPError as exc:
                part.unlink(missing_ok=True)
                raise ProviderError(f"Couldn't download {item.path}: {exc}") from exc
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            if digest.hexdigest() != item.sha256 or part.stat().st_size != item.size:
                part.unlink(missing_ok=True)
                raise ProviderError(
                    f"{item.path} came down different from the file it should be (its sha256 "
                    "doesn't match), so it wasn't kept"
                )
            os.replace(part, folder / item.name)
    finally:
        if owned:
            http.close()
    return folder


def _ready(path: Path, item: ModelFile) -> bool:
    return path.is_file() and path.stat().st_size == item.size


def remove(root: Path | None = None) -> None:
    """Take the model off this computer (Settings → Lore)."""
    folder = model_dir(root)
    for item in FILES:
        (folder / item.name).unlink(missing_ok=True)
        (folder / f"{item.name}.part").unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass
    with _LOAD_LOCK:
        _LOADED.pop(str(folder), None)


# The loaded model, by folder, shared by every session of the process.
_LOADED: dict[str, tuple[Any, Any]] = {}
_LOAD_LOCK = threading.Lock()


# onnxruntime's Windows wheels need Microsoft's C++ runtime (msvcp140.dll),
# which Python doesn't bring and a fresh Windows 11 doesn't have: the lab's
# VM, Nov 20 2026, where the import failed with "DLL load failed".
VC_RUNTIME = (
    "On Windows it needs Microsoft's Visual C++ Redistributable (x64), which this computer "
    "doesn't have. Install it (in PowerShell: winget install -e --id "
    "Microsoft.VCRedist.2015+.x64 --source winget) and start SealedLore again"
)


def runtime_problem(exc: BaseException) -> str:
    """What to say when onnxruntime or tokenizers won't load here."""
    text = str(exc).strip()
    if sys.platform == "win32" and "DLL load failed" in text:
        return f"the built-in embeddings model can't start. {VC_RUNTIME}."
    first = text.splitlines()[0] if text else type(exc).__name__
    return f"the built-in embeddings model can't start here: {first}"


def _model(folder: Path) -> tuple[Any, Any]:
    with _LOAD_LOCK:
        found = _LOADED.get(str(folder))
        if found is None:
            try:
                import onnxruntime
                from tokenizers import Tokenizer
            except ImportError as exc:
                raise ProviderError(runtime_problem(exc)) from exc

            tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
            tokenizer.enable_truncation(MAX_TOKENS)
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            session = onnxruntime.InferenceSession(
                str(folder / "model_int8.onnx"), options, providers=["CPUExecutionProvider"]
            )
            found = (tokenizer, session)
            _LOADED[str(folder)] = found
        return found


class LocalEmbeddings:
    """The built-in bge-m3, as an embeddings backend (`EmbeddingBackend`)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self._lock = threading.Lock()

    def close(self) -> None:
        """Nothing to close: the model stays loaded for the next story."""

    def embed(self, texts: Sequence[str], *, as_query: bool = False) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=(), model=BUILTIN_EMBEDDING_MODEL, dimensions=0)
        folder = model_dir(self.root)
        if not installed(self.root):
            raise ProviderError(
                "the built-in embeddings model isn't on this computer yet: download it in "
                "Settings → Lore"
            )
        call = CALLS.start(
            EMBEDDINGS,
            BUILTIN_EMBEDDING_MODEL,
            endpoint="This computer",
            address="",
            protection=ON_THIS_COMPUTER,
        )
        try:
            with self._lock:
                vectors, tokens = self._vectors(folder, list(texts))
        except Exception as exc:
            CALLS.finish(call, "failed", detail=str(exc).splitlines()[0] if str(exc) else "")
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"the built-in embeddings model failed: {exc}") from exc
        CALLS.finish(
            call,
            "done",
            detail=f"{len(texts)} text{'s' if len(texts) != 1 else ''}",
            tokens_in=tokens,
            cost=0.0,
        )
        return EmbeddingResult(
            vectors=tuple(vectors),
            model=BUILTIN_EMBEDDING_MODEL,
            dimensions=len(vectors[0]) if vectors else 0,
            usage=Usage(prompt_tokens=tokens, cost=0.0, cost_reported=True),
        )

    def _vectors(self, folder: Path, texts: list[str]) -> tuple[list[tuple[float, ...]], int]:
        import numpy

        tokenizer, session = _model(folder)
        encoded = tokenizer.encode_batch(texts)
        order = sorted(range(len(texts)), key=lambda i: len(encoded[i].ids))
        vectors: list[tuple[float, ...] | None] = [None] * len(texts)
        for start in range(0, len(order), BATCH):
            chunk = order[start : start + BATCH]
            width = max(len(encoded[i].ids) for i in chunk)
            ids = numpy.full((len(chunk), width), PAD_ID, dtype=numpy.int64)
            mask = numpy.zeros((len(chunk), width), dtype=numpy.int64)
            for row, index in enumerate(chunk):
                tokens = encoded[index].ids
                ids[row, : len(tokens)] = tokens
                mask[row, : len(tokens)] = 1
            hidden = session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
            first = hidden[:, 0, :]
            first = first / numpy.linalg.norm(first, axis=1, keepdims=True)
            for row, index in enumerate(chunk):
                vectors[index] = tuple(float(v) for v in first[row])
        tokens = sum(len(e.ids) for e in encoded)
        return [v for v in vectors if v is not None], tokens


def make_embeddings(settings: Any, root: Path | None = None) -> Any:
    """The embeddings backend `settings` (a resolved `EmbeddingProviderConfig`)
    names: the built-in model, or an endpoint's client."""
    from sealedlore.providers.embeddings import OpenAICompatibleEmbeddings

    if getattr(settings, "builtin", False):
        return LocalEmbeddings(root)
    return OpenAICompatibleEmbeddings(settings)
