"""Every file a character card has (the V3 spec's `assets`), kept: read from a
CHARX, a PNG or JSON (storage/tavern_cards.py), kept in Your characters
(storage/collection.py), out again whole in a CHARX, and put to use: the
card's background behind what is made from it, and the pictures a greeting
shows under it (storage/card_files.py). The cards here are made up, in the
shape RisuAI writes, from the spec: never one from a card site."""

from __future__ import annotations

import base64
import json
import zipfile
import zlib
from pathlib import Path

from sealedlore.engine import card_link
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.image import ImageRef
from sealedlore.models.tavern import CardAsset
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage import collection
from sealedlore.storage.card_files import background_of, greeting_pictures
from sealedlore.storage.images import reference_files
from sealedlore.storage.paths import COLLECTION_ID
from sealedlore.storage.picture_store import DiskPictures
from sealedlore.storage.png_text import with_text
from sealedlore.storage.repository import load_story_bundle, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario, scenario_from_bundle
from sealedlore.storage.tavern_cards import card_charx, card_data, read_card, scenario_from_card
from tests.test_private import make_config
from tests.test_tavern import ELENA


def png_of(red: int) -> bytes:
    """A 1x1 PNG of its own colour, so pictures can be told apart after
    their hidden data is stripped (which drops anything added after one)."""

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


FACE, BARN, FARMHOUSE, SNOW, TINY = png_of(1), png_of(2), png_of(3), png_of(4), png_of(5)
WIND = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 32  # a sound, as it came
GREETING = (
    "{{char}} lowers the rifle an inch.\n\n{{img::Farmhouse}}\n\n"
    '<img src="{{raw::snow.png}}" style="width:100%">\n'
    '"Who sent you, {{user}}?" {{audio::wind}}'
)
ASSETS = [
    {"type": "icon", "uri": "embeded://assets/icon/images/main.png", "name": "main", "ext": "png"},
    {
        "type": "background",
        "uri": "embeded://assets/background/images/barn.png",
        "name": "main",
        "ext": "png",
    },
    {
        "type": "x-risu-asset",
        "uri": "embeded://assets/other/images/farmhouse.png",
        "name": "Farmhouse",
        "ext": "png",
    },
    {
        "type": "x-risu-asset",
        "uri": "embeded://assets/other/images/snow.png",
        "name": "snow",
        "ext": "png",
    },
    {
        "type": "x-risu-asset",
        "uri": "embeded://assets/other/audio/wind.mp3",
        "name": "wind",
        "ext": "mp3",
    },
    {"type": "user_icon", "uri": "https://example.com/you.png", "name": "you", "ext": "png"},
    {
        "type": "x-risu-asset",
        "uri": "data:image/png;base64," + base64.b64encode(TINY).decode(),
        "name": "tiny",
        "ext": "png",
    },
    {
        "type": "x-risu-asset",
        "uri": "embeded://assets/other/images/gone.png",
        "name": "gone",
        "ext": "png",
    },
]
MEMBERS = {
    "assets/icon/images/main.png": FACE,
    "assets/background/images/barn.png": BARN,
    "assets/other/images/farmhouse.png": FARMHOUSE,
    "assets/other/images/snow.png": SNOW,
    "assets/other/audio/wind.mp3": WIND,
}


def risu_charx(tmp_path: Path) -> Path:
    data = {**ELENA, "first_mes": GREETING, "assets": ASSETS}
    path = tmp_path / "elena.charx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("card.json", json.dumps({"spec": "chara_card_v3", "data": data}))
        for member, raw in MEMBERS.items():
            archive.writestr(member, raw)
    return path


def store(root: Path) -> DiskPictures:
    return DiskPictures(COLLECTION_ID, root)


# --- read ------------------------------------------------------------------------------


