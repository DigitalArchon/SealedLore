"""The review of two features before 3.5.0b1 (Nov 23 2026): one character's
answer asked for again (engine/session_reactions.py `regenerate_reaction`),
and lore from the full entry (engine/lore_whole_back.py, its vectors made
ahead in engine/session_lore_vectors.py). Each test here failed on the code as
it was. The mock provider and mock embeddings: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine.model_tuning import sends_lore_from_full

# --- which models are Claude ------------------------------------------------------

# Every spelling a Claude model is served under: Anthropic's own and NanoGPT's
# bare ids, OpenRouter's maker prefix, Bedrock's region and "anthropic." before
# the id, a router before that.
CLAUDE_SPELLINGS = [
    "claude-sonnet-4-6",
    "Anthropic::claude-opus-5-5",
    "anthropic/claude-sonnet-4.6:thinking",
    "openrouter/anthropic/claude-sonnet-4.6",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "Bedrock::us.anthropic.claude-opus-4-1-20250805-v1:0",
]
# Named for Claude, and not it: a model of someone else's, or one made from
# another model, on a server of one's own.
NOT_CLAUDE = [
    "Claude-Opus-Distill-Qwen3",
    "Local::Claude-Opus-Distill-Qwen3",
    "LM Studio::someone/Claude-Opus-Distill-Qwen3-GGUF",
    "someone/claude-sonnet-4-6",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "bedrock/meta.claude-like-70b",
    "claude-3.7-sonnet-reasoning-gemma3-12b",
]


@pytest.mark.parametrize("model", CLAUDE_SPELLINGS)
def test_claude_is_known_by_every_spelling_it_is_served_under(model: str) -> None:
    assert not sends_lore_from_full(model)


@pytest.mark.parametrize("model", NOT_CLAUDE)
def test_a_model_only_named_for_claude_is_not_claude(model: str) -> None:
    assert sends_lore_from_full(model)
