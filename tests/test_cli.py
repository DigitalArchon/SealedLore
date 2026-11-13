"""The terminal: what it refuses says so in words, and the REPL goes on."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from sealedlore.cli import main as cli_main
from sealedlore.storage.repository import load_story_bundle
from tests.test_private import MARKER, build, enter, play, turn


def _inputs(monkeypatch, lines: list[str]) -> None:
    """The terminal's lines, then the end of input."""
    queue = list(lines)

    def answer(_prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", answer)


def test_a_story_left_in_a_private_scene_refuses_in_words_and_keeps_the_scene(
    tmp_path: Path, monkeypatch, capsys
):
    """A story the app closed inside a scene kept on disk: /scene and /ask
    ended the terminal with a traceback, and /undo read the story without
    the scene, deleting the exchange before it and the whole scene with it."""
    session, _main, held = build(tmp_path)
    play(session, turn(held, "I walk in."))
    enter(session, keep="disk")
    play(session, turn(held, f"I whisper {MARKER}."))
    session.save()
    public = [node.id for node in session.path()]
    name = session.cast[0].name

    _inputs(
        monkeypatch,
        [
            "/scene somewhere else",
            "/where the mill",
            "/present Nobody",
            "/privacy on",
            f"/hold {name}",
            "/undo-scene",
            "/ask what happened?",
            "/undo",  # the scene's own exchange: allowed, as in the window
            "/undo",  # the exchange before the scene: refused
        ],
    )
    assert cli_main(["--data-dir", str(tmp_path), "chat", "--mock", session.story.id]) == 0
    out = capsys.readouterr().out
    assert out.count("waits until the private scene ends") >= 5
    assert "the private scene has no model to ask" in out
    assert "End the private scene first" in out

    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.private_spans[-1].status == "open"
    assert [node.id for node in saved.nodes if not node.meta.private_span] == public
    assert saved.story.active_leaf_id == public[-1]


def test_exporting_a_story_that_isnt_there_says_so(tmp_path: Path, capsys):
    for command in (["export", "no-such-story"], ["scenario", "export", "no-such-story"]):
        target = tmp_path / "out.json"
        assert cli_main(["--data-dir", str(tmp_path), *command, str(target)]) == 1
        assert "no-such-story" in capsys.readouterr().err
        assert not target.exists()


@pytest.mark.parametrize(
    ("verb", "flags"),
    [(["export"], []), (["export"], ["--markdown"]), (["scenario", "export"], [])],
)
def test_exporting_where_nothing_can_be_written_says_so(
    tmp_path: Path, capsys, verb: list[str], flags: list[str]
):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "I walk in."))
    folder = tmp_path / "a folder"
    folder.mkdir()
    argv = ["--data-dir", str(tmp_path), *verb, session.story.id, str(folder), *flags]
    assert cli_main(argv) == 1
    assert "Couldn't write" in capsys.readouterr().err


def test_inspect_shows_a_long_request_s_end_as_well_as_its_start():
    """Its first 4,000 characters alone never got past the standing rules."""
    from sealedlore.cli import INSPECT_HEAD, INSPECT_TAIL, _both_ends

    short = "x" * 100
    assert _both_ends(short) == short
    long = "RULES " * 2_000 + "THE AUTHOR'S TURN: I open the door."
    shown = _both_ends(long)
    assert shown.startswith("RULES") and shown.endswith("I open the door.")
    assert "characters left out" in shown
    assert len(shown) < INSPECT_HEAD + INSPECT_TAIL + 80