def test_every_file_on_a_card_is_read(tmp_path: Path):
    card = read_card(risu_charx(tmp_path))
    assert card.picture == FACE
    found = {f.name: f for f in card.files}
    assert [f.name for f in card.files] == ["main", "Farmhouse", "snow", "wind", "you", "tiny"]
    assert (found["main"].type, found["main"].data) == ("background", BARN)
    assert found["Farmhouse"].path == "assets/other/images/farmhouse.png"
    assert found["wind"].data == WIND and found["wind"].ext == "mp3"  # as it came
    # A link is kept as the card's link, never fetched; a data: URL is read.
    assert (found["you"].uri, found["you"].data) == ("https://example.com/you.png", None)
    assert found["tiny"].data == TINY
    # The one the card names but doesn't hold is said.
    assert any("couldn't be read" in note for note in card.expression_notes)


def test_a_png_cards_files_come_from_its_chunks(tmp_path: Path):
    data = {
        **ELENA,
        "assets": [
            {"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"},
            {"type": "background", "uri": "__asset:1", "name": "main", "ext": "png"},
        ],
    }
    texts = {
        "ccv3": base64.b64encode(json.dumps({"spec": "chara_card_v3", "data": data}).encode()),
        "chara-ext-asset_:1": base64.b64encode(BARN),
    }
    path = tmp_path / "elena.png"
    path.write_bytes(with_text(FACE, {k: v.decode() for k, v in texts.items()}))
    card = read_card(path)
    assert card.picture == FACE
    [barn] = card.files
    assert (barn.type, barn.data) == ("background", BARN)


def test_a_json_cards_files_come_from_data_urls(tmp_path: Path):
    data = {
        **ELENA,
        "assets": [
            {
                "type": "icon",
                "uri": "data:image/png;base64," + base64.b64encode(FACE).decode(),
                "name": "main",
                "ext": "png",
            },
            {
                "type": "background",
                "uri": "data:image/png;base64," + base64.b64encode(BARN).decode(),
                "name": "barn",
                "ext": "png",
            },
        ],
    }
    path = tmp_path / "elena.json"
    path.write_text(json.dumps({"spec": "chara_card_v3", "data": data}))
    card = read_card(path)
    assert card.picture == FACE
    assert [(f.type, f.data) for f in card.files] == [("background", BARN)]


# --- kept, copied, forgotten, out again --------------------------------------------------


def kept_elena(tmp_path: Path):
    root = tmp_path / "data"
    card = read_card(risu_charx(tmp_path))
    character, notes = collection.kept_from_card(card, store(root))
    return root, character, notes


def test_kept_in_your_characters_with_every_file(tmp_path: Path):
    root, elena, notes = kept_elena(tmp_path)
    assets = {a.name: a for a in elena.tavern.assets}
    assert len(elena.tavern.assets) == 6
    held = [a for a in elena.tavern.assets if a.held]
    assert all(a.file.startswith(f"{collection.FILES_DIR}/") for a in held)
    assert store(root).read(assets["Farmhouse"].file) == FARMHOUSE
    assert store(root).read(assets["wind"].file) == WIND
    assert not assets["you"].held and assets["you"].uri == "https://example.com/you.png"
    said = " ".join(notes)
    assert "kept with the card" in said and "left out" not in said
    assert "1 background, 3 other pictures and 1 other file" in said
    assert "1 file is a link" in said


def test_a_kept_card_goes_out_whole_as_a_charx(tmp_path: Path):
    root, elena, _notes = kept_elena(tmp_path)
    pictures = [(ref, store(root).read(ref.file)) for ref in elena.reference_images]
    files = [(a, store(root).read(a.file) if a.held else None) for a in elena.tavern.assets]
    _v2, v3 = card_data(elena)
    out = tmp_path / "out.charx"
    out.write_bytes(card_charx(v3, pictures, files))
    again = read_card(out)
    assert again.picture == FACE
    before = [(f.type, f.name, f.data, f.uri) for f in read_card(risu_charx(tmp_path)).files]
    after = [(f.type, f.name, f.data, f.uri) for f in again.files]
    assert after == before
    # Under the paths the card had them, where they were safe to write.
    with zipfile.ZipFile(out) as archive:
        assert "assets/other/audio/wind.mp3" in archive.namelist()


