"""The privacy promises looked over before 3.5.0b1 (the review of Nov 22 2026).

Each test plays what the review found with mock clients only: a marker in a
private scene's messages (or in a chat's card), then everything each client
was sent is searched for it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.private import PrivacyRecord
from sealedlore.providers.base import ProviderError
from sealedlore.providers.mock import MockChatProvider, MockEmbeddings
from sealedlore.providers.privacy import NotBuiltProvider
from sealedlore.storage.repository import load_story_bundle
from tests.test_private import MARKER, build, play, turn

LOCAL = "http://localhost:11434/v1"
ELSEWHERE = "https://api.elsewhere.example/v1"


class At(MockChatProvider):
    """A scene's client as the window makes it: it knows its own endpoint."""

    def __init__(self, settings: ProviderConfig) -> None:
        super().__init__(["A reply."])
        self.config = settings


def private_at(address: str, model: str = "local/model") -> ProviderConfig:
    return ProviderConfig(name="private", base_url=address, model=model)


def disk_scene(tmp_path: Path) -> tuple[StorySession, At, str]:
    """A scene kept on disk on a server on this computer, a message in it."""
    session, _main, held = build(tmp_path)
    play(session, turn(held, "I walk in."))
    settings = session.config.private()
    local = At(settings)
    session.enter_private(
        keep="disk", provider=local, model=settings.model, base_url=settings.base_url
    )
    play(session, turn(held, f"I whisper {MARKER} to her."))
    session.save()
    return session, local, held


