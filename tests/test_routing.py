"""NanoGPT's host for each role's model (models/route.py, engine/routing.py).

A route is the request's `provider` object; the subscription's routing
sends none. Only on NanoGPT, never for a TEE or encrypted model, never in a
private scene, and a host chosen for one model never applies to another.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.routing import config_route, describe, routable, route_body
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.route import ROUTE_ROLES, ModelRoute
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle

NANO = "https://nano-gpt.com/api/v1"
GLM = "z-ai/glm-5.3"
FAST = ModelRoute(priority="latency")


def config(base_url: str = NANO, **changes) -> Config:
    return Config(
        providers=[ProviderConfig(name="p", base_url=base_url, model="z-ai/glm-5.3")],
        active_provider_name="p",
        min_cacheable_tokens=1,
        **changes,
    )


# --- the rules ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "body"),
    [
        (None, {}),
        (ModelRoute(), {}),
        (FAST, {"provider": {"sort": "latency", "min_quantization": "fp8"}}),
        (ModelRoute(priority="speed", fp8=False), {"provider": {"sort": "speed"}}),
        (ModelRoute(priority="price"), {"provider": {"sort": "price", "min_quantization": "fp8"}}),
        (
            ModelRoute(priority="host", host="parasail", host_model="z-ai/glm-5.3"),
            {"provider": {"order": ["parasail"], "min_quantization": "fp8"}},
        ),
        # A host is one model's: on another it is the subscription's routing.
        (ModelRoute(priority="host", host="parasail", host_model="moonshotai/kimi-k2.6"), {}),
    ],
)
def test_a_route_is_the_provider_object_nanogpt_reads(route, body):
    assert route_body(route, "z-ai/glm-5.3") == body


def test_only_nanogpt_models_that_arent_tee_have_routes():
    assert routable(NANO, "z-ai/glm-5.3")
    assert not routable("https://openrouter.ai/api/v1", "z-ai/glm-5.3")
    assert not routable(NANO, "TEE/glm-5.3") and not routable(NANO, "private/glm-5.3")
    assert not routable(NANO, "  ")


def test_a_route_says_what_it_is_and_that_it_is_paid():
    assert describe(None) == "Subscription routing"
    assert describe(FAST) == "Fastest first word · FP8+ · paid"
    host = ModelRoute(priority="host", host="parasail", fp8=False)
    assert describe(host, host_name="Parasail") == "Parasail · any precision · paid"


def test_every_role_falls_back_as_its_model_does():
    """A blank role uses its fallback's model, and so that role's route."""
    cfg = config(model_routes={"story": FAST}, lore_model="")  # lore has a model by default
    for role in ROUTE_ROLES:
        assert config_route(cfg, role, "z-ai/glm-5.3") == route_body(FAST, "z-ai/glm-5.3"), role
    cfg.scene_model = "deepseek/deepseek-v4.1-flash"  # the scene has its own, unrouted
    assert config_route(cfg, "scene", "deepseek/deepseek-v4.1-flash") == {}
    assert config_route(cfg, "plot", "deepseek/deepseek-v4.1-flash") == {}, "plot → scene"
    assert config_route(cfg, "lore", "deepseek/deepseek-v4.1-flash") == {}, "lore → scene"
    assert config_route(cfg, "authoring", "z-ai/glm-5.3") != {}, "authoring → story"


# --- in play ------------------------------------------------------------------------


MODELS = {
    "story": "m/story",
    "summarisation": "m/summary",
    "scene": "m/scene",
    "authoring": "m/authoring",
    "image_prompt": "m/picture-writer",
}
ROUTES = {
    "story": ModelRoute(priority="latency"),
    "summarisation": ModelRoute(priority="speed"),
    "scene": ModelRoute(priority="price"),
    "authoring": ModelRoute(priority="latency", fp8=False),
    "image_prompt": ModelRoute(priority="host", host="morph", host_model="m/picture-writer"),
}


def session_with_routes(tmp_path: Path, story: Story, cast, **changes) -> tuple:
    cfg = config(
        summarization_model=MODELS["summarisation"],
        scene_model=MODELS["scene"],
        authoring_model=MODELS["authoring"],
        image_prompt_model=MODELS["image_prompt"],
        model_routes=ROUTES,
        **changes,
    )
    story.defaults.main_model = MODELS["story"]
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    provider = MockChatProvider([f"Passage {i}." for i in range(40)])
    session = StorySession(
        bundle,
        cfg,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    return session, provider


def routes_sent(provider: MockChatProvider) -> dict[str, list]:
    found: dict[str, list] = {}
    for payload in provider.payloads:
        found.setdefault(payload["model"], []).append(payload.get("provider"))
    return found


def test_each_call_carries_its_roles_route(tmp_path: Path, story: Story, cast):
    session, provider = session_with_routes(tmp_path, story, cast)
    session.story.held_character_id = cast[0].id
    turn = TurnRequest(speaker_id=cast[0].id, user_text="Serrik lifts the lantern.")
    list(session.send(turn))  # the passage, then the scene read
    session.ask("Why did Maela go quiet?")
    session.archive(session.path()[:2])  # a chapter, on the summariser
    with pytest.raises(ValueError):  # the mock's reply isn't a prompt; the call went out
        session.write_image_prompt("the lantern")
    sent = routes_sent(provider)
    for role, model in MODELS.items():
        if role == "authoring":
            continue  # the review; its site is held by the test below
        assert model in sent, f"no {role} call"
        expected = route_body(ROUTES[role], model).get("provider")
        assert all(body == expected for body in sent[model]), (role, sent[model])


def test_no_route_off_nanogpt_or_in_a_private_scene(tmp_path: Path, story: Story, cast):
    session, provider = session_with_routes(tmp_path, story, cast)
    session.config.providers[0].base_url = "https://openrouter.ai/api/v1"
    assert all(session.route_for(role) == {} for role in ROUTE_ROLES)
    session.config.providers[0].base_url = NANO
    assert session.route_for("scene") != {}
    session.enter_private(keep="memory", provider=MockChatProvider(["Quiet."]), model="local/m")
    assert all(session.route_for(role) == {} for role in ROUTE_ROLES)


def test_a_chat_has_its_own_route_and_a_tee_chat_none(tmp_path: Path):
    story = Story(title="Tutor", mode="chat", chat_prompt="Be brief.")
    story.defaults.main_model = "z-ai/glm-5.3"
    story.defaults.main_route = ModelRoute(priority="price")
    cfg = config(model_routes={"story": FAST})
    session = StorySession(StoryBundle(story=story), cfg, MockChatProvider(["Hi."]), root=tmp_path)
    assert session.route_for("story") == route_body(story.defaults.main_route, "z-ai/glm-5.3")
    assert session.route_for("summarisation") == session.route_for("story")
    story.defaults.main_model = "TEE/glm-5.3"
    assert all(session.route_for(role) == {} for role in ROUTE_ROLES)


# --- every request is routed ------------------------------------------------------------

ENGINE = Path(__file__).resolve().parent.parent / "src" / "sealedlore" / "engine"
# Requests that are never routed: the private model's own.
UNROUTED = {
    ("session_private.py", "_condense"),
    ("session_private.py", "summarise_private"),
}


def test_every_request_the_engine_builds_names_its_route():
    """A new call site must say whose route it takes, or it silently goes on
    the subscription's routing whatever the author chose."""
    missing = []
    for path in sorted(ENGINE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(function):
                if not (
                    isinstance(call, ast.Call) and getattr(call.func, "id", "") == "ChatRequest"
                ):
                    continue
                if (path.name, function.name) in UNROUTED:
                    continue
                if not any(keyword.arg == "extra_body" for keyword in call.keywords):
                    missing.append(f"{path.name}:{call.lineno} ({function.name})")
    assert not missing, "requests with no route: " + ", ".join(sorted(set(missing)))


# --- was the route followed? -------------------------------------------------------------

from sealedlore.engine.routing import HostPrice, check_route  # noqa: E402
from sealedlore.models.node import Usage  # noqa: E402

PARASAIL = HostPrice(input=1.47, output=4.62, cache_read=0.27)
HOST = {"order": ["parasail"], "min_quantization": "fp8"}


def test_a_bill_of_nothing_means_the_route_was_dropped():
    """Live (Sept 2026): asked for a host that was down, NanoGPT served the
    call on its own subscription routing and billed nothing, saying nothing."""
    check = check_route(
        GLM, HOST, Usage(prompt_tokens=15, completion_tokens=25, cost_reported=True)
    )
    assert check.met is False and "subscription routing" in check.reason


def test_a_bill_at_the_preferred_hosts_price_is_as_asked():
    # The live Parasail reply: 15 in, 25 out, $0.00013755.
    usage = Usage(prompt_tokens=15, completion_tokens=25, cost=0.00013755, cost_reported=True)
    assert check_route(GLM, HOST, usage, PARASAIL).met is True


def test_a_bill_at_another_price_means_another_host():
    usage = Usage(prompt_tokens=1770, completion_tokens=18, cost=0.001208, cost_reported=True)
    check = check_route(GLM, HOST, usage, PARASAIL)  # that was Nube's price
    assert check.met is False and "another host" in check.reason


def test_cached_tokens_are_priced_as_cached():
    usage = Usage(
        prompt_tokens=10_000, cache_read_tokens=9_000, completion_tokens=100, cost_reported=True
    )
    usage.cost = (1_000 * 1.47 + 9_000 * 0.27 + 100 * 4.62) / 1e6
    assert check_route(GLM, HOST, usage, PARASAIL).met is True


def test_a_sort_is_as_asked_when_billed_and_unknown_without_a_cost():
    fast = {"sort": "latency", "min_quantization": "fp8"}
    billed = Usage(prompt_tokens=10, completion_tokens=10, cost=0.0001, cost_reported=True)
    assert check_route(GLM, fast, billed).met is True
    assert check_route(GLM, fast, Usage(prompt_tokens=10)).met is None


def test_the_session_hears_of_each_routed_reply(tmp_path: Path, story: Story, cast):
    session, provider = session_with_routes(tmp_path, story, cast)
    provider.usage = Usage(prompt_tokens=100, completion_tokens=20, cost_reported=True)  # $0
    session.story.held_character_id = cast[0].id
    list(session.send(TurnRequest(speaker_id=cast[0].id, user_text="Serrik waits.")))
    checks = session.route_checks()
    by_model = {check.model: (check, roles) for _at, check, roles in checks}
    story_check, roles = by_model[MODELS["story"]]
    assert story_check.met is False and roles == ["story"]
    assert MODELS["scene"] in by_model, "the scene read reported too"
    # A request with no route reports nothing.
    session.config.model_routes = {}
    before = len(session.route_checks())
    session.ask("Anything?")
    assert len(session.route_checks()) == before
