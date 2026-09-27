"""Wire rendering and cache_control placement. See §6.2."""

from __future__ import annotations

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.providers.wire import should_use_cache_control, to_wire_messages

PATTERNS = ["anthropic/", "claude-", "claude/"]

MESSAGES = [
    PromptMessage(
        role="system",
        parts=(
            ContentPart(text="rules"),
            ContentPart(text="cast", cache_breakpoint=True),
        ),
    ),
    PromptMessage(role="user", parts=(ContentPart(text="the turn"),)),
]


def test_plain_string_content_for_non_anthropic_models():
    wire = to_wire_messages(MESSAGES, use_cache_control=False)
    assert wire == [
        {"role": "system", "content": "rules\n\ncast"},
        {"role": "user", "content": "the turn"},
    ]


def test_block_form_marks_only_the_breakpoint_parts():
    wire = to_wire_messages(MESSAGES, use_cache_control=True)
    assert wire[0]["content"] == [
        {"type": "text", "text": "rules"},
        {"type": "text", "text": "\n\ncast", "cache_control": {"type": "ephemeral"}},
    ]
    assert wire[1]["content"] == [{"type": "text", "text": "the turn"}]


def test_blocks_read_as_the_plain_text_does():
    # Anthropic joins blocks with nothing between them, so the separator has
    # to be in the blocks: both forms must reach the model as the same text.
    blocks = to_wire_messages(MESSAGES, use_cache_control=True)
    plain = to_wire_messages(MESSAGES, use_cache_control=False)
    for block_message, plain_message in zip(blocks, plain, strict=True):
        assert "".join(b["text"] for b in block_message["content"]) == plain_message["content"]


def test_a_new_part_leaves_the_blocks_before_it_unchanged():
    longer = [
        PromptMessage(role="system", parts=(*MESSAGES[0].parts, ContentPart(text="summaries"))),
        MESSAGES[1],
    ]
    before = to_wire_messages(MESSAGES, use_cache_control=True)[0]["content"]
    after = to_wire_messages(longer, use_cache_control=True)[0]["content"]
    assert after[: len(before)] == before


def test_cache_control_markers_are_not_shared_between_blocks():
    wire = to_wire_messages(MESSAGES, use_cache_control=True)
    marker = wire[0]["content"][1]["cache_control"]
    marker["type"] = "mutated"
    again = to_wire_messages(MESSAGES, use_cache_control=True)
    assert again[0]["content"][1]["cache_control"] == {"type": "ephemeral"}


def test_auto_mode_detects_anthropic_models_by_prefix():
    assert should_use_cache_control("anthropic/claude-sonnet-4.5", patterns=PATTERNS) is True
    assert should_use_cache_control("claude-sonnet-4.5", patterns=PATTERNS) is True
    assert should_use_cache_control("Claude-Opus-4.1", patterns=PATTERNS) is True
    assert should_use_cache_control("openai/gpt-5", patterns=PATTERNS) is False
    assert should_use_cache_control("llama-3.3-70b", patterns=PATTERNS) is False


def test_manual_override_wins_either_way():
    assert should_use_cache_control("openai/gpt-5", mode="on", patterns=PATTERNS) is True
    assert should_use_cache_control("claude-sonnet-4.5", mode="off", patterns=PATTERNS) is False


def test_auto_mode_without_patterns_never_caches():
    assert should_use_cache_control("claude-sonnet-4.5", patterns=[]) is False


def test_a_one_hour_cache_marks_every_breakpoint_and_sends_the_beta_header():
    from sealedlore.providers.base import ChatRequest
    from sealedlore.providers.wire import EXTENDED_TTL_HEADERS, build_chat_payload, cache_headers

    messages = [
        PromptMessage(role="system", parts=(ContentPart(text="rules", cache_breakpoint=True),)),
        PromptMessage(role="user", parts=(ContentPart(text="turn"),)),
    ]
    hour = ChatRequest(
        model="anthropic/claude", messages=messages, use_cache_control=True, cache_ttl="1h"
    )
    block = build_chat_payload(hour)["messages"][0]["content"][0]
    assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert cache_headers(hour) == EXTENDED_TTL_HEADERS

    five = ChatRequest(model="anthropic/claude", messages=messages, use_cache_control=True)
    assert build_chat_payload(five)["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert cache_headers(five) == {}
    # No caching, no header, whatever the lifetime says.
    plain = ChatRequest(model="z-ai/glm", messages=messages, cache_ttl="1h")
    assert cache_headers(plain) == {}
