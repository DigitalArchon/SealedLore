"""Client for nano-gpt's /decisions endpoint (TypeSafe Jev, a "System One" model).

Jev answers typed questions about a state with probabilities instead of
writing text: a Noul question ("the next passage will need this entry") comes
back as a number from 0 to 1. Questions run in isolation against the same
state, so asking one per lore entry costs input tokens only ($0.042 per
million; output is free) and ~0.8s however many there are. Measured against
a judged lore test set.

Only nano-gpt serves it among OpenAI-compatible hosts, so it rides on the chat
provider's endpoint and key; anything else answers 404, and the caller falls
back to similarity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from sealedlore.providers.base import ProviderError
from sealedlore.providers.http import make_client


@dataclass(frozen=True)
class Decisions:
    """Each Noul question's probability, by the question's key."""

    probabilities: dict[str, float]
    model: str
    raw_usage: dict[str, Any] = field(default_factory=dict)


def build_decisions_payload(model: str, state: str, questions: Mapping[str, str]) -> dict[str, Any]:
    """One Noul question per key; the value is its instructions."""
    return {
        "model": model,
        "state": state,
        "questions": {
            key: {"type": "noul", "instructions": text} for key, text in questions.items()
        },
    }


def parse_decisions(body: dict[str, Any], *, expected: set[str]) -> dict[str, float]:
    """The probabilities, or an error if any question went unanswered."""
    answers = body.get("answers")
    if not isinstance(answers, dict):
        raise ProviderError("the decisions reply held no answers")
    found: dict[str, float] = {}
    for key, answer in answers.items():
        value = answer.get("noul") if isinstance(answer, dict) else None
        if key in expected and isinstance(value, (int, float)):
            found[key] = float(value)
    missing = expected - set(found)
    if missing:
        raise ProviderError(f"the decisions reply left {len(missing)} questions unanswered")
    return found


class DecisionsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/decisions"

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(self.base_url, timeout=httpx.Timeout(self.timeout_seconds))
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def decide(self, model: str, state: str, questions: Mapping[str, str]) -> Decisions:
        payload = build_decisions_payload(model, state, questions)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self._get_client().post(self.endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.endpoint} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.endpoint} returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.endpoint} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ProviderError(f"{self.endpoint} returned {type(body).__name__}")
        usage = body.get("usage")
        return Decisions(
            probabilities=parse_decisions(body, expected=set(questions)),
            model=str(body.get("model") or model),
            raw_usage=usage if isinstance(usage, dict) else {},
        )