def test_a_copy_and_a_version_have_files_of_their_own(tmp_path: Path):
    root, elena, _notes = kept_elena(tmp_path)
    bundle = collection.load_collection(root)
    bundle.cast.append(elena)
    copy = collection.copied(elena, store(root))
    bundle.cast.append(copy)
    mine = {a.file for a in elena.tavern.assets if a.held}
    theirs = {a.file for a in copy.tavern.assets if a.held}
    assert mine and not mine & theirs
    farm = next(a for a in copy.tavern.assets if a.name == "Farmhouse")
    assert store(root).read(farm.file) == FARMHOUSE
    # Forgetting one gives back its own files, never the other's.
    gone = collection.forget(bundle, elena)
    assert mine <= set(gone) and not theirs & set(gone)


def test_each_characters_files_are_in_a_folder_of_their_own_under_the_cards_names(
    tmp_path: Path,
):
    root, elena, _notes = kept_elena(tmp_path)
    folder = f"{collection.FILES_DIR}/{elena.id}/"
    files = {a.name: a.file for a in elena.tavern.assets if a.held}
    assert all(file.startswith(folder) for file in files.values())
    assert files["Farmhouse"] == f"{folder}Farmhouse.png"
    assert files["wind"] == f"{folder}wind.mp3"
    copy = collection.copied(elena, store(root))
    assert all(
        a.file.startswith(f"{collection.FILES_DIR}/{copy.id}/")
        for a in copy.tavern.assets
        if a.held
    )


def test_files_kept_in_one_folder_before_move_into_their_characters(tmp_path: Path):
    """3.0.0b1 kept every character's files in images/assets itself."""
    root, elena, _notes = kept_elena(tmp_path)
    pictures = store(root)
    held = [a for a in elena.tavern.assets if a.held]
    for number, asset in enumerate(held):
        data = pictures.read(asset.file)
        pictures.remove(asset.file)
        asset.file = f"{collection.FILES_DIR}/old{number}.{asset.ext}"
        pictures.write(asset.file, data)
    old = [a.file for a in held]
    lost = held[-1]
    pictures.remove(lost.file)  # one that can't be read stays where it is
    gone = collection.settle_files([elena], pictures)
    assert gone == old[:-1]
    folder = f"{collection.FILES_DIR}/{elena.id}/"
    assert all(a.file.startswith(folder) for a in held[:-1]) and lost.file == old[-1]
    farm = next(a for a in held if a.name == "Farmhouse")
    assert pictures.read(farm.file) == FARMHOUSE
    # The old ones are still there until the window has saved the new places.
    assert pictures.read(old[0]) is not None
    assert collection.settle_files([elena], pictures) == []


def test_a_characters_folder_goes_with_its_last_file(tmp_path: Path):
    root, elena, _notes = kept_elena(tmp_path)
    folder = root / "characters" / collection.FILES_DIR / elena.id
    assert folder.is_dir()
    for asset in elena.tavern.assets:
        if asset.held:
            store(root).remove(asset.file)
    assert not folder.exists()
    assert folder.parent.is_dir(), "images/assets itself stays"


def test_a_storys_copy_holds_none_of_the_cards_files(tmp_path: Path):
    _root, elena, _notes = kept_elena(tmp_path)
    copy = card_link.story_copy(elena, [elena], "John")
    assert copy.tavern.assets == []
    assert len(elena.tavern.assets) == 6  # the card keeps them


# --- a greeting's pictures, the background --------------------------------------------


