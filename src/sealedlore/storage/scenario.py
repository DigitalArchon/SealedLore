"""Scenario files: converting a story to and from its shareable base, and disk I/O.

Conversions are pure; only `write_scenario` and `read_scenario` touch disk.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from sealedlore.models.scenario import SCENARIO_FORMAT, SCENARIO_VERSION, Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story, StorySetup
from sealedlore.models.supporting import SupportingCast
from sealedlore.storage.atomic import atomic_write_json
from sealedlore.storage.image_meta import MetadataError, strip_metadata, stripped_or_same
from sealedlore.storage.images import IMAGES_FILE, is_safe_relative
from sealedlore.storage.repository import StoryBundle

SCENARIO_SUFFIX = ".sealedlore-scenario.json"


class ScenarioError(ValueError):
    """A file that isn't a scenario this version can read, said in plain words."""


def _is_empty(scene: SceneState) -> bool:
    return scene == SceneState()


def scenario_from_bundle(bundle: StoryBundle, files: Mapping[str, bytes] | None = None) -> Scenario:
    """`files` are the reference pictures' bytes (`storage.images.reference_files`),
    carried in the scenario so the pictures travel with it."""
    story = bundle.story
    setup = story.setup
    # A story whose starting scene was never set: the one it is in now is
    # the best guess at where it began.
    starting = setup.starting_scene if not _is_empty(setup.starting_scene) else story.scene
    cast = [
        # A portrait is a path on this machine, meaningless anywhere else.
        character.model_copy(update={"portrait_path": None}, deep=True)
        # Whoever wasn't picked from a "play one of these" choice is back in
        # the cast, so the next playthrough gets the choice again.
        for character in [*bundle.cast, *setup.set_aside]
    ]
    # Vectors are cached per endpoint and model, so the importer re-embeds.
    lore = [entry.model_copy(update={"embedding_hash": None}, deep=True) for entry in bundle.lore]
    supporting = [
        character.model_copy(update={"portrait_path": None}, deep=True)
        for character in bundle.supporting.characters
    ]
    return Scenario(
        title=story.title,
        description=setup.description,
        world_bible=story.world_bible,
        opening_text=setup.opening_text,
        opening_mode=setup.opening_mode,
        # A fresh card: who set it, what changed and where are this
        # playthrough's, and the ids they name don't exist in another one.
        starting_scene=starting.model_copy(
            deep=True, update={"source": "author", "changes": [], "set_at_node_id": None}
        ),
        absent_at_start=list(setup.absent_at_start),
        suggested_character_id=setup.suggested_character_id,
        choose_one_of=list(setup.choose_one_of),
        style=story.style.model_copy(deep=True),
        agency_mode=story.defaults.agency_mode,
        npc_scope=story.defaults.npc_scope,
        world_activity=story.defaults.world_activity,
        cast=cast,
        lore=lore,
        supporting=supporting,
        plot=story.plot.model_copy(deep=True) if story.plot is not None else None,
        image_style=story.image_style,
        mode=story.mode,
        chat_prompt=story.chat_prompt,
        chat_tail=story.chat_tail,
        reference_images=[ref.model_copy() for ref in story.reference_images],
        prompt_edits={key: edit.model_copy() for key, edit in story.prompt_edits.items()},
        reference_files={
            relative: base64.b64encode(data).decode("ascii")
            for relative, data in (files or {}).items()
        },
    )


def _decoded_files(encoded: Mapping[str, str]) -> dict[str, bytes]:
    """Reference pictures from a file, dropping any that aren't safe or
    readable, their hidden data removed where it can be."""
    files: dict[str, bytes] = {}
    for relative, text in encoded.items():
        if not is_safe_relative(relative) or relative == IMAGES_FILE:
            continue
        try:
            files[relative] = stripped_or_same(base64.b64decode(text, validate=True))
        except (binascii.Error, ValueError):
            continue
    return files


