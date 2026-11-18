"""Who saw which part of a passage that moves (engine/reactions.py
`passage_views`, engine/session_views.py): a passage counted whole for anyone
in the scene at either end of it, so one that went from the street into a
closed office gave the street the office's talk. Now the scene model marks
which paragraphs each character played by their own model was there for."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.reactions import (
    VIEW_CATCH_UP,
    apply_views,
    character_history,
    content_sha,
    cuts_away,
    needs_view,
    passage_views,
    seen_part,
)
from sealedlore.models.node import Node
from sealedlore.models.scene import SceneState
from tests.test_reactions import a_cast, make_session, play, sent, turn

ASKS = "WHEN THE PASSAGE BEGAN"
BOTH = SceneState(location="The undercroft", present_character_ids=["char-serrik", "char-maela"])
TOWER = SceneState(location="The tower", present_character_ids=["char-serrik"])
MOVING = (
    "In the undercroft Serrik and Maela argue over the map.\n\n"
    "Serrik climbs alone to the tower, where the Captain whispers TOWER-SECRET."
)


def passage(text: str = MOVING, scene: SceneState | None = TOWER, id: str = "p") -> Node:
    node = Node(id=id, kind="assistant", speaker_id="__narrator__", content=text)
    node.meta.scene = scene
    return node


def maela():
    return a_cast()[1]


def view_calls(session):
    return [r for r in session.provider.requests if ASKS in sent(r)]


# --- pure ---------------------------------------------------------------------


def test_a_passage_moves_for_someone_in_it_at_one_end_or_cut_away_inside() -> None:
    node = passage()
    assert needs_view(maela(), BOTH, TOWER, node)  # she was left behind
    assert needs_view(maela(), TOWER, BOTH, node)  # she came in
    assert not needs_view(maela(), BOTH, BOTH, passage("They argue.\n\nThey argue more."))
    assert not needs_view(maela(), TOWER, TOWER, node)  # never there
    cut = passage("They argue.\n\nMeanwhile, far to the north, the Captain rides.", BOTH)
    assert cuts_away(["They argue.", "Meanwhile, far to the north, the Captain rides."])
    assert needs_view(maela(), BOTH, BOTH, cut)
    assert cuts_away(["They argue.", "---", "In the tower, he waits."])


def test_the_reply_keeps_each_ones_paragraphs_by_name() -> None:
    node = passage()
    assert apply_views('{"Maela": [1, 7, 1]}', node, [maela()])  # a name part answers
    assert node.meta.seen_parts == {"char-maela": [1]}  # out of range and repeats gone
    assert node.meta.seen_parts_of == content_sha(MOVING)
    assert seen_part(node, maela()) == "In the undercroft Serrik and Maela argue over the map."


def test_none_of_it_is_nothing_and_an_unread_reply_leaves_it_whole() -> None:
    node = passage()
    assert apply_views('{"Maela Orr": []}', node, [maela()])
    assert seen_part(node, maela()) == ""
    other = passage()
    for bad in ("no json", '{"Maela Orr": "1"}', '{"Somebody Else": [1]}', '{"Maela Orr": [true]}'):
        assert not apply_views(bad, other, [maela()])
    assert other.meta.seen_parts is None and seen_part(other, maela()) is None


def test_an_edited_passage_counts_whole_again_and_is_asked_again() -> None:
    node = passage()
    apply_views('{"Maela Orr": [1]}', node, [maela()])
    node.content = MOVING + "\n\nA third paragraph."
    assert seen_part(node, maela()) is None
    assert [n.id for n, *_ in passage_views([node], [maela()], start=BOTH)] == ["p"]


def test_only_the_newest_passages_not_yet_read_are_looked_at() -> None:
    nodes = [passage(id=f"p{i}", scene=TOWER if i % 2 else BOTH) for i in range(VIEW_CATCH_UP + 6)]
    found = passage_views(nodes, [maela()], start=BOTH)
    ids = [n.id for n, *_ in found]
    assert all(int(i[1:]) >= 6 for i in ids)  # older ones stay whole
    for node, *_ in found:
        apply_views('{"Maela Orr": [1]}', node, [maela()])
    assert passage_views(nodes, [maela()], start=BOTH) == []


# --- in the story ---------------------------------------------------------------


def moved(tmp_path: Path):
    session = make_session(tmp_path, ["The door gives.", "Hm."])
    play(session, turn())
    node = session.path()[-1]
    node.content = MOVING
    node.meta.scene = TOWER
    session.story.setup.starting_scene = BOTH
    session.provider.responses = ['{"Maela Orr": [1]}']
    return session, node


def test_she_is_given_only_what_she_was_there_for(tmp_path: Path) -> None:
    session, node = moved(tmp_path)
    list(session.mark_views(session.path()))
    [asked] = view_calls(session)
    assert "[2] Serrik climbs alone" in sent(asked) and "ASK: Maela Orr" in sent(asked)
    assert node.meta.seen_parts == {"char-maela": [1]}
    seen, _ = character_history(
        session.path(), session.cast[1], session.cast, start=BOTH, texts=DEFAULT_TEXTS
    )
    joined = "\n".join(seen)
    assert "argue over the map" in joined and "TOWER-SECRET" not in joined
    # Read once: the next round asks nothing more.
    list(session.mark_views(session.path()))
    assert len(view_calls(session)) == 1


def test_the_round_reads_the_passage_before_anyone_answers(tmp_path: Path) -> None:
    session, node = moved(tmp_path)
    session.provider.responses = ["They climb.", '{"Maela Orr": [1]}', "Hm."]
    session.provider._call_count = 0
    play(session, turn("I climb after him."))
    assert node.meta.seen_parts == {"char-maela": [1]}
    assert "Who saw what" in {
        row
        for row in __import__(
            "sealedlore.engine.usage", fromlist=["KIND_LABELS"]
        ).KIND_LABELS.values()
    }


def test_a_failed_reading_leaves_it_whole_and_says_so(tmp_path: Path) -> None:
    session, node = moved(tmp_path)
    session.provider.responses = ["not json"]
    notices = [getattr(n, "text", "") for n in session.mark_views(session.path())]
    assert any("counts whole" in n for n in notices)
    assert node.meta.seen_parts is None


def test_no_reading_without_a_character_on_their_own_model(tmp_path: Path) -> None:
    cast = a_cast()
    cast[1].own_model = None
    session = make_session(tmp_path, ["The door gives."], cast=cast)
    play(session, turn())
    session.path()[-1].meta.scene = TOWER
    list(session.mark_views(session.path()))
    assert view_calls(session) == []
