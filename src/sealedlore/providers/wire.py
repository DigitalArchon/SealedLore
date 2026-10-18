"""Rendering prompt messages to the OpenAI-compatible wire format. See §6.2.

Anthropic prompt caching through an OpenAI-compatible endpoint needs content
in block form with `cache_control` markers. Non-Anthropic models get plain
string content, because some endpoints reject block form outright.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sealedlore.messages import PART_JOINER, PromptMessage
from sealedlore.models.config import CacheControlMode, split_model

if TYPE_CHECKING:
    from sealedlore.providers.base import ChatRequest

CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}
# A one-hour cache needs the ttl on every marker *and* this header: live on
# nano-gpt (Sept 2026), the marker alone was billed as a 1h write and gone
# after 6.5 minutes; with the header it was read back at 6.5 minutes. Their
# `promptCaching` body option failed outright ("all_fallbacks_failed").
EXTENDED_TTL_HEADERS = {"anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11"}


def cache_marker(ttl: str) -> dict[str, str]:
    return (
        {**CACHE_CONTROL_EPHEMERAL, "ttl": "1h"} if ttl == "1h" else dict(CACHE_CONTROL_EPHEMERAL)
    )


def cache_headers(request: ChatRequest) -> dict[str, str]:
    """Request headers a cache setting needs; none for the default five minutes."""
    if request.use_cache_control and request.cache_ttl == "1h":
        return dict(EXTENDED_TTL_HEADERS)
    return {}


def should_use_cache_control(
    model: str, *, mode: CacheControlMode = "auto", patterns: Sequence[str] = ()
) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    # The bare id: a reference may name its endpoint (models/config.py).
    lowered = split_model(model)[1].lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def to_wire_messages(
    messages: Sequence[PromptMessage], *, use_cache_control: bool, ttl: str = "5m"
) -> list[dict[str, Any]]:
    if not use_cache_control:
        return [
            {"role": message.role, "content": message.text}
            if not any(part.image for part in message.parts)
            else {"role": message.role, "content": _with_pictures(message)}
            for message in messages
        ]

    # Anthropic concatenates a message's text blocks with nothing between them
    # (measured on Sonnet 4.6: two blocks cost exactly the tokens of the glued
    # string), so without a separator every section ran into the last —
    # "…Elena Ruiz# TURN DIRECTIVES", one history turn straight into the next.
    # It goes at the *start* of each later block, never the end of an earlier
    # one: adding a part must not change a byte of the parts before it, or the
    # cached prefix dies with it.
    wire: list[dict[str, Any]] = []
    for message in messages:
        blocks: list[dict[str, Any]] = []
        for index, part in enumerate(message.parts):
            text = part.text if index == 0 else PART_JOINER + part.text
            block: dict[str, Any] = {"type": "text", "text": text}
            if part.cache_breakpoint:
                block["cache_control"] = cache_marker(ttl)
            if part.text or not part.image:
                blocks.append(block)
            if part.image:
                blocks.append(_picture_block(part.image))
        wire.append({"role": message.role, "content": blocks})
    return wire


def _picture_block(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def _with_pictures(message: PromptMessage) -> list[dict[str, Any]]:
    """A message with pictures as content blocks, its text as one string
    would be (the parts joined) with each picture after its part's text."""
    blocks: list[dict[str, Any]] = []
    for index, part in enumerate(message.parts):
        text = part.text if index == 0 else PART_JOINER + part.text
        if part.text:
            blocks.append({"type": "text", "text": text})
        if part.image:
            blocks.append(_picture_block(part.image))
    return blocks


def without_pictures(payload: Any) -> Any:
    """A request body as the API log keeps it: every picture sent as data
    (a data URL, or Anthropic's base64 source) replaced by a note of its
    size, as the picture requests' references are logged by name, never
    their bytes. The body sent is left as it is."""
    if isinstance(payload, dict):
        if payload.get("type") == "base64" and isinstance(payload.get("data"), str):
            kept = dict(payload)
            kept["data"] = f"[a picture, {len(payload['data']) * 3 // 4 // 1024} KB]"
            return kept
        return {key: without_pictures(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [without_pictures(value) for value in payload]
    if isinstance(payload, str) and payload.startswith("data:image/"):
        return f"[a picture, {len(payload) * 3 // 4 // 1024} KB]"
    return payload


def build_chat_payload(
    request: ChatRequest, *, provider_extra_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The /chat/completions request body.

    Shared by the real client and the mock so that the context inspector and
    the API log show the same payload either way.
    """
    params = request.params
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": to_wire_messages(
            request.messages, use_cache_control=request.use_cache_control, ttl=request.cache_ttl
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    optional = {
        "temperature": params.temperature,
        "top_p": params.top_p,
        "max_tokens": params.max_tokens,
        "presence_penalty": params.presence_penalty,
        "frequency_penalty": params.frequency_penalty,
        "seed": params.seed,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    if params.stop:
        payload["stop"] = list(params.stop)
    if params.reasoning.enabled:
        reasoning: dict[str, Any] = {"enabled": True}
        if params.reasoning.effort:
            reasoning["effort"] = params.reasoning.effort
        payload["reasoning"] = reasoning
    elif params.reasoning.effort == "none":
        # DeepSeek's "no reasoning at all". An explicit {"enabled": false} is
        # never sent: GLM 5.3 and Opus 5.5 refuse it (engine/reasoning.py).
        payload["reasoning"] = {"effort": "none"}

    payload.update(provider_extra_body or {})
    payload.update(request.extra_body)
    return payload
