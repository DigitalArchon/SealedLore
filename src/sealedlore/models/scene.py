"""Scene state, embedded in story.json and on each passage.

The scene used to be one story-level record the author kept by hand. It is now
a snapshot on the passage it describes (`NodeMeta.scene`), kept by a small
model that reads each passage (engine/scene_update.py), so it follows the
story and the branch the author is on. `Story.scene` is the live copy for the
current leaf.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id

# Who last set a snapshot. "story" is the scene read after a passage, which is
# what makes the Scene panel offer Undo; anything the author does is "author".
SceneSource = Literal["author", "story"]
Privacy = Literal["public", "private"]
# The three ways of putting someone in a scene by shortcut, which is what the
# presence rule exists to stop (the second rule in CONTRIBUTING.md).
ShortcutKind = Literal["already_there", "perceived_from_outside", "knew_without_being_told"]
SceneChangeKind = Literal["arrived", "left", "location", "time", "situation", "privacy"]


class OffstageCharacter(BaseModel):
    character_id: str
    note: str


class SceneChange(BaseModel):
    """One thing a passage changed about the scene, for the panel and the notice."""

    kind: SceneChangeKind
    # Who, for arrivals and departures.
    name: str = ""
    character_id: str | None = None
    # How they came or went ("came in from the main street"), or the new value.
    detail: str | None = None


class SceneShortcut(BaseModel):
    """Someone put in the scene by shortcut, as the scene read found it."""

    name: str
    character_id: str | None = None
    kind: ShortcutKind
    # The sentence, as the passage has it, so the banner can quote it and
    # Trim can find it.
    quote: str


class SceneState(BaseModel):
    location: str | None = None
    time_of_day: str | None = None
    situation: str | None = None
    present_character_ids: list[str] = Field(default_factory=list)
    # Everyone else who is here and has a name: supporting characters by their
    # card's name, and people with no card as written. Names, not ids, so
    # nothing that iterates the cast picks them up. The roster used to cover
    # the cast alone, which on the real 216-passage story meant it governed
    # one character while the setting's regulars came and went unseen.
    present_others: list[str] = Field(default_factory=list)
    offstage_but_nearby: list[OffstageCharacter] = Field(default_factory=list)
    # Whether what happens here stays with those present. None is unknown.
    privacy: Privacy | None = None
    # Whether `present_others` has been kept, by the scene read or by hand.
    # Only then can someone on file who isn't listed be called absent; an
    # untracked card says nothing about them, as the roster always did.
    tracked: bool = False
    source: SceneSource = "author"
    # How this snapshot differs from the one before it (scene reads only).
    changes: list[SceneChange] = Field(default_factory=list)
    # The leaf the scene was last set at. Kept for stories written before
    # snapshots, where it anchors the one scene they had.
    set_at_node_id: str | None = None


class SceneLogEntry(BaseModel):
    """A scene that has ended: where, who was there, and whether it was private.

    The record of who could know what. A private exchange stays with the
    people listed here until one of them passes it on.
    """

    id: str = Field(default_factory=new_id)
    location: str | None = None
    present_names: list[str] = Field(default_factory=list)
    privacy: Privacy | None = None
    gist: str = ""
    # The passage that ended it. Entries are shown only when this is on the
    # active path, so a branch that forks earlier never sees them.
    closed_at_node_id: str
