"""The sample stories: the start page and File → New… find them wherever
SealedLore is installed (the AppImage keeps
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
from sealedlore.storage.paths import PLOT_FORMAT_GUIDE, PLOT_WRITING_GUIDE, sample_stories

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "SampleStories"


def test_every_plot_file_but_the_guides_is_a_sample(tmp_path: Path):
    for name in (
        "Doomsville.md",
        PLOT_FORMAT_GUIDE,
        PLOT_WRITING_GUIDE,
        "Dust_Road.md",
        "notes.txt",
    ):
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
    # The writing guide ships beside them (the AppImage copies every .md
    # there) and is no story either.
    assert (SAMPLES / PLOT_WRITING_GUIDE).is_file()
    assert PLOT_WRITING_GUIDE not in [path.name for _, path in offered]
    for name, path in offered:
        parsed = parse_plot_markdown(path.read_text(encoding="utf-8"), fallback_title=name)
        assert parsed.ok, f"{path.name}: " + "; ".join(p.describe() for p in parsed.problems)


def test_the_samples_of_worlds_of_want_say_things_are_scarce():
    """`scarce: yes` helps the world designer in a world of want and makes a
    world without scarcity plainer, so only the samples set in one say it."""
    scarce = {
        path.name
        for _name, path in sample_stories(SAMPLES)
        if parse_plot_markdown(path.read_text(encoding="utf-8")).scenario.scarce
    }
    assert scarce == {"Doomsville with a map.md", "The Ashen Reach.md"}


def _quiet_armed(plot, chronicle, day: int = 1) -> set[str]:
    from sealedlore.engine.chronicle import armed

    days = {event.id: day for event in plot.events}
    chronicle.minutes = (day - 1) * 24 * 60 + 12 * 60
    return {
        item.event.title
        for item in armed(plot, chronicle, days, mentioned=set())
        if "begin" in item.kinds and item.event.pacing == "quiet" and item.event.kind == "optional"
    }


def test_the_samples_quiet_events_wait_for_their_moment():
    """A quiet-moment event whose conditions held from the start had the
    director asked before every passage from turn 1 (Brasshaven, The Ninth
    Furnace, the extended Doomsville; the review of Oct 20 2026). Each now
    waits for what its story needs first."""
    from sealedlore.engine.chronicle import happen, initial_chronicle, set_place

    def load(path: Path):
        return parse_plot_markdown(path.read_text(encoding="utf-8")).scenario.plot

    brass = load(SAMPLES / "Brasshaven.md")
    chronicle = initial_chronicle(brass)
    set_place(chronicle, "Tinker's Row")
    assert _quiet_armed(brass, chronicle) == set()
    chronicle.facts["automaton_repaired"] = "yes"
    assert _quiet_armed(brass, chronicle) == {"Tock speaks"}
    tock = brass.event("tock_speaks")
    happen(chronicle, tock, tock.variants[0], node_id="n", revealed=True)
    assert _quiet_armed(brass, chronicle) == {"An invitation from Lady Ashcombe"}

    furnace = load(SAMPLES / "The Ninth Furnace.md")
    chronicle = initial_chronicle(furnace)
    assert _quiet_armed(furnace, chronicle) == set()
    set_place(chronicle, "The Pilgrims' Rest")
    assert _quiet_armed(furnace, chronicle) == set()
    set_place(chronicle, None)  # out on the road
    assert _quiet_armed(furnace, chronicle) == {"The imp's bargain"}

    roads = load(ROOT / "tests" / "data" / "doomsville_extended.md")
    chronicle = initial_chronicle(roads)
    set_place(chronicle, "The Northern Camp")
    assert _quiet_armed(roads, chronicle, day=3) == set()
    set_place(chronicle, "Hellsville")
    assert _quiet_armed(roads, chronicle, day=1) == set()
    assert "Father Brennan's flock" in _quiet_armed(roads, chronicle, day=3)


def _begins_armed(plot, chronicle, mentioned: set[str] | None = None) -> set[str]:
    from sealedlore.engine.chronicle import armed

    days = {event.id: 1 for event in plot.events}
    return {
        item.event.title
        for item in armed(plot, chronicle, days, mentioned=mentioned or set())
        if "begin" in item.kinds and item.event.kind == "optional"
    }


def test_the_samples_any_time_events_wait_until_they_could_happen():
    """An "any time" event with nothing to wait for was offered to the director
    on every call of a journey (The Ninth Furnace's hollow and way out) or of
    a stay at home (Brasshaven's calculating engine, once Tock had spoken):
    live harness runs, Oct 2026. Each now waits for where or what it needs."""
    from sealedlore.engine.chronicle import happen, initial_chronicle, set_place

    def load(name: str):
        return parse_plot_markdown((SAMPLES / name).read_text(encoding="utf-8")).scenario.plot

    brass = load("Brasshaven.md")
    chronicle = initial_chronicle(brass)
    set_place(chronicle, "Tinker's Row")
    chronicle.facts["automaton_repaired"] = "yes"
    tock = brass.event("tock_speaks")
    happen(chronicle, tock, tock.variants[0], node_id="n", revealed=True)
    assert "The calculating engine" not in _begins_armed(brass, chronicle)
    # Named (the invitation to Ashcombe Hall), or there: armed.
    assert "The calculating engine" in _begins_armed(brass, chronicle, {"The Ashcombe Works"})
    set_place(chronicle, "The Ashcombe Works")
    assert "The calculating engine" in _begins_armed(brass, chronicle)

    furnace = load("The Ninth Furnace.md")
    chronicle = initial_chronicle(furnace)
    set_place(chronicle, "The Pilgrims' Rest")
    set_place(chronicle, None)  # out on the road
    armed_now = _begins_armed(furnace, chronicle)
    assert "The runaways' hollow" not in armed_now and "The way out" not in armed_now
    set_place(chronicle, "The ash wastes")
    assert "The runaways' hollow" in _begins_armed(furnace, chronicle)
    # The way out once someone who knows has come into the story.
    assert "The way out" not in _begins_armed(furnace, chronicle)
    hollow = furnace.event("the_runaways_hollow")
    happen(chronicle, hollow, hollow.variants[0], node_id="n", revealed=True)
    assert "The way out" in _begins_armed(furnace, chronicle)
    chronicle = initial_chronicle(furnace)
    set_place(chronicle, "The Pilgrims' Rest")
    set_place(chronicle, None)
    bargain = furnace.event("the_imp_s_bargain")
    happen(chronicle, bargain, bargain.variants[0], node_id="n", revealed=True)
    assert "The way out" in _begins_armed(furnace, chronicle)


def test_both_doomsville_samples_play_the_same_plot():
    """The map sample is Doomsville on a map, so a fix to one's facts or
    events belongs in both: `has_companions` flipped yes on a stranger met
    on the road or a shared meal until its meaning said what doesn't count
    (Oct 21 2026)."""
    from tests.test_plot_file import normalised

    def plot_of(name: str) -> dict:
        scenario = parse_plot_markdown((SAMPLES / name).read_text(encoding="utf-8")).scenario
        return normalised(scenario)["plot"]

    plain, mapped = plot_of("Doomsville.md"), plot_of("Doomsville with a map.md")
    assert plain == mapped
    companions = next(f for f in plain["facts"] if f["name"] == "has_companions")
    assert "meeting someone" in companions["meaning"] and companions["watched"]


def test_doomsville_s_bandits_are_never_offered_in_town_and_keep_their_days():
    """The director was asked about the roadside ambush on every turn the
    character spent in Hellsville, where it can't begin. The gate is on a
    variant, not the event: an optional event's own conditions that still
    hold when its last day passes keep it waiting past it, and the ambush
    is for days 2 to 60 only."""
    from sealedlore.engine.chronicle import (
        armed,
        clock_minutes,
        initial_chronicle,
        resolve_due,
        set_place,
    )

    def offered(plot, chronicle) -> bool:
        found = armed(plot, chronicle, {}, mentioned=set())
        return "first_bandit_meeting" in {item.event.id for item in found}

    for name in ("Doomsville.md", "Doomsville with a map.md"):
        plot = parse_plot_markdown((SAMPLES / name).read_text(encoding="utf-8")).scenario.plot
        chronicle = initial_chronicle(plot)
        chronicle.minutes = clock_minutes(10, 12, 0)
        set_place(chronicle, "Hellsville")
        assert not offered(plot, chronicle), name
        set_place(chronicle, None)  # out on the road
        assert offered(plot, chronicle), name
        chronicle.minutes = clock_minutes(61, 12, 0)
        resolve_due(plot, chronicle, {}, node_id=None)
        assert chronicle.events["first_bandit_meeting"].state == "missed", name
        assert not offered(plot, chronicle), name


def _names_entry(text: str, entry) -> bool:
    """Whether a text names a lore entry: its title, or a keyword of more than
    one word, articles aside. A one-word keyword ("the singing", "the gate")
    is the story's everyday word, not the secret."""
    import re

    for form in (entry.title, *entry.keywords):
        words = re.sub(r"^(?:the|a|an)\s+", "", form.strip(), flags=re.IGNORECASE)
        if form is not entry.title and len(words.split()) < 2:
            continue
        if words and re.search(rf"(?<!\w){re.escape(words)}(?!\w)", text, re.IGNORECASE):
            return True
    return False


def _brought_by(event) -> set[str]:
    """What an event has certainly brought in once it has happened. Whoever
    an offscreen way brings joins only when the character learns of it."""
    if any(variant.offscreen for variant in event.variants):
        return set()
    ways = [set(variant.brings) | set(variant.reveals) for variant in event.variants]
    return set(event.brings) | set(event.reveals) | (set.intersection(*ways) if ways else set())


def _known_once(plot, conditions, seen: frozenset[str] = frozenset()) -> set[str]:
    """Hidden ids certainly brought in by the time the conditions hold: what
    an event they need has brought, a hidden place they stand the character
    in (and whatever revealed it), and what the events that alone set a fact
    to the value they ask have brought."""

    def after(event) -> set[str]:
        return _brought_by(event) | _known_once(plot, event.requires, seen | {event.id})

    def all_of(events) -> set[str]:
        events = [event for event in events if event.id not in seen]
        return set.intersection(*(after(event) for event in events)) if events else set()

    known: set[str] = set()
    for condition in conditions:
        if condition.event_id and condition.happened and condition.event_id not in seen:
            known |= after(plot.event(condition.event_id))
        elif condition.place and condition.op == "=":
            place = next((p for p in plot.places if p.name == condition.place), None)
            if place is not None and place.lore_id:
                known.add(place.lore_id)
                known |= all_of(
                    event
                    for event in plot.events
                    if place.lore_id in event.reveals
                    or any(place.lore_id in variant.reveals for variant in event.variants)
                )
        elif condition.fact and condition.op == "=":
            fact = plot.fact(condition.fact)
            if fact is None or fact.watched or condition.value == fact.initial:
                continue
            known |= all_of(
                event
                for event in plot.events
                if any(
                    (a.fact, a.value) == (condition.fact, condition.value)
                    for a in (a for variant in event.variants for a in variant.sets)
                )
            )
    return known


@pytest.mark.parametrize(
    "path",
    [path for _, path in sample_stories(SAMPLES)]
    + [ROOT / "tests" / "data" / "doomsville_extended.md"],
    ids=lambda path: path.stem,
)
def test_no_event_names_what_is_still_hidden(tmp_path: Path, path: Path):
    """An event's own text is what a lead-in shows, days ahead, with only the
    names of whoever it brings taken out, and it opens the passage the event
    begins in, a variant's tell after it. A hidden character or lore entry
    either names, that nothing which must have happened by then has brought
    in, reaches the storyteller early and with no card: Brasshaven's night of
    the automatons named the hidden calculating engine, and the Silverwood's
    Seal breaking named the hidden Seal of Thorns. A name the world or the
    opening already uses is no secret."""
    from sealedlore.engine.chronicle import introduced
    from sealedlore.engine.director import direction_text
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.validators import mentions
    from sealedlore.models.config import Config
    from sealedlore.models.plot import Direction
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario

    scenario = parse_plot_markdown(path.read_text(encoding="utf-8")).scenario
    plot = scenario.plot
    bundle = bundle_from_scenario(scenario)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle, Config(), MockChatProvider(), root=tmp_path, learn_corrections=False
    )
    scene = scenario.starting_scene
    told = "\n".join(
        [scenario.world_bible or "", scenario.opening_text, scene.location, scene.situation]
    )
    people = [c for c in (*scenario.cast, *scenario.supporting) if c.plot_hidden]
    lore = [e for e in scenario.lore if e.plot_hidden]
    secrets = [c for c in people if not mentions(told, c)] + [
        e for e in lore if not _names_entry(told, e)
    ]
    assert people or lore

    def named(text: str, allowed: set[str]) -> list[str]:
        return [
            getattr(item, "name", None) or item.title
            for item in secrets
            if item.id not in allowed
            and (_names_entry(text, item) if item in lore else mentions(text, item))
        ]

    problems: list[str] = []
    for event in plot.events:
        before = _known_once(plot, event.requires)
        if event.lead_in:
            lead = session._unnamed_lead_in(
                Direction(
                    event_id=event.id,
                    kind="lead_in",
                    text=direction_text(event, "lead_in", None),
                )
            )
            problems += [f"{event.title} (lead-in): {n}" for n in named(lead.text, before)]
        for variant in event.variants:
            if variant.offscreen:
                continue
            allowed = before | _known_once(plot, variant.requires) | set(introduced(event, variant))
            text = direction_text(event, "begin", variant)
            way = variant.title or "begin"
            problems += [f"{event.title} ({way}): {n}" for n in named(text, allowed)]
    assert problems == []


def test_the_ninth_furnace_s_places_answer_to_their_own_names_only(tmp_path: Path):
    """A place's keywords wake its `at:` events and choose its lore. "The
    citadel" is the story's everyday word for where Maren's brother is held,
    so every passage about going there offered his event at the Iron Citadel
    by mention alone; "the Rest" answered to "the rest of". Both places are
    still found by what names them, and their lore with them."""
    from sealedlore.engine.retrieval import select_lore
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario

    path = SAMPLES / "The Ninth Furnace.md"
    bundle = bundle_from_scenario(parse_plot_markdown(path.read_text(encoding="utf-8")).scenario)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle, Config(), MockChatProvider(), root=tmp_path, learn_corrections=False
    )
    named = session._places_named
    citadel, rest = "The Iron Citadel", "The Pilgrims' Rest"
    assert named("We walk the rest of the way. The Citadel can wait.") == set()
    assert named("Smoke rises over the Iron Citadel.") == {citadel}
    assert named("They call it Malgareth's fortress.") == {citadel}
    assert named("Back at Pilgrims' Rest the drums start.") == {rest}
    assert named("The waystation's gates are chained.") == {rest}

    def chosen(turn: str) -> set[str]:
        report = select_lore(bundle.lore, keyword_text=turn, count_tokens=len)
        return {injection.entry.title for injection in report.injected}

    assert {citadel, rest} <= chosen("From the Pilgrims' Rest we can see the Iron Citadel.")
    assert chosen("The rest of them follow.") == set()


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

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


