"""The sample stories: File → New story from a sample and the start page's
Try a sample… find them wherever SealedLore is installed (the AppImage keeps
them at a mount point Import… could never find), and a sample added to
SampleStories/ is offered with no other change."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.world import applied
from sealedlore.engine.world_art import local_area
from sealedlore.models.world import WorldMap
from sealedlore.storage import paths
from sealedlore.storage.paths import PLOT_FORMAT_GUIDE, sample_stories

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "SampleStories"


def test_every_plot_file_but_the_guide_is_a_sample(tmp_path: Path):
    for name in ("Doomsville.md", PLOT_FORMAT_GUIDE, "Dust_Road.md", "notes.txt"):
        (tmp_path / name).write_text("# x\n", encoding="utf-8")
    (tmp_path / "drafts.md").mkdir()
    assert [(name, path.name) for name, path in sample_stories(tmp_path)] == [
        ("Doomsville", "Doomsville.md"),
        ("Dust Road", "Dust_Road.md"),
    ]


def test_no_samples_folder_is_no_samples(monkeypatch):
    monkeypatch.setattr(paths, "samples_dir", lambda: None)
    assert sample_stories() == []


def test_the_shipped_samples_are_offered_and_import_cleanly():
    """Every sample shipped is offered and parses without an error, so a
    sample added later that the importer would refuse fails here, not in a
    new user's first five minutes."""
    offered = sample_stories(SAMPLES)
    assert "Doomsville" in [name for name, _ in offered]
    assert PLOT_FORMAT_GUIDE not in [path.name for _, path in offered]
    for name, path in offered:
        parsed = parse_plot_markdown(path.read_text(encoding="utf-8"), fallback_title=name)
        assert parsed.ok, f"{path.name}: " + "; ".join(p.describe() for p in parsed.problems)


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def samples(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "samples"
    folder.mkdir()
    shutil.copy(SAMPLES / "Doomsville.md", folder)
    shutil.copy(SAMPLES / PLOT_FORMAT_GUIDE, folder)
    monkeypatch.setenv("SEALEDLORE_SAMPLES", str(folder))
    return folder


@pytest.fixture
def window(app, tmp_path: Path, samples: Path, monkeypatch):
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    imported: list[Path] = []
    # Importing opens Setup, a modal dialog; what matters here is which file.
    monkeypatch.setattr(window, "_import_plot_file", imported.append)
    window.imported = imported
    yield window
    window.close()


def _items(window: MainWindow) -> list[str]:
    return [action.text() for action in window.samples_menu.actions()]


def test_the_menu_lists_the_samples_and_picks_up_a_new_one(window, samples: Path):
    assert _items(window) == ["Doomsville"]
    shutil.copy(samples / "Doomsville.md", samples / "Dust_Road.md")
    window.samples_menu.aboutToShow.emit()
    assert _items(window) == ["Doomsville", "Dust Road"]
    window.samples_menu.actions()[1].trigger()
    assert window.imported == [samples / "Dust_Road.md"]


def test_the_menu_says_when_there_are_none(window, samples: Path):
    (samples / "Doomsville.md").unlink()
    window.samples_menu.aboutToShow.emit()
    [only] = window.samples_menu.actions()
    assert only.text() == "No sample stories found" and not only.isEnabled()


def test_the_start_page_offers_a_sample(window, samples: Path):
    window.reload_transcript()
    buttons = {
        button.text(): button
        for button in window.transcript.findChildren(QPushButton)
        if button.objectName() == "placeholderButton"
    }
    assert "Try a sample…" in buttons
    # With one sample it is imported at once; with more, the list opens.
    buttons["Try a sample…"].click()
    assert window.imported == [samples / "Doomsville.md"]


def test_a_sample_s_shipped_map_art_fits_its_map():
    """A sample's world_art.json beside it (storage/world_art.py) names only
    places and things the sample's map has, so every import gets it whole."""
    from sealedlore.storage.world_art import load_sidecar, load_sidecar_art, sidecar_art_path

    for path in sorted(SAMPLES.glob("*.md")):
        if not sidecar_art_path(path).exists():
            continue
        art = load_sidecar_art(path)
        assert art is not None, f"{path.name}: its art can't be read"
        scenario = parse_plot_markdown(path.read_text(encoding="utf-8")).scenario
        assert scenario.world_base is not None
        ids = {p.id for p in scenario.world_base.places} | {
            t.id for t in scenario.world_base.things
        }
        drawn = (
            {s.subject_id for s in art.sprites}
            | set(art.layouts)
            | {t.place_id for t in art.terrain}
        )
        assert drawn <= ids, f"{path.name}: art for {drawn - ids}"
        assert art.terrain and art.layouts, f"{path.name}: nothing drawn"
        # Its map pictures (the isometric views) are files beside it, and
        # every one listed comes with its file.
        listed = json.loads(sidecar_art_path(path).read_text(encoding="utf-8"))["pictures"]
        with_files, files = load_sidecar(path)
        assert len(with_files.pictures) == len(listed), f"{path.name}: a map picture is missing"
        assert {p.subject_id for p in with_files.pictures} <= ids
        assert sorted(files) == sorted(p.file for p in with_files.pictures)
        # A showcase: every area with a plan comes with its isometric view
        # (the author, Oct 15 2026: several areas each, not the start alone).
        world = applied(WorldMap(), scenario.world_base, base=True)
        areas = {
            pid
            for pid in art.layouts
            if pid in world.places and (local_area(world, pid) or world.places[pid]).id == pid
        }
        viewed = {p.subject_id for p in with_files.pictures if p.subject == "isometric"}
        assert areas <= viewed, f"{path.name}: no view of {sorted(areas - viewed)}"


def test_every_sample_character_comes_with_its_portrait():
    """The samples ship a portrait of every character (the author, Oct 15
    2026), read as any plot file's pictures are: none missing, none refused,
    hidden data stripped."""
    from sealedlore.gui.ref_images import load_plot_pictures
    from sealedlore.storage.image_meta import strip_metadata

    offered = sample_stories(SAMPLES)
    assert offered
    for _name, path in offered:
        parsed = parse_plot_markdown(path.read_text())
        assert load_plot_pictures(parsed, path.parent) == [], path.name
        scenario = parsed.scenario
        for character in [*scenario.cast, *scenario.supporting]:
            assert character.reference_images, f"{path.name}: {character.name}"
            assert character.reference_images[0].caption == "portrait"
        for entry in scenario.lore:
            assert entry.reference_images, f"{path.name}: {entry.title}"
        for relative in scenario.reference_files:
            source = path.parent / "pictures" / Path(relative).name
            assert not source.exists() or strip_metadata(source.read_bytes()) == (
                source.read_bytes()
            )


def test_a_sample_s_map_shows_each_area_s_own_view_over_it(app, tmp_path: Path):
    """Brasshaven, looked at over the Ashcombe Works: Isometric lays the
    works' own view over the works (it used to show Tinker's Row, where the
    story is), each painted area is marked before Isometric is pressed."""
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_setup = lambda *a, **k: False
    window.resize(1600, 1000)
    window._import_plot_file(SAMPLES / "Brasshaven.md")
    session = window.session
    window.show_world(True)
    view = window.world_view
    works = next(p for p in session.world().places.values() if p.name == "The Ashcombe Works")
    view.go_to(works.id)
    assert view.area_in_view().id == works.id
    assert view._badges[works.id].isVisible() and not view._isos[works.id].isVisible()
    view.iso_button.setChecked(True)
    assert view._isos[works.id].isVisible()
    assert view._isos[works.id].sceneBoundingRect() == view._plans[works.id].rect()
    assert view.iso_bar.offer.text() == "The Ashcombe Works: its isometric view."
    window._chat_allows_close = lambda: True
    window.close()


def test_the_guide_s_front_matter_parses_as_written():
    """The review: copied as written, the guide's example front matter was an
    import error (its notes were `#`, read as part of each value)."""
    import re

    guide = (SAMPLES / PLOT_FORMAT_GUIDE).read_text(encoding="utf-8")
    block = re.search(r"## Settings at the top\s+```markdown\n(.*?)```", guide, re.DOTALL)
    assert block is not None
    text = block.group(1) + "\n# World\n\nA town.\n\n# Opening\n\nIt begins.\n"
    parsed = parse_plot_markdown(text)
    assert [p.message for p in parsed.problems if p.error] == []
    assert parsed.scenario.title == "Doomsville"


def test_media_lists_only_the_views_the_map_shows(app, tmp_path: Path):
    """The review: Media's Map section listed (and Save all wrote) the views
    of places the map keeps under the fog, the vox relay among them."""
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_setup = lambda *a, **k: False
    window._import_plot_file(SAMPLES / "The Ashen Reach.md")
    window.refresh_images()
    names = [
        window.images_panel.map_list.item(i).text().split("\n")[1]
        for i in range(window.images_panel.map_list.count())
    ]
    assert sorted(names) == ["Hollowmouth", "Shaft Nine"]
    window._chat_allows_close = lambda: True
    window.close()
