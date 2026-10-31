"""A story's own models (Story → Setup → Models): blank goes with the settings."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.routing import differing_models, route_body
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import Node
from sealedlore.models.route import ModelRoute
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.library import story_listings
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.scenario import fresh_playthrough

NANO = "https://nano-gpt.com/api/v1"
FAST = ModelRoute(priority="latency")
CHEAP = ModelRoute(priority="price")


def settings(**changes) -> Config:
    return Config(
        providers=[ProviderConfig(name="nano", base_url=NANO, model="m/settings-story")],
        active_provider_name="nano",
        **changes,
    )


def session_of(story: Story, config: Config, root: Path) -> StorySession:
    return StorySession(
        StoryBundle(story=story),
        config,
        MockChatProvider(["A passage."]),
        root=root,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_a_story_with_nothing_of_its_own_follows_the_settings(tmp_path):
    config = settings(scene_model="m/settings-scene", plot_model="m/settings-plot")
    session = session_of(Story(title="T"), config, tmp_path)
    assert session.model == "m/settings-story"
    assert session.scene_model == "m/settings-scene"
    assert session.plot_model == "m/settings-plot"
    config.providers[0].model = "m/later"
    config.scene_model = "m/later-scene"
    assert session.model == "m/later" and session.scene_model == "m/later-scene"
    assert session.assembly_options().model == "m/later", "its token estimate is that model's"


def test_a_story_s_own_model_beats_the_settings_for_every_role(tmp_path):
    config = settings(
        summarization_model="s/sum",
        scene_model="s/scene",
        plot_model="s/plot",
        lore_model="s/lore",
        authoring_model="s/auth",
        image_prompt_model="s/pic",
    )
    story = Story(title="T")
    own = story.defaults
    own.main_model, own.summarization_model = "o/story", "o/sum"
    own.scene_model, own.plot_model = "o/scene", "o/plot"
    own.lore_model, own.authoring_model, own.image_prompt_model = "o/lore", "o/auth", "o/pic"
    session = session_of(story, config, tmp_path)
    assert session.model == "o/story"
    assert session.summarization_model == "o/sum"
    assert session.scene_model == "o/scene"
    assert session.plot_model == "o/plot"
    assert session.authoring_model == "o/auth"
    assert session.image_prompt_model == "o/pic"
    for role, model in {
        "story": "o/story",
        "summarisation": "o/sum",
        "scene": "o/scene",
        "plot": "o/plot",
        "lore": "o/lore",
        "authoring": "o/auth",
        "image_prompt": "o/pic",
    }.items():
        assert session.role_model(role) == model, role


def test_blank_roles_fall_back_through_the_story_s_own_as_through_the_settings(tmp_path):
    story = Story(title="T")
    story.defaults.scene_model = "o/scene"
    session = session_of(story, settings(lore_model=""), tmp_path)
    assert session.plot_model == "o/scene", "plot falls back to the scene"
    assert session.role_model("lore") == "o/scene"
    assert session.role_model("summarisation") == "m/settings-story"


def test_a_route_goes_with_its_model(tmp_path):
    config = settings(scene_model="m/settings-scene", model_routes={"scene": FAST, "story": FAST})
    story = Story(title="T")
    session = session_of(story, config, tmp_path)
    assert session.route_for("scene") == route_body(FAST, "m/settings-scene")
    # With its own model it has its own route, and none is none: Settings'
    # was chosen for another model.
    story.defaults.scene_model = "o/scene"
    assert session.route_for("scene") == {}
    story.defaults.model_routes["scene"] = CHEAP
    assert session.route_for("scene") == route_body(CHEAP, "o/scene")
    assert session.route_for("story") == route_body(FAST, "m/settings-story")
    story.defaults.main_model = "o/story"
    assert session.route_for("story") == {}


def test_an_old_story_on_the_default_follows_it_and_one_on_another_stays(tmp_path):
    """Every story used to be given the default model when it was made. One
    on another model has been played there, and isn't moved without a word."""
    config = settings(summarization_model="m/sum")
    same = Story(title="same")
    same.defaults.main_model = "m/settings-story"
    same.defaults.summarization_model = "m/sum"
    session = session_of(same, config, tmp_path)
    assert same.defaults.main_model is None and same.defaults.summarization_model is None
    assert session.model == "m/settings-story"
    other = Story(title="other")
    other.defaults.main_model = "m/an-older-default"
    assert session_of(other, config, tmp_path).model == "m/an-older-default"
    assert other.defaults.main_model == "m/an-older-default"


def test_settling_happens_once_so_a_later_choice_of_the_same_model_stands(tmp_path):
    story = Story(title="T")
    config = settings()
    session_of(story, config, tmp_path)
    assert story.defaults.models_settled
    story.defaults.main_model = "m/settings-story"
    session_of(story, config, tmp_path)
    assert story.defaults.main_model == "m/settings-story"
    config.providers[0].model = "m/later"
    assert session_of(story, config, tmp_path).model == "m/settings-story"


def test_a_chat_keeps_its_own_model_when_it_settles(tmp_path):
    story = Story(title="T", mode="chat")
    story.defaults.main_model = "m/settings-story"
    session_of(story, settings(), tmp_path)
    assert story.defaults.main_model == "m/settings-story"


def test_a_restart_keeps_the_story_s_own_models_and_routes(tmp_path):
    story = Story(title="T")
    story.defaults.scene_model = "o/scene"
    story.defaults.model_routes["scene"] = CHEAP
    fresh = fresh_playthrough(StoryBundle(story=story), "Again")
    assert fresh.story.defaults.scene_model == "o/scene"
    assert fresh.story.defaults.model_routes == {"scene": CHEAP}


def test_only_a_model_that_differs_from_the_settings_counts(tmp_path):
    config = settings(summarization_model="s/sum")
    own = {"story": "m/settings-story", "summarisation": "o/sum", "scene": "s/sum"}
    assert differing_models(own, config) == {"summarisation": "o/sum"}, "scene falls back to s/sum"


def test_the_listing_says_what_kind_a_story_is_when_it_was_played_and_its_models(tmp_path):
    plain = Story(id="plain", title="Plain", created_at="2026-01-01T00:00:00+00:00")
    plain.defaults.scene_model = "o/scene"
    nodes = [
        Node(
            id="b", kind="user", speaker_id="s", content="x", created_at="2026-03-01T00:00:00+00:00"
        ),
        Node(
            id="a", kind="user", speaker_id="s", content="y", created_at="2026-02-01T00:00:00+00:00"
        ),
    ]
    save_story_bundle(StoryBundle(story=plain, nodes=nodes), root=tmp_path)
    save_story_bundle(StoryBundle(story=Story(id="chat", title="C", mode="chat")), root=tmp_path)
    listed = {row.id: row for row in story_listings(tmp_path)}
    row = listed["plain"]
    assert row.kind == "Standard" and row.models == {"scene": "o/scene"}
    assert row.started.startswith("2026-02-01") and row.played.startswith("2026-03-01")
    assert row.made.startswith("2026-01-01")
    assert listed["chat"].kind == "Simple" and not listed["chat"].started
