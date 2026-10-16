from sealedlore.models.config import Config, ProviderConfig
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.endpoints import EndpointsProvider
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.tee import is_private_mode
from sealedlore.providers.wire import should_use_cache_control, to_wire_messages


def make_chat_provider(settings: ProviderConfig) -> ChatProvider:
    """The client that reaches `settings`' model on one endpoint: the one
    place that chooses.

    An end-to-end encrypted model (nano-gpt's `private/*`) is never sent in
    the clear, whatever the endpoint; Anthropic's own API gets its native
    client (Settings → Endpoints → Speaks, or read from the address); anything
    else speaks OpenAI's /chat/completions.
    """
    if is_private_mode(settings.model):
        # Imported here: the attestation code behind it is nothing the
        # other clients need.
        from sealedlore.providers.private_mode import PrivateModeProvider

        return PrivateModeProvider(settings)
    if settings.speaks() == "anthropic":
        return AnthropicProvider(settings)
    return OpenAICompatibleProvider(settings)


def make_session_provider(config: Config) -> EndpointsProvider:
    """The one provider a session, the CLI or a speed test holds: it sends
    each request to the endpoint its model names (providers/endpoints.py),
    making that endpoint's client through `make_chat_provider` on first use."""
    return EndpointsProvider(config)


__all__ = [
    "AnthropicProvider",
    "ChatProvider",
    "ChatRequest",
    "EndpointsProvider",
    "MockChatProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ReasoningDelta",
    "StreamCompleted",
    "StreamEvent",
    "TextDelta",
    "make_chat_provider",
    "make_session_provider",
    "parse_usage",
    "should_use_cache_control",
    "to_wire_messages",
]
