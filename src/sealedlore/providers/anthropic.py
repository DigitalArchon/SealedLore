"""Streaming client for Anthropic's own Messages API.

The app's prompt caching rests on `cache_control` blocks and the one-hour
lifetime (wire.py), which the native API gives us control of where the
OpenAI-compatibility layer does not. The request is `POST /v1/messages`:
the system block as a list of text blocks, the history as alternating user
and assistant messages of text blocks, each breakpoint's marker on the block
it was assigned to, `max_tokens` on every request, and extended thinking
shaped for the model's generation (providers/anthropic_models.py). The
reply is Anthropic's SSE: message_start (the input usage), content blocks
of text or thinking as deltas, message_delta (the stop reason and output
usage), message_stop.

What the OpenAI client has and this one hasn't: prices (Anthropic's listing
names none, so every call is unpriced, never $0), NanoGPT's routes and
reasoning defaults (what each model does unasked is stated here instead,
from Anthropic's docs), and Private Mode. The HTTP machinery (one client,
cancel, retries, siblings) is `providers/streaming.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from sealedlore.messages import PART_JOINER, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.anthropic_models import (
    listing_entry,
    reasoning_default,
    surface_of,
    thinking_fields,
)
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.streaming import StreamingHttpProvider
from sealedlore.providers.wire import cache_marker

API_VERSION = "2023-06-01"
# A one-hour cache marker (`Config.cache_ttl`) needs this beta header on the
# OpenAI-compatible route (wire.py, measured on nano-gpt); Anthropic's own
# docs now show the ttl working without it, and the header is still accepted,
# so it is sent whenever a marker carries the hour.
EXTENDED_TTL_BETA = "extended-cache-ttl-2025-04-11"
SSE_DATA_PREFIX = "data:"
# The listing is paged; a cap against a page that never says it is the last.
MAX_LISTING_PAGES = 10
LISTING_PAGE = 100

# Anthropic's stop reasons as the app names them. Anything else (a refusal,
# a pause) is kept by its own name: the record says what happened.
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


def origin(base_url: str) -> str:
    """The API's root, with or without the /v1 the author may have typed."""
    root = base_url.strip().rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def render_messages(
    messages: Sequence[PromptMessage], *, use_cache_control: bool, ttl: str = "5m"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The `system` blocks and the `messages` of a Messages API request.

    As `wire.to_wire_messages`: a message's parts are text blocks, every
    block but a sequence's first leads with PART_JOINER (Anthropic joins
    blocks with nothing between them, and a separator at the start of a
    later block never changes the bytes of an earlier one), and a breakpoint
    part carries its cache marker. Unlike the OpenAI shape, the API wants
    one `system`, user and assistant turns strictly alternating, so every
    system message's blocks go together and consecutive messages of one role
    are one message. An empty part makes no block (the API refuses an empty
    text block); a marker on one moves to the block before it.
    """
    system: list[dict[str, Any]] = []
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            blocks = system
        elif wire and wire[-1]["role"] == message.role:
            blocks = wire[-1]["content"]
        else:
            wire.append({"role": message.role, "content": []})
            blocks = wire[-1]["content"]
        for part in message.parts:
            marker = cache_marker(ttl) if use_cache_control and part.cache_breakpoint else None
            if part.text:
                text = part.text if not blocks else PART_JOINER + part.text
                block: dict[str, Any] = {"type": "text", "text": text}
                if marker is not None:
                    block["cache_control"] = marker
                blocks.append(block)
            elif marker is not None and blocks:
                blocks[-1]["cache_control"] = marker
            if part.image:
                blocks.append(_picture_block(part.image))
    return system, wire


def _picture_block(url: str) -> dict[str, Any]:
    """A picture sent as data (`ContentPart.image`, a data URL) as the
    Messages API takes one."""
    head, _, data = url.partition(",")
    media_type = head.removeprefix("data:").split(";")[0] or "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def build_messages_payload(
    request: ChatRequest, *, provider_extra_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The /v1/messages request body, as sent (the inspector and the API log).

    The app's sampling settings map where the API has them: temperature or
    top_p (one of the two: the API refuses both on Claude 4 and later),
    stop sequences, max_tokens. Presence and frequency penalties and the seed
    have no counterpart and are left out. Sampling is left out beside
    thinking, which the API refuses with it, and altogether on the models
    that refuse it (Opus 4.7 and later, Sonnet 5 and later, Haiku 5.5 and
    later). A NanoGPT route (`extra_body["provider"]`) is never sent.
    """
    params = request.params
    system, messages = render_messages(
        request.messages, use_cache_control=request.use_cache_control, ttl=request.cache_ttl
    )
    thinking = thinking_fields(surface_of(request.model), params.reasoning, params.max_tokens)
    payload: dict[str, Any] = {
        "model": request.model,
        "max_tokens": thinking.max_tokens,
        "messages": messages,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if thinking.sampling:
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        elif params.top_p is not None:
            payload["top_p"] = params.top_p
    if params.stop:
        payload["stop_sequences"] = list(params.stop)
    payload.update(thinking.fields)
    payload.update(provider_extra_body or {})
    payload.update({key: value for key, value in request.extra_body.items() if key != "provider"})
    return payload


def wants_extended_ttl(request: ChatRequest) -> bool:
    return request.use_cache_control and request.cache_ttl == "1h"


def app_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Anthropic's usage with the app's two names beside its own.

    Its `input_tokens` are the uncached tokens alone; the app counts a
    prompt whole, cached tokens included (`prompt_tokens` on nano-gpt and
    OpenRouter), and its token-correction factor and budget read it so. The
    verbatim fields stay for the record.
    """

    def count(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return {
        **raw,
        "prompt_tokens": count("input_tokens")
        + count("cache_creation_input_tokens")
        + count("cache_read_input_tokens"),
        "completion_tokens": count("output_tokens"),
    }


class AnthropicProvider(StreamingHttpProvider):
    def __init__(self, config: ProviderConfig, *, client: httpx.Client | None = None) -> None:
        super().__init__(config, client=client)
        # The listing, for `fetch_reasoning_defaults` to name the same models.
        self._models: dict[str, dict[str, Any]] | None = None

    @property
    def endpoint(self) -> str:
        return origin(self.config.base_url) + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "anthropic-version": API_VERSION}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        return headers

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        return build_messages_payload(request, provider_extra_body=self.config.extra_body)

    def _prepare(self, request: ChatRequest) -> tuple[str, dict[str, Any], dict[str, str]]:
        payload = self.build_payload(request)
        if not payload["messages"] or payload["messages"][0]["role"] != "user":
            raise ProviderError(
                "Anthropic's API needs the conversation to begin with a user message, and "
                "this prompt begins with the model's; nothing was sent"
            )
        headers = {**self._headers(), "accept": "text/event-stream"}
        if wants_extended_ttl(request):
            headers["anthropic-beta"] = EXTENDED_TTL_BETA
        return self.endpoint, payload, headers

    def _http_error(self, url: str, response: httpx.Response) -> ProviderError:
        """As the base's, with the endpoint's own message in the error: a
        refusal says "model: claude-x" or "credit balance is too low"."""
        error = super()._http_error(url, response)
        message = _error_message(error.body)
        if message:
            error.args = (f"{error.args[0]}: {message}",)
        return error

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        url, payload, headers = self._prepare(request)
        usage_raw: dict[str, Any] = {}
        stop_reason: str | None = None
        response_id: str | None = None
        reasoning_chars = 0

        with self._get_client().stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            extensions={"trace": self._trace},
        ) as response:
            if response.status_code >= 400:
                raise self._http_error(url, response)

            for line in response.iter_lines():
                # "event:" lines name what the data line says itself.
                if not line.startswith(SSE_DATA_PREFIX):
                    continue
                data = line[len(SSE_DATA_PREFIX) :].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"malformed stream event: {data[:200]!r}") from exc
                if not isinstance(event, dict):
                    raise ProviderError(f"malformed stream event: {data[:200]!r}")

                kind = event.get("type")
                if kind == "error":
                    error = event.get("error")
                    detail = error if isinstance(error, dict) else {"message": str(error)}
                    raise ProviderError(
                        "Anthropic reported an error: "
                        f"{detail.get('type', 'error')}: {detail.get('message', '')}".rstrip(": ")
                    )
                if kind == "message_start":
                    message = event.get("message")
                    message = message if isinstance(message, dict) else {}
                    if isinstance(message.get("id"), str):
                        response_id = message["id"]
                    if isinstance(message.get("usage"), dict):
                        usage_raw.update(message["usage"])
                elif kind == "content_block_delta":
                    delta = event.get("delta")
                    delta = delta if isinstance(delta, dict) else {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            yield TextDelta(text=text)
                    elif delta.get("type") == "thinking_delta":
                        thinking = delta.get("thinking")
                        if isinstance(thinking, str) and thinking:
                            reasoning_chars += len(thinking)
                            yield ReasoningDelta(text=thinking)
                    # signature_delta carries the thinking block's signature,
                    # which the app never sends back; nothing to keep.
                elif kind == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                        stop_reason = delta["stop_reason"]
                    if isinstance(event.get("usage"), dict):
                        usage_raw.update(event["usage"])
                elif kind == "message_stop":
                    break

        if self._cancelled:
            # Shut down mid-stream, the connection just reads as ended.
            raise StreamCancelled()
        usage_raw = app_usage(usage_raw) if usage_raw else usage_raw
        completed = StreamCompleted(
            usage=parse_usage(usage_raw),
            finish_reason=_FINISH.get(stop_reason, stop_reason) if stop_reason else None,
            raw_usage=usage_raw,
            response_id=response_id,
            reasoning_tokens=reasoning_chars // 4,
        )
        self._report_route(request, completed)
        self._report_reasoning(request, completed)
        yield completed

    # --- the models list ---------------------------------------------------------

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """GET /v1/models, every page: id → entry in the shape the catalog
        reads. "Prices" by the interface's name only: Anthropic lists none."""
        url = origin(self.config.base_url) + "/v1/models"
        listing: dict[str, dict[str, Any]] = {}
        after: str | None = None
        for _page in range(MAX_LISTING_PAGES):
            params: dict[str, Any] = {"limit": LISTING_PAGE}
            if after:
                params["after_id"] = after
            try:
                response = self._get_client().get(
                    url, params=params, headers=self._headers(), timeout=30.0
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"couldn't fetch the models list: {exc}") from exc
            if response.status_code >= 400:
                raise self._http_error(url, response)
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("the models list wasn't JSON") from exc
            items = data.get("data") if isinstance(data, dict) else None
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    listing[item["id"]] = listing_entry(item)
            last = data.get("last_id") if isinstance(data, dict) else None
            if not (isinstance(data, dict) and data.get("has_more") and isinstance(last, str)):
                break
            after = last
        self._models = listing
        return listing

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        """What each listed model does about thinking when a request says
        nothing. Anthropic's listing doesn't state it; its docs do, by
        generation (anthropic_models.py), and that is what the Settings note
        and "as low as possible" need to know."""
        models = self._models if self._models is not None else self.fetch_model_prices()
        return {model: reasoning_default(model) for model in models}


def _error_message(body: str | None) -> str:
    """The `error.message` of an Anthropic error body, or ""."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message.strip() if isinstance(message, str) else ""
