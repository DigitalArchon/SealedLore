"""Every model must survive a dump-to-JSON / parse-back cycle unchanged."""

from __future__ import annotations

import json

from sealedlore.models.character import Character, Competence
from sealedlore.models.config import Config, EmbeddingProviderConfig, ProviderConfig
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node, NodeMeta, Roll, Usage
from sealedlore.models.scene import OffstageCharacter, SceneState
from sealedlore.models.story import Story, StoryDefaults, StyleDirectives
from sealedlore.models.summary import Summary


def roundtrip(model):
    cls = type(model)
    data = json.loads(model.model_dump_json())
    return cls.model_validate(data)


def test_node_roundtrip_full():
    node = Node(
        parent_id="parent-uuid",
        children=["child-1", "child-2"],
        kind="assistant",
        speaker_id="__narrator__",
        content="The torch guttered in the draft.",
        ooc=None,
        meta=NodeMeta(
            model="anthropic/claude-3.5-sonnet",
            usage=Usage(
                prompt_tokens=1200,
                completion_tokens=300,
                cache_creation_tokens=800,
                cache_read_tokens=400,
                cost=0.0123,
            ),
            reasoning="considered two outcomes",
            agency_mode="contested",
            roll=Roll(die=100, value=73, band="success_at_a_cost"),
            npc_scope="selected",
            npc_scope_ids=["char-1", "char-2"],
            controlled_character_id="char-0",
            request_log_ref="42",
        ),
    )
    assert roundtrip(node) == node


def test_node_roundtrip_minimal_user_turn():
    node = Node(kind="user", speaker_id="char-0", content="I draw my sword.")
    result = roundtrip(node)
    assert result == node
    assert result.parent_id is None
    assert result.meta.usage.prompt_tokens == 0


def test_character_roundtrip():
    character = Character(
        name="Serrik Vaun",
        aliases=["the Grey Wolf"],
        summary="A veteran duellist with a debt he can't repay.",
        full_description="Long sheet of backstory and mannerisms.",
        voice_notes="Clipped sentences, avoids first person when lying.",
        competence=Competence(
            notes="a veteran duellist, illiterate, hopeless at deception",
            tiers={"combat": "expert", "social": "poor", "arcane": "none"},
        ),
        is_player_available=True,
        portrait_path="/portraits/serrik.png",
    )
    assert roundtrip(character) == character


def test_lore_entry_roundtrip():
    entry = LoreEntry(
        title="The Sundering Accord",
        content="The text injected into the prompt.",
        keywords=["accord", "sundering", "treaty"],
        always_on=False,
        priority=1,
        enabled=True,
        embedding_hash="deadbeef",
    )
    assert roundtrip(entry) == entry


def test_scene_state_roundtrip():
    scene = SceneState(
        location="The undercroft beneath Calder Keep",
        time_of_day="past midnight",
        situation="Torches guttering, something moved in the dark.",
        present_character_ids=["char-1", "char-2"],
        offstage_but_nearby=[
            OffstageCharacter(character_id="char-3", note="in the corridor, out of earshot")
        ],
    )
    assert roundtrip(scene) == scene


def test_story_roundtrip():
    story = Story(
        title="The Sundering",
        defaults=StoryDefaults(
            agency_mode="dice", context_token_budget=32_000, main_model="anthropic/claude-3.5"
        ),
        style=StyleDirectives(
            length_target="roughly 150-250 words, two to four paragraphs",
            person="third",
            tense="past",
            forbidden_phrases=["a shiver ran down"],
        ),
        scene=SceneState(location="Calder Keep"),
        active_leaf_id="node-99",
    )
    assert roundtrip(story) == story


def test_summary_roundtrip():
    summary = Summary(
        covered_node_ids=["n1", "n2", "n3"],
        content="Serrik infiltrated the keep and lost the ledger.",
        model="anthropic/claude-3.5-sonnet",
        hand_edited=True,
        stale=False,
    )
    assert roundtrip(summary) == summary


def test_embedding_cache_entry_roundtrip():
    entry = EmbeddingCacheEntry(
        content_hash="abc123",
        model="text-embedding-3-small",
        dimensions=3,
        vector=[0.1, 0.2, 0.3],
    )
    assert roundtrip(entry) == entry


def test_config_roundtrip():
    config = Config(
        providers=[
            ProviderConfig(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key="sk-test",
                model="anthropic/claude-3.5-sonnet",
            )
        ],
        active_provider_name="openrouter",
        embedding_provider=EmbeddingProviderConfig(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-test",
            model="text-embedding-3-small",
            dimensions=1536,
        ),
        lore_retrieval_k=5,
    )
    assert roundtrip(config) == config


def test_config_defaults_when_empty():
    config = Config()
    assert config.providers == []
    assert config.active_provider_name is None
    assert config.embedding_provider is None
    assert config.lore_retrieval_k == 3