def reopened(tmp_path: Path, session: StorySession, now: ProviderConfig) -> tuple[StorySession, At]:
    """The story opened again in a later run, Settings → Private as `now`:
    resumed as the window resumes it, on the scene's own model."""
    config: Config = session.config.model_copy(deep=True)
    config.private_provider = now
    again = StorySession(
        load_story_bundle(session.story.id, root=tmp_path),
        config,
        MockChatProvider(["The story's own model."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
        embeddings=MockEmbeddings(),
    )
    again.recover_private_spans()
    client = At(config.private().model_copy(update={"model": again.open_span.model}))
    again.resume_private(client)
    return again, client


# --- V1: a scene stays on the server it began on, until the author says otherwise -------------


def test_a_plain_disk_scene_reopened_on_another_server_sends_nothing(tmp_path: Path):
    """Begun on a server on this computer, the story closed in it, Settings →
    Private pointed at a server on the internet: the scene's whole history
    went there with the next message, nothing asked."""
    session, local, held = disk_scene(tmp_path)
    record = session.open_span.privacy
    assert (record.kind, record.origin) == ("", "http://localhost:11434")
    session.close()

    again, client = reopened(tmp_path, session, private_at(ELSEWHERE, "some/cloud-model"))
    reason = again.privacy_held()
    assert reason and "http://localhost:11434" in reason
    assert "https://api.elsewhere.example" in reason and "nothing was sent" in reason
    assert again.scene_moved() == ("http://localhost:11434", "https://api.elsewhere.example")
    assert isinstance(again.private_provider, NotBuiltProvider), "a client that sends nothing"
    assert again.tee_block == reason

    with pytest.raises(ProviderError, match="nothing was sent"):
        play(again, turn(held, "And then?"))
    with pytest.raises(ProviderError):
        list(again.regenerate())
    with pytest.raises(ProviderError):
        list(again.ask("What did she whisper?"))
    with pytest.raises(ProviderError):
        list(again.suggest_turns())
    with pytest.raises(ProviderError):
        again.summarise_private()
    with pytest.raises(ProviderError):
        again.write_image_prompt("the two of us")
    assert client.payloads == [] and again.provider.payloads == []
    # Nothing of the turn that was refused stays in the scene.
    assert [node.content for node in again.span_nodes(again.open_span)][0].endswith("to her.")
    assert len(again.span_nodes(again.open_span)) == 2


def test_a_scene_is_carried_on_elsewhere_only_on_the_authors_word_asked_once(tmp_path: Path):
    session, _local, held = disk_scene(tmp_path)
    session.close()
    now = private_at(ELSEWHERE, "some/cloud-model")
    again, held_client = reopened(tmp_path, session, now)
    assert again.privacy_held()

    # The author's yes: a client for the new place, and the scene is recorded there.
    client = At(now.model_copy(update={"model": again.open_span.model}))
    again.carry_scene_on(client)
    span = again.open_span
    assert again.privacy_held() is None and again.scene_moved() is None
    assert (span.privacy.origin, span.host) == (
        "https://api.elsewhere.example",
        "api.elsewhere.example",
    )
    play(again, turn(held, "And then?"))
    sent = client.payloads[-1]
    assert sent["model"] == "local/model", "the scene's own model, as before"
    assert MARKER in json.dumps(sent)
    assert held_client.payloads == []
    again.save()
    again.close()

    # Asked once: the next run finds the scene where it was carried on to.
    third, client3 = reopened(tmp_path, again, now)
    assert third.privacy_held() is None and third.private_provider is client3
    # Set back to where it began, it is another place again: asked again.
    back, _client = reopened(tmp_path, again, private_at(LOCAL))
    assert "https://api.elsewhere.example" in back.privacy_held()


def test_a_held_scene_ends_without_a_summary_with_nothing_sent(tmp_path: Path):
    session, _local, _held = disk_scene(tmp_path)
    session.close()
    again, client = reopened(tmp_path, session, private_at(ELSEWHERE))
    assert again.privacy_held()
    again.discard_private()
    assert not again.in_private and again.privacy_held() is None
    assert client.payloads == [] and again.provider.payloads == []
    assert MARKER not in json.dumps([node.content for node in again.path()])


def test_a_scene_reopened_where_it_began_goes_on_whatever_model_settings_names(tmp_path: Path):
    """Same server, another model in Settings → Private's field: the scene
    goes on, on its own model, with nothing asked (as before)."""
    session, _local, held = disk_scene(tmp_path)
    session.close()
    again, client = reopened(tmp_path, session, private_at(LOCAL, "local/another"))
    assert again.privacy_held() is None and again.private_provider is client
    play(again, turn(held, "And then?"))
    assert client.payloads[-1]["model"] == "local/model"
    # Another path on the same server is the same place.
    other, client2 = reopened(tmp_path, again, private_at("http://localhost:11434/api/v2"))
    assert other.privacy_held() is None and other.private_provider is client2


def test_settings_changed_under_an_open_scene_holds_its_next_request(tmp_path: Path):
    """In the running app: a scene whose client has no endpoint of its own
    goes where Settings → Private points, so a change there mid-scene is the
    same move, held the same way."""
    session, main, held = build(tmp_path)
    play(session, turn(held, "I walk in."))
    private = MockChatProvider([f"A quiet reply that holds {MARKER} close."])
    session.enter_private(keep="disk", provider=private, model="local/model")
    play(session, turn(held, f"I whisper {MARKER} to her."))
    sent, to_main = len(private.payloads), len(main.payloads)

    session.config.private_provider = private_at(ELSEWHERE)
    assert "https://api.elsewhere.example" in session.privacy_held()
    with pytest.raises(ProviderError, match="nothing was sent"):
        play(session, turn(held, "And then?"))
    with pytest.raises(ProviderError):
        session.summarise_private()
    assert len(private.payloads) == sent and len(main.payloads) == to_main
    # Set back, it goes on as it was.
    session.config.private_provider = private_at(LOCAL)
    assert session.privacy_held() is None
    play(session, turn(held, "And then?"))
    assert len(private.payloads) == sent + 1


def test_an_older_scene_with_no_place_recorded_is_asked_about_once(tmp_path: Path):
    """A scene saved before a scene's place was kept (`privacy` None): on the
    host it names, it goes on as before; anywhere else, or with no host
    recorded either, the author is asked once."""
    session, _local, held = disk_scene(tmp_path)
    session.open_span.privacy = None
    session.save()
    session.close()
    same, client = reopened(tmp_path, session, private_at("http://localhost:8080/v1"))
    assert same.privacy_held() is None and same.private_provider is client
    assert same.open_span.privacy.origin == "http://localhost:8080"

    session.open_span.privacy = None
    session.save()
    moved, client2 = reopened(tmp_path, session, private_at(ELSEWHERE))
    assert "localhost" in moved.privacy_held() and client2.payloads == []
    assert isinstance(moved.private_provider, NotBuiltProvider)

    session.open_span.privacy = None
    session.open_span.host = ""
    session.save()
    unknown, client3 = reopened(tmp_path, session, private_at(ELSEWHERE))
    reason = unknown.privacy_held()
    assert reason and "kept no record" in reason and "api.elsewhere.example" in reason
    with pytest.raises(ProviderError):
        play(unknown, turn(held, "And then?"))
    assert client3.payloads == []
    # Yes, once: recorded where it is now.
    unknown.carry_scene_on(At(private_at(ELSEWHERE)))
    assert unknown.privacy_held() is None
    assert unknown.open_span.privacy == PrivacyRecord(origin="https://api.elsewhere.example")


def test_carrying_a_scene_on_never_makes_it_less_private(tmp_path: Path):
    """The author's yes is to another place, never to less privacy: a scene
    begun end-to-end encrypted is not carried on to a plain server by it."""
    session, _main, _held = build(tmp_path)
    began = ProviderConfig(
        name="private", base_url="https://nano-gpt.com/api/v1", model="private/glm-5-3"
    )
    session.config.private_provider = began
    session.enter_private(
        keep="disk", provider=At(began), model=began.model, base_url=began.base_url
    )
    session.save()
    plain = ProviderConfig(name="private", base_url=ELSEWHERE, model="glm-5-3")
    session.open_span.model = "glm-5-3"  # as a Tinfoil model's id: no prefix says it is private
    session.private_provider = At(plain)
    assert session.privacy_held() and session.scene_moved() is None
    with pytest.raises(ValueError):
        session.carry_scene_on(At(plain))
    assert session.open_span.privacy.origin == "https://nano-gpt.com"


# --- V2: a TEE/ scene's looks are chosen by the model that was attested ----------------------


class FakeTee:
    """The scene's attested TEE/ model, as the window sets it on the session."""

    model = "TEE/glm-5.3"

    def __init__(self) -> None:
        self.checked: list[str | None] = []

    def due(self) -> bool:
        return False

    def check_reply(self, response_id: str | None, **_record) -> str:
        self.checked.append(response_id)
        return "verified"

    def close(self) -> None:
        pass


def tee_scene(tmp_path: Path, look_model: str) -> tuple[StorySession, MockChatProvider, str]:
    """A scene on TEE/glm-5.3, attested, two people in it with pictures to
    choose between, and Settings → Private → Model for looks as given."""
    from sealedlore.models.image import ImageRef

    session, _main, held = build(tmp_path)
    for character in session.cast[:2]:
        character.reference_images = [
            ImageRef(file=f"images/refs/{character.id}-a.png", look="coat", expression="neutral"),
            ImageRef(file=f"images/refs/{character.id}-b.png", look="coat", expression="angry"),
        ]
    session.story.scene.present_character_ids = [c.id for c in session.cast[:2]]
    play(session, turn(held, "I walk in."))
    nano = ProviderConfig(
        name="private", base_url="https://nano-gpt.com/api/v1", model="TEE/glm-5.3"
    )
    session.config.private_provider = nano
    session.config.private_look_model = look_model
    private = MockChatProvider([f"A quiet reply that holds {MARKER} close."])
    session.enter_private(keep="memory", provider=private, model=nano.model, base_url=nano.base_url)
    session.private_tee = FakeTee()
    return session, private, held


def test_a_tee_scenes_looks_are_chosen_by_its_own_attested_model(tmp_path: Path):
    """Settings → Private → Model for looks named another TEE/ model: each
    scene passage, and the author's turn with it, went to a model whose
    enclave was never attested and whose reply nothing checked."""
    session, private, held = tee_scene(tmp_path, "TEE/kimi-k3")
    assert session.open_span.privacy.kind == "tee"
    play(session, turn(held, f"I whisper {MARKER} to her."))
    picks = [p for p in private.payloads if "THE LATEST PASSAGE" in json.dumps(p)]
    assert picks, "the looks were chosen"
    assert MARKER in json.dumps(picks[-1]), "the pick reads the scene's passage"
    assert {p["model"] for p in private.payloads} == {"TEE/glm-5.3"}
    assert session._private_look_model(session.open_span) == "TEE/glm-5.3"


def test_an_encrypted_scene_keeps_the_model_chosen_for_its_looks(tmp_path: Path):
    """End-to-end encrypted models are attested by the client that seals to
    them, whichever model is asked: Settings' choice stands there, as before."""
    session, _private, _held = tee_scene(tmp_path, "private/kimi-k3")
    sealed = session.open_span.model_copy(update={"model": "private/glm-5-3"})
    assert session._private_look_model(sealed) == "private/kimi-k3"
    # And never a less private one, as before.
    session.config.private_look_model = "TEE/glm-5.3-flash"
    assert session._private_look_model(sealed) == "private/glm-5-3"
    # A scene on an ordinary private model keeps the setting too.
    plain = session.open_span.model_copy(update={"model": "local/model"})
    session.config.private_look_model = "local/small"
    assert session._private_look_model(plain) == "local/small"


# --- V3: a TEE or encrypted chat's portrait prompts are written by its own model -------------

CARD_SECRET = "CARD-SECRET-ZQX"


def private_chat(tmp_path: Path, model: str) -> tuple[StorySession, MockChatProvider]:
    """A chat on a TEE or end-to-end encrypted model, a character brought in
    for their pictures, and Settings naming another model to write picture
    prompts (and for every other job)."""
    from sealedlore.models.character import Character
    from sealedlore.models.story import Story
    from sealedlore.storage.repository import StoryBundle, save_story_bundle

    story = Story(title="A plan with Mara", mode="chat", chat_prompt="You are helpful.")
    story.defaults.main_model = model
    bundle = StoryBundle(story=story)
    bundle.cast.append(
        Character(
            name="Mara Venn",
            summary="A smuggler.",
            full_description=f"Mara Venn, {CARD_SECRET}, runs guns past the blockade.",
        )
    )
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        image_prompt_model="zai-org/glm-5.3",
        authoring_model="zai-org/glm-5.3",
        scene_model="zai-org/glm-5.3",
        summarization_model="zai-org/glm-5.3",
    )
    main = MockChatProvider(['{"prompts": []}'])
    session = StorySession(
        bundle,
        config,
        main,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    return session, main


@pytest.mark.parametrize("model", ["TEE/glm-5.3-flash", "private/glm-5-3"])
def test_a_private_chats_portrait_prompts_are_written_by_its_own_model(tmp_path: Path, model: str):
    """The chat's characters' cards went to Settings' image prompt writer:
    an ordinary model, from a chat whose every call is promised to its own."""
    session, main = private_chat(tmp_path, model)
    assert session.tee_chat
    people, lore = session.picture_cards()
    session.write_card_prompts(people, lore)
    assert main.payloads, "the writer was asked"
    assert CARD_SECRET in json.dumps(main.payloads[-1])
    assert {p["model"] for p in main.payloads} == {model}
    assert session.image_prompt_model == model, "and the dialogs name it truly"
    # Whatever a caller names: the cards never leave the chat's model.
    session.write_card_prompts(people, lore, model="zai-org/glm-5.3")
    assert {p["model"] for p in main.payloads} == {model}
    # Nor a picture whose style is put into words (From a picture…).
    session.describe_picture_style(b"not a real picture", "image/png", model="zai-org/glm-5.3")
    assert {p["model"] for p in main.payloads} == {model}


def test_an_ordinary_chats_portrait_prompts_keep_the_writer_settings_names(tmp_path: Path):
    session, main = private_chat(tmp_path, "zai-org/glm-5.3-flash")
    assert not session.tee_chat and session.image_prompt_model == "zai-org/glm-5.3"
    people, lore = session.picture_cards()
    session.write_card_prompts(people, lore)
    assert {p["model"] for p in main.payloads} == {"zai-org/glm-5.3"}
