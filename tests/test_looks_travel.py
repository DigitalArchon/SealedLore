"""A character's pictures, their labels and their sheet's own rows and
columns (engine/look_sheet.py) go with the story wherever it goes: a
scenario file, Restart and Duplicate settings, a story backup. The author
(Nov 3 2026): a story with many pictures added should start new stories as
it is."""

from __future__ import annotations

import os
import zlib
from pathlib import Path

from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.story import Story
from sealedlore.storage.archive import read_archive, restore_archive, write_archive
from sealedlore.storage.images import all_image_files, reference_files, story_path
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)

LABELS = [
    ("portrait", "winter coat", "neutral"),
    ("angry", "winter coat", "angry"),
    ("armour", "combat armour", "neutral"),
    ("loose", "", ""),
]
# How much of him each shows (engine/portraits.py FRAMINGS), where said.
FRAMINGS = {"portrait": "head", "armour": "full"}


def png(red: int) -> bytes:
    """A 1x1 PNG of its own colour, made by hand: storage has no Qt."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")

    header = (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0])))
        + chunk(b"IEND", b"")
    )


def made(root: Path) -> StoryBundle:
    john = Character(
        id="john",
        name="John",
        sheet_looks=["formal clothes"],
        sheet_expressions=["neutral", "angry", "laughing"],
    )
    bundle = StoryBundle(story=Story(id="looks", title="Doomsville, mine"), cast=[john])
    for number, (ref_id, look, expression) in enumerate(LABELS):
        file = f"images/refs/{ref_id}.png"
        bundle.pending_files[file] = png(40 * number + 10)
        john.reference_images.append(
            ImageRef(
                id=ref_id,
                file=file,
                caption=ref_id,
                look=look,
                expression=expression,
                framing=FRAMINGS.get(ref_id, ""),
            )
        )
    save_story_bundle(bundle, root=root)
    return load_story_bundle("looks", root=root)


def check(bundle: StoryBundle, root: Path) -> None:
    """Every picture there with its own bytes, labels and order; the sheet's
    rows and columns as they were."""
    john = next(c for c in bundle.cast if c.name == "John")
    assert [(r.caption, r.look, r.expression) for r in john.reference_images] == [
        (ref_id, look, expression) for ref_id, look, expression in LABELS
    ]
    assert [r.framing for r in john.reference_images] == [
        FRAMINGS.get(ref_id, "") for ref_id, _look, _expression in LABELS
    ]
    for number, ref in enumerate(john.reference_images):
        assert story_path(bundle.story.id, ref.file, root).read_bytes() == png(40 * number + 10)
    assert john.sheet_looks == ["formal clothes"]
    assert john.sheet_expressions == ["neutral", "angry", "laughing"]


def test_a_scenario_file_carries_every_picture_its_labels_and_the_sheet(tmp_path: Path):
    source = made(tmp_path / "a")
    path = tmp_path / "mine.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(source, reference_files(source, tmp_path / "a")))
    started = bundle_from_scenario(read_scenario(path))
    save_story_bundle(started, root=tmp_path / "b")
    check(load_story_bundle(started.story.id, root=tmp_path / "b"), tmp_path / "b")


def test_restart_and_duplicate_settings_keep_them(tmp_path: Path):
    source = made(tmp_path)
    again = fresh_playthrough(source, "Doomsville, again", reference_files(source, tmp_path))
    save_story_bundle(again, root=tmp_path)
    assert again.story.id != source.story.id
    check(load_story_bundle(again.story.id, root=tmp_path), tmp_path)


def test_a_story_backup_keeps_them(tmp_path: Path):
    source = made(tmp_path / "a")
    path = tmp_path / "backup.sealedlore-story.json"
    write_archive(path, source, [], files=all_image_files("looks", tmp_path / "a"))
    bundle, log = read_archive(path)
    restored = restore_archive(bundle, log, root=tmp_path / "b")
    check(load_story_bundle(restored.story.id, root=tmp_path / "b"), tmp_path / "b")


def test_a_charx_card_carries_every_picture_with_its_labels(tmp_path: Path):
    """A character on their own, every picture with them: the portrait the
    card's icon, the rest "emotion" assets named by expression, as
    SillyTavern and RisuAI read them; SealedLore's labels (the look too) and
    the sheet's own rows and columns ride in the card's extensions."""
    import json
    import zipfile

    from sealedlore.storage.tavern_cards import card_charx, card_data, read_card, scenario_from_card

    source = made(tmp_path / "a")
    john = source.cast[0]
    pictures = [
        (ref, story_path(source.story.id, ref.file, tmp_path / "a").read_bytes())
        for ref in john.reference_images
    ]
    _v2, v3 = card_data(john, story_title=source.story.title)
    path = tmp_path / "John.charx"
    path.write_bytes(card_charx(v3, pictures))

    with zipfile.ZipFile(path) as archive:
        card = json.loads(archive.read("card.json"))
    assets = card["data"]["assets"]
    assert [(a["type"], a["name"]) for a in assets] == [
        ("icon", "main"),
        ("emotion", "angry"),
        ("emotion", "neutral"),
        ("emotion", "picture"),
    ]

    imported = scenario_from_card(read_card(path), player="Jane")
    character = next(c for c in imported.scenario.cast if c.name == "John")
    assert [(r.look, r.expression) for r in character.reference_images] == [
        (look, expression) for _id, look, expression in LABELS
    ]
    assert [r.caption for r in character.reference_images][1:] == ["angry", "armour", "loose"]
    assert [r.framing for r in character.reference_images] == ["head", "", "full", ""]
    assert character.sheet_looks == ["formal clothes"]
    assert character.sheet_expressions == ["neutral", "angry", "laughing"]
    for number, ref in enumerate(character.reference_images[1:], 1):
        data = imported.scenario.reference_files[ref.file]
        import base64

        assert base64.b64decode(data) == png(40 * number + 10)


def test_pictures_kept_for_a_template_go_on_its_next_story(tmp_path: Path):
    """Kept in the data folder by the template's title; the next story from
    it matches its own pictures by their bytes, takes the kept labels, adds
    the rest, and keeps any of its own the kept story didn't have."""
    from sealedlore.storage.template_pictures import (
        apply_kept,
        forget_kept,
        keep_pictures,
        read_kept,
        template_of,
    )

    source = made(tmp_path / "a")
    source.story.setup.template = "Doomsville"
    assert template_of(source.story) == "Doomsville"
    root = tmp_path / "data"
    keep_pictures(
        "Doomsville",
        source.story.title,
        source.cast,
        lambda file: story_path(source.story.id, file, tmp_path / "a").read_bytes(),
        root=root,
    )
    kept = read_kept("Doomsville", root=root)
    assert kept is not None and kept.count == 4 and kept.kept_from == "Doomsville, mine"

    # The next story from the template: the portrait as the template has it,
    # unlabelled, and a picture of its own the kept story had removed.
    files = {"images/refs/p.png": png(10), "images/refs/x.png": png(250)}
    john = Character(
        id="john2",
        name="John",
        reference_images=[
            ImageRef(id="p", file="images/refs/p.png", caption="portrait"),
            ImageRef(id="x", file="images/refs/x.png", caption="template's own"),
        ],
    )
    added = apply_kept(kept, [john], files.get, files.__setitem__, root=root)
    assert added == 3  # the portrait was the template's own: matched, not added
    assert [
        (r.id if r.id in "px" else "new", r.look, r.expression) for r in john.reference_images
    ] == [
        ("p", "winter coat", "neutral"),
        ("new", "winter coat", "angry"),
        ("new", "combat armour", "neutral"),
        ("new", "", ""),
        ("x", "", ""),
    ]
    assert files[john.reference_images[1].file] == png(50)
    assert [r.framing for r in john.reference_images] == ["head", "", "full", "", ""]
    assert john.sheet_looks == ["formal clothes"]
    # Another template's story takes nothing; forgotten, nothing is offered.
    assert read_kept("The Silverwood", root=root) is None
    assert forget_kept("Doomsville", root=root) and read_kept("Doomsville", root=root) is None