def _samples_offered(chooser) -> dict[str, object]:
    return {
        key.split(":", 1)[1]: card
        for key, card in chooser.cards.items()
        if key.startswith("sample:")
    }


def test_the_start_page_offers_each_sample_and_picks_up_a_new_one(window, samples: Path, app):
    """With no story open the start page shows every sample with its title,
    its line and its picture; one dropped into the folder appears."""
    shutil.copytree(SAMPLES / "pictures", samples / "pictures")
    window.reload_transcript()
    assert window.centre_stack.currentWidget() is window.start_page
    offered = _samples_offered(window.start_page)
    assert list(offered) == ["Doomsville.md"]
    card = offered["Doomsville.md"]
    assert card.title.text() == "Doomsville"
    assert card.text.text().startswith("Four years after the dead rose")
    window.start_page._show_pictures()
    assert card.picture is not None and not card.picture.pixmap().isNull()
    shutil.copy(samples / "Doomsville.md", samples / "Dust_Road.md")
    window.reload_transcript()
    offered = _samples_offered(window.start_page)
    assert set(offered) == {"Doomsville.md", "Dust_Road.md"}
    offered["Dust_Road.md"].activated.emit()
    assert window.imported == [samples / "Dust_Road.md"]


def test_with_no_samples_none_are_offered(window, samples: Path):
    (samples / "Doomsville.md").unlink()
    window.reload_transcript()
    assert not _samples_offered(window.start_page)
    assert {"premise", "blank", "chat", "import"} <= set(window.start_page.cards)


