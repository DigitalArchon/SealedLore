"""How much a premise draft sets up (engine/draft_detail.py): the world, the
people and how much about each, three levels apiece. The standard levels are
the draft as it always was, byte for byte; any other is a line in the request,
after the premise and the author's own characters, before the chosen person
and tense."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.authoring import build_generation_messages
from sealedlore.engine.draft_detail import (
    LEVELS,
    STANDARD,
    STANDARD_DETAIL,
    DraftDetail,
    render_detail,
)
from sealedlore.engine.drafting import StoryDraft
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.generation import ReasoningConfig
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import read_api_log
from tests.test_authoring import DRAFT

PREMISE = "A lighthouse keeper's island, and a ship that never reaches port."
OWN = [Character(name="Elena Ruiz", summary="A medic.", full_description="Thirty, steady.")]
LEAD = "The author has chosen how much to set up."


def texts_of(messages) -> list[str]:
    return [message.text for message in messages]


def test_the_standard_levels_are_the_default_and_anything_else_is_refused():
    assert DraftDetail() == STANDARD_DETAIL
    assert DraftDetail().chosen() == {}
    assert DraftDetail(world="basic", depth="deep").chosen() == {"world": "basic", "depth": "deep"}
    with pytest.raises(ValueError, match="isn't one of basic, standard, detailed"):
        DraftDetail(world="huge")
    with pytest.raises(ValueError):
        DraftDetail(people="lots", depth="sketchy")
    with pytest.raises(ValueError):
        DraftDetail(people="")


def test_every_level_has_its_words():
    """Keys built from a level's name, which no single draft reaches all of."""
    keys = [
        f"authoring.draft.{choice}.{level}"
        for choice, levels in LEVELS.items()
        for level in levels
        if level != STANDARD
    ]
    assert [key for key in keys if key not in TEXTS] == []
    assert f"authoring.draft.world.{STANDARD}" not in TEXTS, "standard is the prompt's own"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"person": "second", "tense": "present"},
        {"characters": OWN},
        {"characters": OWN, "person": "first", "tense": "past"},
    ],
)
def test_a_standard_draft_is_sent_as_it_always_was(kwargs):
    """The default choices add nothing: the request is the one built without
    them, byte for byte, the system prompt included."""
    before = build_generation_messages(PREMISE, **kwargs)
    after = build_generation_messages(PREMISE, detail=DraftDetail(), **kwargs)
    assert texts_of(after) == texts_of(before)
    assert LEAD not in after[-1].text
    assert render_detail(STANDARD_DETAIL) == ""


@pytest.mark.parametrize(
    ("choice", "level"),
    [(choice, level) for choice, levels in LEVELS.items() for level in levels if level != STANDARD],
)
def test_each_level_adds_its_line_and_only_it(choice, level):
    standard = build_generation_messages(PREMISE)
    chosen = build_generation_messages(PREMISE, detail=DraftDetail(**{choice: level}))
    assert chosen[0].text == standard[0].text, "the system prompt never changes"
    block = DEFAULT_TEXTS.fill(
        "authoring.draft.detail", sizes=f"- {DEFAULT_TEXTS[f'authoring.draft.{choice}.{level}']}"
    )
    assert chosen[1].text == standard[1].text + "\n\n" + block
    for other in TEXTS:
        if other.startswith("authoring.draft.") and other.count(".") == 3:
            assert (TEXTS[other].default in chosen[1].text) == (
                other == f"authoring.draft.{choice}.{level}"
            ), other


def test_the_sizes_come_after_the_characters_and_before_the_voice():
    detail = DraftDetail(world="detailed", people="lots", depth="deep")
    user = build_generation_messages(
        PREMISE, person="second", tense="present", characters=OWN, detail=detail
    )[1].text
    characters = user.index("The author wants the story built around characters of their own")
    sizes = user.index(LEAD)
    voice = user.index("The author has chosen how the story is told.")
    assert user.startswith("The author's premise:\n\n" + PREMISE)
    assert characters < sizes < voice
    # A line each, in the order the choices read.
    lines = [line for line in user[sizes:voice].splitlines() if line.startswith("- ")]
    assert lines == [
        f"- {DEFAULT_TEXTS['authoring.draft.world.detailed']}",
        f"- {DEFAULT_TEXTS['authoring.draft.people.lots']}",
        f"- {DEFAULT_TEXTS['authoring.draft.depth.deep']}",
    ]


def test_an_edited_level_is_what_is_sent():
    texts = PromptTexts({"authoring.draft.people.handful": "Two people, no more."})
    user = build_generation_messages(PREMISE, texts=texts, detail=DraftDetail(people="handful"))[
        1
    ].text
    assert user.endswith("- Two people, no more.")


def test_a_draft_logs_what_was_chosen_beside_its_request(tmp_path: Path):
    provider = MockChatProvider([json.dumps(DRAFT)])
    detail = DraftDetail(people="lots", depth="sketch")
    draft = StoryDraft(provider, PREMISE, "m", reasoning=ReasoningConfig(), detail=detail)
    list(draft.stream())
    assert LEAD in provider.last_request.messages[-1].text
    bundle, _ = draft.build(root=tmp_path)
    request = read_api_log(bundle.story.id, root=tmp_path)[0]
    assert request["kind"] == "draft_request"
    assert request["detail"] == {"world": STANDARD, "people": "lots", "depth": "sketch"}
    assert LEAD in request["payload"]["messages"][-1]["content"]


def test_a_standard_draft_logs_as_it_always_did(tmp_path: Path):
    provider = MockChatProvider([json.dumps(DRAFT)])
    draft = StoryDraft(provider, PREMISE, "m", reasoning=ReasoningConfig())
    assert draft.detail == STANDARD_DETAIL
    list(draft.stream())
    bundle, _ = draft.build(root=tmp_path)
    request = read_api_log(bundle.story.id, root=tmp_path)[0]
    assert list(request) == ["id", "kind", "at", "premise", "payload"]