def test_kept_pictures_are_put_back_when_the_data_folder_is_reached_through_a_link(
    tmp_path: Path,
):
    """A home folder at /var/home behind /home, or a data folder moved to
    another disk and linked back: the kept folder, compared as written with
    each picture's resolved path, never held one, so nothing came back."""
    import pytest

    from sealedlore.storage.template_pictures import _folder, apply_kept, keep_pictures, read_kept

    real = tmp_path / "real"
    real.mkdir()
    try:
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic links here")
    root = tmp_path / "link" / "sealedlore"
    john = Character(
        name="John", reference_images=[ImageRef(file="images/refs/a.png", look="coat")]
    )
    keep_pictures("Doomsville", "Doomsville", [john], {"images/refs/a.png": png(30)}.get, root=root)
    kept = read_kept("Doomsville", root=root)
    assert kept is not None
    fresh, written = Character(name="John"), {}
    assert apply_kept(kept, [fresh], lambda file: None, written.__setitem__, root=root) == 1
    assert [ref.look for ref in fresh.reference_images] == ["coat"]
    assert list(written.values()) == [png(30)]

    # Still never a file outside the kept folder, however a row names it.
    outside = tmp_path / "outside.png"
    outside.write_bytes(png(60))
    folder = _folder("Doomsville", root)
    for row in kept.cards["John"]["pictures"]:
        row["file"], row["sha256"] = os.path.relpath(outside, folder), ""
        assert (folder / row["file"]).is_file()
    (folder / "elsewhere").symlink_to(outside)
    kept.cards["John"]["pictures"].append({"file": "elsewhere", "sha256": ""})
    other, written = Character(name="John"), {}
    assert apply_kept(kept, [other], lambda file: None, written.__setitem__, root=root) == 0
    assert written == {}


def test_a_story_remembers_its_template_through_restart_and_scenario_files(tmp_path: Path):
    from sealedlore.models.scenario import Scenario

    started = bundle_from_scenario(Scenario(title="Doomsville"), title="My Doomsville")
    assert started.story.setup.template == "Doomsville"
    save_story_bundle(started, root=tmp_path)
    again = fresh_playthrough(started, "Again", {})
    assert again.story.setup.template == "Doomsville"
    path = tmp_path / "mine.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(started, {}))
    assert bundle_from_scenario(read_scenario(path)).story.setup.template == "Doomsville"
