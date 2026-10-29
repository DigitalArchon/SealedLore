"""What one mode keeps from another, outside the window: the terminal and
the loaders. The mode audit (Oct 29 2026) found the CLI playing a chat as a
story; the split (Oct 29 2026) left Visual Stories to an app of their own."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from sealedlore.cli import main as cli_main
from sealedlore.models.node import CHAT_USER_ID
from sealedlore.models.story import Story
from sealedlore.storage.library import story_listings
from sealedlore.storage.repository import (
    VISUAL_ELSEWHERE,
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)


def _inputs(monkeypatch, lines: list[str]) -> None:
    """The terminal's lines, then the end of input."""
    queue = list(lines)

    def answer(_prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", answer)


def _game(root: Path, story_id: str = "game-1") -> str:
    """A Visual Story's folder as SealedLore played them before they became a
    game of their own: its story.json says mode "visual"."""
    folder = root / "stories" / story_id
    folder.mkdir(parents=True)
    (folder / "story.json").write_text(
        json.dumps({"id": story_id, "title": "Doomsville", "mode": "visual"}), encoding="utf-8"
    )
    return story_id


def test_a_visual_story_left_here_is_listed_and_never_opened(tmp_path: Path, monkeypatch, capsys):
    """The visual mode is an app of its own now: a game played here before
    is listed so it can be found and deleted, with the reason, and neither
    the window, the terminal nor a loader takes it for a story."""
    game = _game(tmp_path)
    chat = Story(title="A chat", mode="chat")
    save_story_bundle(StoryBundle(story=chat), root=tmp_path)
    listing = {row.id: row for row in story_listings(tmp_path)}
    assert listing[game].error == VISUAL_ELSEWHERE and listing[game].title == "Doomsville"
    assert listing[chat.id].error is None
    with pytest.raises(ValueError, match="its own app"):
        load_story_bundle(game, root=tmp_path)
    cli_main(["--data-dir", str(tmp_path), "list"])
    out = capsys.readouterr().out
    assert f"{chat.id}  A chat  (chat, 0 messages)" in out and game not in out
    _inputs(monkeypatch, ["I look around."])
    assert cli_main(["--data-dir", str(tmp_path), "chat", "--mock", game]) == 1
    assert VISUAL_ELSEWHERE in capsys.readouterr().err
    assert not (tmp_path / "stories" / game / "nodes.json").exists()


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