def test_file_new_offers_the_samples_too(window, samples: Path, monkeypatch):
    from sealedlore.gui import main_window as main_window_module

    seen = []

    def choose(dialog) -> int:
        seen.append(set(dialog.chooser.cards))
        _samples_offered(dialog.chooser)["Doomsville.md"].activated.emit()
        return 1

    monkeypatch.setattr(main_window_module.NewDialog, "exec", choose)
    window.stories.new_story_requested.emit()  # New… under the Stories list
    assert {"premise", "blank", "chat", "import", "sample:Doomsville.md"} <= seen[0]
    # File → New… has no stories played last: the start page has them.
    assert not any(key.startswith("open:") for key in seen[0])
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
        places = {p.subject_id for p in with_files.pictures if p.subject != "walker"}
        assert places <= ids
        walkers = {p.subject_id for p in with_files.pictures if p.subject == "walker"}
        assert walkers <= {c.name for c in scenario.cast}
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
    assert view.iso_bar.offer.text() == "The Ashcombe Works: its painting."
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


def test_the_writing_guide_s_place_gate_is_doomsville_s_own():
    """The writing guide's example of a place gate is Doomsville's ambush as
    it ships, so a change to the one is made to the other."""
    import re

    guide = (SAMPLES / PLOT_WRITING_GUIDE).read_text(encoding="utf-8")
    block = re.search(r"```markdown\n<!-- Good: never offered in town[^\n]*\n(.*?)```", guide, re.S)
    assert block is not None
    text = "# Places\n\n## Hellsville\nA town.\n\n# Events\n\n" + block.group(1)
    parsed = parse_plot_markdown(text)
    assert [p.message for p in parsed.problems if p.error] == []
    example = parsed.scenario.plot.events[0]
    sample = next(
        event
        for event in parse_plot_markdown(
            (SAMPLES / "Doomsville.md").read_text(encoding="utf-8")
        ).scenario.plot.events
        if event.title == example.title
    )

    def shape(event) -> tuple:
        ways = [(v.title, [c.model_dump() for c in v.requires]) for v in event.variants]
        return event.window, event.pacing, event.trigger, event.requires, ways

    assert shape(example) == shape(sample)
    assert example.variants[0].requires[0].place == "Hellsville"


