"""A story's passages on an end-to-end encrypted model are marked as a
private scene's and a chat's are (the author, Nov 19 2026: the Private model
profile makes every job one): "encrypted" once the attested enclave's seal
has opened the reply, the author's turn with it; any other model's, none."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.providers.base import StreamCompleted
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle


class SealedMock(MockChatProvider):
    """Replies as Private Mode's client gives them: opened with the seal."""

    def stream(self, request):
        for event in super().stream(request):
            yield (
                dataclasses.replace(event, sealed=True)
                if isinstance(event, StreamCompleted)
                else event
            )


def play(tmp_path: Path, story, cast, estimator, model: str, provider) -> StorySession:
    config = Config(
        providers=[ProviderConfig(name="NanoGPT", api_key="k", model=model)],
        active_provider_name="NanoGPT",
        scene_reads="manual",
        plot_reads=False,
        prose_reminders=False,
    )
    session = StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        provider,
        root=tmp_path,
        estimator=estimator,
        learn_corrections=False,
    )
    turn = TurnRequest(
        speaker_id="char-serrik",
        user_text="I lift the torch.",
        controlled_character_id="char-serrik",
        npc_scope="all",
        agency_mode="contested",
    )
    for _event in session.send(turn):
        pass
    return session


def test_a_storys_passage_on_an_encrypted_model_is_marked(tmp_path, story, cast, estimator):
    session = play(
        tmp_path, story, cast, estimator, "private/glm-5-3", SealedMock(["The torch gutters."])
    )
    turn, passage = session.path()[-2:]
    assert passage.meta.tee == "encrypted"
    assert turn.meta.tee == "encrypted"


def test_any_other_models_passage_is_not(tmp_path, story, cast, estimator):
    session = play(
        tmp_path,
        story,
        cast,
        estimator,
        "anthropic/claude-sonnet-4.6",
        MockChatProvider(["The torch gutters."]),
    )
    assert session.path()[-1].meta.tee is None
