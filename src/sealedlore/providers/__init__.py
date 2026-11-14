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
from sealedlore.providers.privacy import NANOGPT_PRIVATE, NotBuiltProvider, not_built, privacy_for
from sealedlore.providers.wire import should_use_cache_control, to_wire_messages


def make_chat_provider(settings: ProviderConfig) -> ChatProvider:
    """The client that reaches `settings`' model on one endpoint: the one
    place that chooses.

    An end-to-end encrypted model (providers/privacy.py: nano-gpt's
    `private/*`, or any model on a sealed endpoint) is never sent in the
    clear, whatever the endpoint: NanoGPT's gets its sealing client, and one
    whose client this version hasn't sends nothing, saying why
    (`NotBuiltProvider`). Anthropic's own API gets its native client
    (Settings → Endpoints → Speaks, or read from the address); anything else
    speaks OpenAI's /chat/completions. A key saved for another place than the
    endpoint's address now names goes nowhere: its client refuses, saying
    why (`ProviderConfig.sendable`).
    """
    settings = settings.sendable()
    privacy = privacy_for(settings.model, settings)
    if privacy.service == NANOGPT_PRIVATE:
        # Imported here: the attestation code behind it is nothing the
        # other clients need.
        from sealedlore.providers.private_mode import PrivateModeProvider

        return PrivateModeProvider(settings)
    if privacy.sealed:
        return NotBuiltProvider(
            settings,
            not_built(privacy, settings.base_url)
            or f"{settings.model} is end-to-end encrypted, and no client here seals it; "
            "nothing was sent",
        )
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