def test_media_lists_only_the_views_the_map_shows(app, tmp_path: Path):
    """The review: Media's Map section listed (and Save all wrote) the views
    of places the map keeps under the fog, the vox relay among them."""
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_setup = lambda *a, **k: False
    window._import_plot_file(SAMPLES / "The Ashen Reach.md")
    window.refresh_images()
    items = [
        window.images_panel.map_list.item(i).text().split("\n")
        for i in range(window.images_panel.map_list.count())
    ]
    views = [parts[1] for parts in items if parts[0] == "Painting"]
    assert sorted(views) == ["Hollowmouth", "Shaft Nine"]
    # The sample's avatars are listed too (Oct 17 2026), one per character.
    assert sum(parts[0] == "Avatar" for parts in items) >= 2
    window._chat_allows_close = lambda: True
    window.close()


def test_a_map_sample_is_ready_to_play_on_its_map():
    """Map-first play (engine/area.py): a sample that keeps a map ships its
    tile set, every building with a plan furnished, a walking sprite for
    every character, and a sprite for every thing in every area, so nothing
    is drawn when it is played."""
    from sealedlore.engine.area import build_area
    from sealedlore.engine.tile_sets import item_slugs
    from sealedlore.storage.tile_sets import read_asset, shipped_sets
    from sealedlore.storage.world_art import load_sidecar, sidecar_art_path

    sets = shipped_sets(SAMPLES)
    for path in sorted(SAMPLES.glob("*.md")):
        if not sidecar_art_path(path).exists():
            continue
        art, _files = load_sidecar(path)
        scenario = parse_plot_markdown(path.read_text(encoding="utf-8")).scenario
        assert art.tile_set in sets, f"{path.name}: no tile set {art.tile_set!r}"
        _info, folder = sets[art.tile_set]
        world = applied(WorldMap(), scenario.world_base, base=True)
        walkers = {p.subject_id for p in art.pictures if p.subject == "walker"}
        assert walkers == {c.name for c in scenario.cast}, f"{path.name}: walkers"
        names = [c.name for c in scenario.cast]
        for pid in art.layouts:
            area = build_area(world, art, pid, people=names)
            if area is None:
                continue
            if area.indoor and world.places[pid].kind in ("building", "room") and area.rooms:
                assert art.furnishing_for(pid, "") is not None, f"{area.name}: not furnished"
            for thing in area.placements:
                assert any(read_asset(folder, f"items/{s}") for s in item_slugs(thing.name)), (
                    f"{path.name}: {area.name}: no sprite for {thing.name!r}"
                )


