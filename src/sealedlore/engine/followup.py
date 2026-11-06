"""Follow-ups: the author asking for changes to a story or a plot set up for
them.

The author (Nov 6 2026): after a story or a plot is made for them, "they
might reply, 'I like the world, but I'd prefer the characters were more
badass.' Or 'I like the characters, but the world isn't hostile enough.' and
it can update it (with the ability to undo as well)."

A story's follow-up sends the setup as JSON, what was asked before and the
request to the authoring model (`authoring.followup`). Its reply is changes
of a review's kinds (engine/authoring.py, `FOLLOW_UP_KINDS`: cards and lore
entries can be taken out, and the starting scene set, since nothing has been
played), checked as a review's are and applied at once. So only what the
author asked about is written again, and what they like is never sent
through a model a second time. A plot's follow-up gives the model the plot as
it is now as its own last reply, then the request; its reply is the whole plot
again, read and checked as the first one was (engine/plot_draft.py). The
session keeps both for undo (engine/session_followup.py).

Pure.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.authoring import (
    PICTURE_STYLES,
    _choices,
    _name_of,
    _slots,
    character_view,
)
from sealedlore.engine.picture_styles import write_style
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.scene import SceneState
from sealedlore.storage.repository import StoryBundle

STORY, PLOT = "story", "plot"


@dataclass
class FollowUpTurn:
    """One request, and what came of it."""

    target: str
    request: str
    # A story's: the model's own words to the author.
    summary: str = ""
    # What was changed, a line each ("World", "Jane Moss › description").
    changed: list[str] = field(default_factory=list)
    # What was asked for and couldn't be done, or was left out.
    refused: list[str] = field(default_factory=list)
    undone: bool = False


def setup_view(bundle: StoryBundle, hidden_ids: Collection[str] = frozenset()) -> dict:
    """The setup as the model is shown it: everything a follow-up can change,
    by name. Cards and entries the plot keeps hidden are the plot's, left
    out as a review leaves them out."""
    story = bundle.story
    setup = story.setup
    scene = setup.starting_scene if setup.starting_scene != SceneState() else story.scene
    style = story.style.model_dump(exclude={"detached"})
    if not story.style.detached:
        style.pop("custom_text", None)
    present = [_name_of(bundle, i) for i in scene.present_character_ids]
    return {
        "title": story.title,
        "description": setup.description,
        "world": story.world_bible,
        "world_activity": story.defaults.world_activity,
        "agency_mode": story.defaults.agency_mode,
        "picture_style": write_style(story.picture_style, story.image_style) or None,
        "style": style,
        "opening": {"text": setup.opening_text, "mode": setup.opening_mode},
        "starting_scene": {
            "location": scene.location,
            "time_of_day": scene.time_of_day,
            "situation": scene.situation,
            "present": [name for name in present if name] + list(scene.present_others),
        },
        "cast": [character_view(c) for c in bundle.cast if c.id not in hidden_ids],
        "supporting": [
            character_view(c) for c in bundle.supporting.characters if c.id not in hidden_ids
        ],
        "lore": [
            {
                "title": entry.title,
                "content": entry.content,
                "keywords": entry.keywords,
                "always_on": entry.always_on,
                "enabled": entry.enabled,
                "until_mentioned": entry.until_mentioned,
            }
            for entry in bundle.lore
            if entry.id not in hidden_ids
        ],
    }


def followup_system(texts: PromptTexts = DEFAULT_TEXTS) -> str:
    slots = _slots(texts)
    return texts.fill(
        "authoring.followup",
        agency_modes=slots["agency_modes"],
        world_activities=slots["world_activities"],
        picture_styles=_choices([style.label for style in PICTURE_STYLES]),
        opening_modes=slots["opening_modes"],
        style_fields=slots["style_fields"],
        style_guide=slots["style_guide"],
        character_fields=slots["character_fields"],
        tiers=slots["tiers"],
        character_shape=slots["character_shape"],
        lore_fields=slots["lore_fields"],
    )


def render_earlier(turns: Sequence[FollowUpTurn]) -> str:
    """What was asked before and still stands, a line each."""
    lines = []
    for turn in turns:
        if turn.undone:
            continue
        done = turn.summary or ", ".join(turn.changed) or "nothing could be changed"
        lines.append(f"- “{' '.join(turn.request.split())}”: {done}")
    return "\n".join(lines)


def _earlier(turns: Sequence[FollowUpTurn], texts: PromptTexts) -> str:
    earlier = render_earlier(turns)
    return texts.fill("authoring.followup.earlier", earlier=earlier) if earlier else ""


def build_followup_messages(
    view: dict,
    request: str,
    earlier: Sequence[FollowUpTurn] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """A story's follow-up: the setup now, what was asked before, the request."""
    user = texts.fill(
        "authoring.followup.request",
        setup=json.dumps(view, indent=2, ensure_ascii=False),
        earlier=_earlier(earlier, texts),
        request=" ".join(request.split()),
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=followup_system(texts)),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def plot_request_text(
    request: str, earlier: Sequence[FollowUpTurn] = (), texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """A plot's follow-up, after the plot as it is now."""
    return texts.fill(
        "authoring.plot.followup",
        earlier=_earlier(earlier, texts),
        request=" ".join(request.split()),
    )


def setup_fingerprint(bundle: StoryBundle) -> str:
    """Of everything a follow-up can change, and nothing the story writes as
    it goes (a condensed card, an embedding, the live scene): the same
    before an undo as just after the follow-up, unless something else changed
    it meanwhile."""
    story = bundle.story
    data = {
        "story": story.model_dump(
            include={
                "title",
                "world_bible",
                "setup",
                "defaults",
                "style",
                "picture_style",
                "image_style",
                "plot",
            }
        ),
        "cast": [c.model_dump(exclude={"condensed", "condensed_from"}) for c in bundle.cast],
        "supporting": [
            c.model_dump(exclude={"condensed", "condensed_from"})
            for c in bundle.supporting.characters
        ],
        "lore": [entry.model_dump(exclude={"embedding_hash"}) for entry in bundle.lore],
    }
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()