def test_a_greetings_pictures_are_found_and_taken_out_of_its_text():
    farm = CardAsset(type="x-risu-asset", name="Farmhouse", ext="png", file="images/assets/a.png")
    snow = CardAsset(type="x-risu-asset", name="snow.png", ext="png", file="images/assets/b.png")
    porch = CardAsset(
        type="other",
        name="porch",
        ext="webp",
        file="images/assets/c.webp",
        path="assets/other/images/porch.webp",
    )
    wind = CardAsset(type="x-risu-asset", name="wind", ext="mp3", file="images/assets/d.mp3")
    barn = CardAsset(type="background", name="barn", ext="png", file="images/assets/e.png")
    happy = ImageRef(id="h", file="images/refs/h.png", expression="Happy")
    text = (
        "She waits.\n\n{{IMG::farmhouse}}\n\n"  # any case
        '<img src="{{raw::snow}}">\n'  # its name without the extension
        "![porch](embeded://assets/other/images/porch.webp)\n"
        "{{img::farmhouse}} {{audio::wind}} {{bg::barn}} {{emotion::happy}}\n"
        '<img src="https://example.com/x.png"> {{img::nobody}}\n\n\n"Who are you?"'
    )
    shown = greeting_pictures(text, [farm, snow, porch, wind, barn], [happy])
    assert shown.pictures == [farm, snow, porch, happy]  # in order, each once, the sound not
    assert shown.background is barn
    # A link stays as written; everything of the card's is taken out.
    assert shown.text == ('She waits.\n\n<img src="https://example.com/x.png">\n\n"Who are you?"')


def test_the_main_background_is_the_one_named_main():
    first = CardAsset(type="background", name="dusk", ext="png", file="images/assets/a.png")
    main = CardAsset(type="background", name="main", ext="png", file="images/assets/b.png")
    sound = CardAsset(type="background", name="main", ext="mp3", file="images/assets/c.mp3")
    assert background_of([first, main]) is main
    assert background_of([first]) is first
    assert background_of([sound]) is None


def test_a_story_from_the_card_has_its_background_and_its_greetings_pictures(tmp_path: Path):
    card = read_card(risu_charx(tmp_path))
    made = scenario_from_card(card, player="John")
    scenario = made.scenario
    assert "{{img::" not in scenario.opening_text and "<img" not in scenario.opening_text
    assert scenario.opening_text.startswith("Elena lowers the rifle an inch.")
    names = [p.card_file for p in scenario.opening_pictures]
    assert names == ["Farmhouse", "snow"]
    assert all(p.from_card == "Elena" for p in scenario.opening_pictures)
    assert all(p.file in scenario.reference_files for p in scenario.opening_pictures)
    assert scenario.background_image in scenario.reference_files
    assert base64.b64decode(scenario.reference_files[scenario.background_image]) == BARN
    assert any("background is behind the story" in note for note in made.notes)
    assert any("under the opening" in note for note in made.notes)


def test_the_opening_shows_them_once_and_a_restart_keeps_them(tmp_path: Path):
    card = read_card(risu_charx(tmp_path))
    bundle = bundle_from_scenario(scenario_from_card(card, player="John").scenario)
    john = next(c for c in bundle.cast if c.name == "John")
    save_story_bundle(bundle, root=tmp_path)
    config = make_config()
    config.scene_reads = "manual"
    session = StorySession(
        load_story_bundle(bundle.story.id, root=tmp_path),
        config,
        MockChatProvider([]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.begin(john.id))
    opening = session.full_path()[0]
    assert session.show_opening_pictures() is True
    shown = [i for i in session.generated_images() if i.from_card]
    assert [i.card_file for i in shown] == ["Farmhouse", "snow"]
    assert all(i.anchor_node_id == opening.id for i in shown)
    assert session.pictures.read(shown[0].file) == FARMHOUSE
    # Once: opened again, or after another job, nothing is added twice.
    assert session.show_opening_pictures() is False
    # A restart (a scenario of the story) still has them to show.
    files = reference_files(session.bundle, tmp_path)
    again = scenario_from_bundle(session.bundle, files)
    assert [p.card_file for p in again.opening_pictures] == ["Farmhouse", "snow"]
    assert all(p.file in again.reference_files for p in again.opening_pictures)
