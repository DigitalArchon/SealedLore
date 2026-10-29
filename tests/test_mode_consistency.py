"""What one mode keeps from another, outside the window: the terminal and
the copies a story is made from. The mode audit (Oct 29 2026) found the CLI
playing a chat and a visual story as stories, and a visual story's copy a
game with nobody in it."""

from __future__ import annotations

import builtins
from pathlib import Path

from sealedlore.cli import main as cli_main
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.models.story import Story
from sealedlore.models.visual import PersonSheet, VisualDesign
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    scenario_from_bundle,
)


def _inputs(monkeypatch, lines: list[str]) -> None:
    """The terminal's lines, then the end of input."""
    queue = list(lines)

    def answer(_prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", answer)


def _visual(story: Story, cast) -> StoryBundle:
    story.mode = "visual"
    story.held_character_id = "char-serrik"
    story.visual_design = VisualDesign(people={"Maela Orr": PersonSheet(ability=3)})
    return StoryBundle(story=story, cast=cast)


def test_the_terminal_lists_each_mode_as_what_it_is(tmp_path: Path, story, cast, capsys):
    save_story_bundle(_visual(story, cast), root=tmp_path)
    chat = Story(title="A chat", mode="chat")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    cli_main(["--data-dir", str(tmp_path), "list"])
    out = capsys.readouterr().out
    assert f"{story.id}  {story.title}  (visual story)" in out
    assert f"{chat.id}  A chat  (chat, 0 messages)" in out


def test_the_terminal_never_plays_a_visual_story(
    tmp_path: Path, story, cast, monkeypatch, capsys
):
    """It played one as a story: two messages, a scene read, the storyteller's
    rules, in a story that must have none."""
    save_story_bundle(_visual(story, cast), root=tmp_path)
    _inputs(monkeypatch, ["I look around."])
    assert cli_main(["--data-dir", str(tmp_path), "chat", "--mock", story.id]) == 1
    assert "played in the app's game window" in capsys.readouterr().out
    assert load_story_bundle(story.id, root=tmp_path).nodes == []


def test_a_chat_in_the_terminal_is_the_user_s_messages_and_nothing_of_a_story(
    tmp_path: Path, monkeypatch, capsys
):
    """Its turns went as the Narrator's, and /ooc, /hold and /dice stored a
    note, a held character and a roll on messages a chat never sends them with."""
    chat = Story(title="A chat", mode="chat")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    _inputs(monkeypatch, ["/ooc keep it short", "/mode dice", "/where the moon", "Hello."])
    cli_main(["--data-dir", str(tmp_path), "chat", "--mock", chat.id])
    out = capsys.readouterr().out
    for command in ("/ooc", "/mode", "/where"):
        assert f"  {command} is for stories; a chat has only its messages" in out
    nodes = load_story_bundle(chat.id, root=tmp_path).nodes
    mine = next(node for node in nodes if node.kind == "user")
    assert mine.speaker_id == CHAT_USER_ID and mine.ooc is None and mine.meta.roll is None


def test_a_visual_story_s_copy_is_the_same_game_unplayed(story, cast):
    """Duplicate settings (and the CLI's) went through a scenario that had no
    design and no player: the copy opened on nobody, with nobody in it."""
    source = _visual(story, cast)
    copy = fresh_playthrough(source, "Again")
    assert copy.story.visual and copy.story.title == "Again"
    assert copy.story.visual_design == source.story.visual_design
    assert copy.story.held_character_id == "char-serrik"
    assert "char-serrik" in copy.story.scene.present_character_ids
    assert copy.story.area_live is None
    # Shared as a scenario, the design goes with it; the player is the
    # importer's to choose (asked as the game opens).
    imported = bundle_from_scenario(scenario_from_bundle(source))
    assert imported.story.visual_design == source.story.visual_design
    assert imported.story.held_character_id is None