def test_every_building_on_a_sample_s_map_can_be_entered_and_left(tmp_path: Path):
    """The author (Oct 18 2026): John couldn't leave AfterDark's farmhouse,
    and no story, sample or new, should have a building you can't get into
    and out of. Every place with an inside drawn: a door in from the ground
    round it, every way out onto ground, a way out walkable from the door."""
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.world_art import is_deck
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import load_story_bundle, save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario
    from sealedlore.storage.world_art import load_sidecar, sidecar_art_path
    from tests.test_open_country import ways_in_and_out

    checked, problems = 0, []
    for path in sorted(SAMPLES.glob("*.md")):
        if not sidecar_art_path(path).exists():
            continue
        art, _files = load_sidecar(path)
        scenario = parse_plot_markdown(path.read_text(encoding="utf-8")).scenario
        bundle = bundle_from_scenario(scenario)
        bundle.world_art = art
        save_story_bundle(bundle, root=tmp_path)
        session = StorySession(
            load_story_bundle(bundle.story.id, root=tmp_path),
            Config(map_art=False),
            MockChatProvider(),
            root=tmp_path,
            learn_corrections=False,
        )
        session.save = lambda *a, **k: None
        session.story.held_character_id = session.story.held_character_id or session.cast[0].id
        world, art = session.world(), session.map_art_copy()
        for pid, place in world.places.items():
            if place.kind not in ("building", "room", "vehicle"):
                continue
            if art.layout_shown(pid, place.state) is None or is_deck(world, art, pid):
                continue
            if not session.area(pid, world, art).indoor:
                continue
            checked += 1
            problems += [f"{path.stem}: {p}" for p in ways_in_and_out(session, pid)]
    assert checked >= 20
    assert problems == []