def bundle_from_scenario(
    scenario: Scenario, *, main_model: str | None = None, title: str | None = None
) -> StoryBundle:
    """A new, unplayed story: fresh id, no messages, the scene at its start."""
    cast_ids = {character.id for character in scenario.cast}
    # Hand-edited files can name characters that aren't in them; drop those
    # rather than put an unknown id on the roster.
    starting = scenario.starting_scene.model_copy(deep=True)
    starting.present_character_ids = [
        character_id for character_id in starting.present_character_ids if character_id in cast_ids
    ]
    starting.offstage_but_nearby = [
        entry for entry in starting.offstage_but_nearby if entry.character_id in cast_ids
    ]
    suggested = scenario.suggested_character_id
    if suggested not in cast_ids:
        suggested = None

    story = Story(
        title=title or scenario.title,
        world_bible=scenario.world_bible,
        setup=StorySetup(
            description=scenario.description,
            opening_text=scenario.opening_text,
            opening_mode=scenario.opening_mode,
            starting_scene=starting,
            absent_at_start=[
                character_id
                for character_id in scenario.absent_at_start
                if character_id in cast_ids and character_id not in starting.present_character_ids
            ],
            suggested_character_id=suggested,
            choose_one_of=[
                character_id for character_id in scenario.choose_one_of if character_id in cast_ids
            ],
        ),
        style=scenario.style.model_copy(deep=True),
        scene=starting.model_copy(deep=True),
        plot=scenario.plot.model_copy(deep=True) if scenario.plot is not None else None,
        image_style=scenario.image_style,
        mode=scenario.mode,
        chat_prompt=scenario.chat_prompt,
        chat_tail=scenario.chat_tail,
        reference_images=[ref.model_copy() for ref in scenario.reference_images],
        prompt_edits={key: edit.model_copy() for key, edit in scenario.prompt_edits.items()},
    )
    story.defaults.agency_mode = scenario.agency_mode
    story.defaults.npc_scope = scenario.npc_scope
    story.defaults.world_activity = scenario.world_activity
    story.defaults.main_model = main_model
    return StoryBundle(
        story=story,
        cast=[character.model_copy(deep=True) for character in scenario.cast],
        lore=[entry.model_copy(deep=True) for entry in scenario.lore],
        supporting=SupportingCast(
            characters=[character.model_copy(deep=True) for character in scenario.supporting]
        ),
        pending_files=_decoded_files(scenario.reference_files),
    )


def fresh_playthrough(
    source: StoryBundle, title: str, files: Mapping[str, bytes] | None = None
) -> StoryBundle:
    """The same setup as `source`, unplayed: a new story with a fresh id.

    Restart and "Duplicate settings" both come through here. Everything a
    scenario carries comes along, and so do the model, budget and generation
    settings a scenario file deliberately leaves out: same machine, same
    author. Reviewed settings are in one of those two places, so a review's
    changes survive a restart. `files` are the reference pictures, which
    come along; pictures generated in play don't.
    """
    bundle = bundle_from_scenario(scenario_from_bundle(source, files), title=title)
    bundle.story.defaults = source.story.defaults.model_copy(deep=True)
    return bundle


def write_scenario(path: Path, scenario: Scenario) -> None:
    """A scenario is made to be shared: every picture goes with its hidden
    data removed, and one that can't have it removed stops the export.
    Raises ScenarioError."""
    files: dict[str, str] = {}
    for relative, text in scenario.reference_files.items():
        try:
            clean = strip_metadata(base64.b64decode(text, validate=True))
        except (MetadataError, binascii.Error, ValueError) as exc:
            raise ScenarioError(
                f"The picture {relative} couldn't have its hidden data removed ({exc}), "
                "so nothing was written. Replace it with another copy of the picture."
            ) from exc
        files[relative] = base64.b64encode(clean).decode("ascii")
    data = scenario.model_dump()
    data["reference_files"] = files
    atomic_write_json(path, data)


def read_scenario(path: Path) -> Scenario:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(f"Couldn't read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{Path(path).name} isn't valid JSON (line {exc.lineno}).") from exc

    if not isinstance(data, dict) or data.get("format") != SCENARIO_FORMAT:
        raise ScenarioError(f"{Path(path).name} isn't a SealedLore scenario file.")
    version = data.get("version")
    if not isinstance(version, int) or version > SCENARIO_VERSION:
        raise ScenarioError(
            f"{Path(path).name} is scenario format version {version}; this SealedLore "
            f"reads up to version {SCENARIO_VERSION}."
        )
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        raise ScenarioError(f"{Path(path).name} has invalid content — {problems}") from exc
