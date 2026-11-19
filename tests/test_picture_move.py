"""A picture copied or moved to another character (engine/picture_move.py,
gui/picture_choose.py): versions of one character sharing some pictures."""

from __future__ import annotations

import os

import pytest

from sealedlore.engine.picture_move import give_picture
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef


def two() -> tuple[Character, Character]:
    max_ = Character(
        name="Max",
        reference_images=[
            ImageRef(id="p1", file="images/refs/max.png", look="leathers", expression="neutral"),
            ImageRef(
                id="p2", file="images/refs/max-angry.png", look="leathers", expression="angry"
            ),
        ],
    )
    return max_, Character(name="Max, older")


def test_a_copy_keeps_the_file_and_labels_under_an_id_of_its_own() -> None:
    source, target = two()
    given = give_picture(source, target, "p2", move=False)
    assert given is not None and given.id != "p2"
    assert (given.file, given.look, given.expression) == (
        "images/refs/max-angry.png",
        "leathers",
        "angry",
    )
    assert [r.id for r in source.reference_images] == ["p1", "p2"]
    assert target.reference_images == [given]
    # The same file twice is nothing to add.
    assert give_picture(source, target, "p2", move=False) is None
    assert len(target.reference_images) == 1


def test_a_move_takes_it_off_the_first_card() -> None:
    source, target = two()
    give_picture(source, target, "p1", move=True)
    assert [r.id for r in source.reference_images] == ["p2"], "the next is their portrait now"
    assert [r.file for r in target.reference_images] == ["images/refs/max.png"]
    # The source's sheet keeps its rows where they were.
    assert source.sheet_ordered and "leathers" in source.sheet_looks


def test_nothing_for_a_picture_they_dont_have() -> None:
    source, target = two()
    assert give_picture(source, target, "nope", move=True) is None
    assert give_picture(source, source, "p1", move=True) is None
    assert len(source.reference_images) == 2


def test_the_choice_names_versions_and_tells_two_of_a_name_apart(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QInputDialog

    from sealedlore.gui.picture_choose import choose_character
    from sealedlore.models.collection import CollectionInfo

    QApplication.instance() or QApplication([])
    main = Character(id="m", name="Max")
    older = Character(name="Max", collection=CollectionInfo(family="m", version="Older"))
    other = Character(name="Max")
    offered = []

    def pick(_parent, _title, _where, labels, *_a):
        offered.extend(labels)
        return labels[1], True

    monkeypatch.setattr(QInputDialog, "getItem", pick)
    chosen = choose_character(
        None, main, [older, other, Character(name="Max")], move=True, kept=[main, older]
    )
    assert offered == ["Max — Older", "Max", "Max (2)"]
    assert chosen is other


def test_your_characters_moves_a_picture_to_another_version(tmp_path, monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QInputDialog

    from sealedlore.gui.main_window import MainWindow
    from tests.test_collection_gui import three_kept

    QApplication.instance() or QApplication([])
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    try:
        elena, ben, older = three_kept(window, tmp_path, monkeypatch)
        ref = elena.reference_images[0]
        monkeypatch.setattr(
            QInputDialog, "getItem", lambda _p, _t, _w, labels, *_a: (labels[-1], True)
        )
        window._collection_board_picture("copy", elena.id, ref.id)
        kept = {c.id: c for c in window._kept()}
        assert [r.file for r in kept[older.id].reference_images] == [ref.file]
        assert ref in kept[elena.id].reference_images
        window._collection_board_picture("move", elena.id, ref.id)
        assert ref not in window._kept_card(elena.id).reference_images
        # The file stays: a card still names it.
        assert window._collection_store().read(ref.file)
    finally:
        window.close()