def test_a_village_in_a_forest_has_a_way_out_on_every_side(tmp_path: Path):
    """The review (Oct 20 2026): The Silverwood's Fernhollow could not be
    left by its west edge: none of the village's ground reached it, and the
    land past it was solid trees. An outdoor plan has a way cut to each edge
    (`area.edges_open`, as a region of open country has), and the ground is
    cleared round a place's yard on the land (`area.ways_round`)."""
    from sealedlore.engine.area import path as walk
    from sealedlore.engine.session import StorySession
    from sealedlore.models.area import Spot
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import load_story_bundle, save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario
    from sealedlore.storage.world_art import load_sidecar

    sample = SAMPLES / "The Silverwood.md"
    art, _files = load_sidecar(sample)
    bundle = bundle_from_scenario(parse_plot_markdown(sample.read_text(encoding="utf-8")).scenario)
    bundle.world_art = art
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        load_story_bundle(bundle.story.id, root=tmp_path),
        Config(map_art=False),
        MockChatProvider(),
        root=tmp_path,
        learn_corrections=False,
    )
    session.save = lambda *a, **k: None
    session.story.held_character_id = session.story.held_character_id or session.cast[0].id
    held = session.held_name()
    start = session.area_state().spots[held]
    village = session.area(start.area)
    assert village.name == "Fernhollow"
    assert any("west edge" in r for r in village.repairs)
    edges = {
        "north": [(x, 0) for x in range(village.width)],
        "south": [(x, village.height - 1) for x in range(village.width)],
        "west": [(0, y) for y in range(village.height)],
        "east": [(village.width - 1, y) for y in range(village.height)],
    }
    for side, tiles in edges.items():
        reached = [t for t in tiles if walk(village, (start.x, start.y), t)]
        assert reached, f"no way from the green to the {side} edge"
        x, y = reached[len(reached) // 2]
        state = session.area_state()
        state.spots[held] = Spot(area=village.id, x=x, y=y)
        session._set_live(state)
        assert session.cross_edge(side) is not None, f"the way {side} is barred"
        landed = session.area_state().spots[held]
        land = session.area(landed.area)
        assert land.window and land.walkable(landed.x, landed.y)
        state = session.area_state()
        state.spots[held] = start
        session._set_live(state)
