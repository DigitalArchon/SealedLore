"""Embeddings servers on this computer (Settings → Lore, Find a server on this
computer): the usual programs, each asked at the address it starts on for the
models it has, and which of them embed.

Nothing is looked for beyond this computer, and nothing but `/v1/models` is
asked: no key, no text. A server that doesn't answer within a moment isn't
there.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from sealedlore.providers.http import make_client, reply_json

# The programs, where each starts by default, in the order they are offered.
# Programs that start at the same address share one entry, so each address
# is asked once.
KNOWN_SERVERS: tuple[tuple[str, str], ...] = (
    ("Ollama", "http://127.0.0.1:11434/v1"),
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("llama.cpp, LocalAI or Text Embeddings Inference", "http://127.0.0.1:8080/v1"),
    ("vLLM", "http://127.0.0.1:8000/v1"),
)
PROBE_SECONDS = 1.5
# A model that embeds, by its name: the common embedding families.
_EMBEDS = re.compile(
    r"embed|bge|\be5\b|e5-|gte|nomic|minilm|mpnet|mxbai|arctic|jina|snowflake|sentence|granite-embedding",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FoundServer:
    name: str
    base_url: str
    models: tuple[str, ...]

    @property
    def embedding_models(self) -> tuple[str, ...]:
        """Its models that embed, bge-m3 first (the one lore is tuned on)."""
        found = [m for m in self.models if _EMBEDS.search(m)]
        return tuple(sorted(found, key=lambda m: (0 if "bge-m3" in m.lower() else 1, m)))


def find_local_servers(*, get: Callable[[str], httpx.Response] | None = None) -> list[FoundServer]:
    """Every known server answering on this computer, with its models."""
    client = None
    if get is None:
        client = make_client("http://127.0.0.1", timeout=httpx.Timeout(PROBE_SECONDS))
        get = client.get
    found: list[FoundServer] = []
    try:
        for name, base in KNOWN_SERVERS:
            try:
                response = get(base + "/models")
                body = reply_json(response) if response.status_code < 400 else None
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                continue
            models = tuple(
                str(item["id"])
                for item in body["data"]
                if isinstance(item, dict) and item.get("id")
            )
            found.append(FoundServer(name, base, models))
    finally:
        if client is not None:
            client.close()
    return found


def found_words(servers: list[FoundServer]) -> str:
    """What Find a server on this computer says."""
    if not servers:
        return (
            "No server answered on this computer (Ollama, LM Studio, llama.cpp, LocalAI, "
            "Text Embeddings Inference or vLLM at their usual ports). Start one, or put "
            "its address in Base URL."
        )
    lines = []
    for server in servers:
        embeds = server.embedding_models
        if embeds:
            lines.append(f"{server.name} at {server.base_url}: {', '.join(embeds[:4])}")
        elif server.name == "Ollama":
            lines.append(
                f"Ollama at {server.base_url}, with no embedding model: "
                "`ollama pull bge-m3` gets the one lore is tuned on"
            )
        else:
            lines.append(f"{server.name} at {server.base_url}, with no embedding model loaded")
    return "Found: " + "; ".join(lines) + "."
