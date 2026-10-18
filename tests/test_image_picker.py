"""The image model picker (gui/image_picker.py): the text model picker's
search and table over the endpoint's image models. Offscreen; no network."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.image_jobs import ImageCatalog, listing_entry  # noqa: E402
from sealedlore.gui.image_picker import (  # noqa: E402
    AT_ONCE,
    PRICE,
    REFERENCES,
    SIZES,
    ImagePickerDialog,
)
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.providers.images import ImageModelInfo  # noqa: E402

MODELS = [
    ImageModelInfo(
        id="bytedance/seedream-v5.0-pro",
        name="Seedream 5.0 Pro",
        resolutions=("1k", "2k", "4k"),
        max_inputs=10,
        max_outputs=4,
        per_image={"1k": 0.03, "2k": 0.045, "4k": 0.09},
        description="Keeps characters as drawn across pictures.",
    ),
    ImageModelInfo(
        id="hidream",
        name="HiDream",
        resolutions=("1024x1024",),
        max_inputs=0,
        max_outputs=1,
        per_image={"1024x1024": 0.01},
    ),
    ImageModelInfo(id="black-forest-labs/flux-kontext", name="FLUX Kontext", max_inputs=1),
]


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def catalog(app) -> ImageCatalog:
    config = Config(image_models=[listing_entry(info) for info in MODELS])
    return ImageCatalog(config, None)  # no endpoint: ensure() fetches nothing


def shown(dialog: ImagePickerDialog) -> list[str]:
    table = dialog.table
    return [
        table.topLevelItem(i).text(0)
        for i in range(table.topLevelItemCount())
        if not table.topLevelItem(i).isHidden()
    ]


def test_the_image_picker_lists_what_matters_and_searches_all_of_it(app, catalog):
    dialog = ImagePickerDialog(catalog, current="hidream")
    rows = {dialog.table.topLevelItem(i).text(0): dialog.table.topLevelItem(i) for i in range(3)}
    seedream = rows["bytedance/seedream-v5.0-pro"]
    assert seedream.text(REFERENCES) == "up to 10" and rows["hidream"].text(REFERENCES) == "none"
    assert seedream.text(SIZES) == "1k, 2k, 4k"
    assert seedream.text(PRICE) == "0.030–0.090" and rows["hidream"].text(PRICE) == "0.010"
    assert seedream.text(AT_ONCE) == "4"
    assert "Keeps characters" in seedream.toolTip(0) and "4k $0.090" in seedream.toolTip(0)
    assert dialog.selected_model() == "hidream", "opens on the current model"
    assert "can't be given a picture" in dialog.detail.text()

    # Search reads the id, name, description and sizes, every word.
    dialog.search.setText("seedream")
    assert shown(dialog) == ["bytedance/seedream-v5.0-pro"]
    dialog.search.setText("4k")
    assert shown(dialog) == ["bytedance/seedream-v5.0-pro"]
    dialog.search.setText("characters drawn")
    assert shown(dialog) == ["bytedance/seedream-v5.0-pro"]
    dialog.search.setText("kontext")
    assert shown(dialog) == ["black-forest-labs/flux-kontext"]
    assert "1 of 3" in dialog.status.text()
    dialog.search.setText("")

    # Sorted by price as numbers, the unpriced last.
    dialog.table.sortByColumn(PRICE, Qt.AscendingOrder)
    assert shown(dialog) == [
        "hidream",
        "bytedance/seedream-v5.0-pro",
        "black-forest-labs/flux-kontext",
    ]
    dialog.deleteLater()


def test_refresh_fetches_on_the_catalogs_thread(app, catalog, monkeypatch):
    asked: list[bool] = []
    monkeypatch.setattr(catalog, "ensure", lambda force=False: asked.append(force))
    dialog = ImagePickerDialog(catalog)
    dialog.refresh_button.click()
    assert asked == [False, True]
    dialog.deleteLater()


def test_the_picture_dialog_and_settings_choose_through_it(app, catalog, monkeypatch, tmp_path):
    import sealedlore.gui.settings_dialog as settings_module
    from sealedlore.gui.settings_dialog import SettingsDialog

    monkeypatch.setattr(
        settings_module, "pick_image_model", lambda *_a: "bytedance/seedream-v5.0-pro"
    )
    dialog = SettingsDialog(catalog.config, None, image_catalog=catalog)
    assert not dialog.image_model.button.isHidden()
    dialog.image_model.button.click()
    assert dialog.image_model.text() == "bytedance/seedream-v5.0-pro"
    sizes = [dialog.image_size.itemText(i) for i in range(dialog.image_size.count())]
    assert sizes == ["1k", "2k", "4k"], "sizes follow the chosen model"
    dialog.deleteLater()


def test_recommended_models_are_offered_beside_the_field_with_their_notes(app):
    """Recommended ▾ beside an image model field: the default first, each
    with its note readable in the menu; choosing one fills the field."""
    from sealedlore.gui.model_picker import ModelField
    from sealedlore.models.config import RECOMMENDED_IMAGE_MODELS, recommended_model

    choices = RECOMMENDED_IMAGE_MODELS["map_isometric_model"]
    # Paintings of a place (the author, Oct 17 2026): FLUX.3 Klein, as for
    # the sprites, with Seedream and Nano Banana enhanced; each named as the
    # model, its edit version sent for it (the author, Oct 18 2026).
    assert choices[0].id == Config().map_isometric_model == Config().map_picture_model
    assert choices[0].id == "blackforestlabs/flux-3/klein/text-to-image"
    assert [c.id for c in choices if c.tag == "enhanced"] == [
        "bytedance/seedream-v5.0-pro",
        "nano-banana-pro",
    ]
    assert "$0.09" in recommended_model("map_isometric_model", choices[1].id).note
    assert "$0.14" in recommended_model("map_isometric_model", choices[2].id).note
    # Not Qwen (the author, Oct 17 2026: its paintings drew roofs as noise).
    assert recommended_model("map_isometric_model", "qwen-image") is None
    assert "klein" in Config().map_picture_model
    field = ModelField("", recommended=choices)
    actions = [a for a in field.recommended_button.menu().actions() if not a.isSeparator()]
    assert actions[0].text() == "FLUX.3 Klein (default)"
    assert not actions[1].isEnabled() and "layout" in actions[1].text()  # the note
    premium = next(a for a in actions if a.text().startswith("Seedream 5.0 Pro"))
    premium.trigger()
    assert field.text() == "bytedance/seedream-v5.0-pro"
    assert ModelField("").recommended_button is None


def test_the_text_model_fields_offer_what_was_measured_for_them(app):
    """Settings → Models: the measured models beside each role (the
    defaults unchanged, first)."""
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import (
        DEFAULT_STORY_MODEL,
        RECOMMENDED_MODELS,
        RECOMMENDED_TEXT_MODELS,
    )

    dialog = SettingsDialog(Config(), None)
    for role, field in (
        ("story", dialog.model),
        ("scene_model", dialog.scene_model),
        ("plot_model", dialog.plot_model),
        ("lore_model", dialog.lore_model),
        ("map_model", dialog.map_model),
        ("map_art_model", dialog.map_art_model),
    ):
        assert field.recommended_button is not None, role
        first = RECOMMENDED_TEXT_MODELS[role][0].id
        assert first == (DEFAULT_STORY_MODEL if role == "story" else RECOMMENDED_MODELS[role])
    premium = next(
        a for a in dialog.model.recommended_button.menu().actions() if "(premium)" in a.text()
    )
    premium.trigger()
    assert dialog.model.text() == "anthropic/claude-opus-5.5"
    assert dialog.summarization_model.recommended_button is None  # nothing measured
    dialog.reject()


def test_a_model_s_edit_version_is_no_row_of_its_own(app):
    """The author, Oct 18 2026: "the user doesn't have to select between Edit
    or Normal mode for any of them". One row a model, saying what it takes;
    a saved edit version opens on its model, and search finds it by either id."""
    k3e = "blackforestlabs/flux-3/klein/edit-image"
    models = [
        ImageModelInfo(
            id="blackforestlabs/flux-3/klein/text-to-image", name="FLUX.3 Klein", max_inputs=10
        ),
        ImageModelInfo(id=k3e, name="FLUX.3 Klein Edit", max_inputs=10),
        ImageModelInfo(id="gemini-flash-edit", name="Gemini Flash Edit", max_inputs=3),
        ImageModelInfo(id="hidream", name="HiDream", max_inputs=0),
    ]
    config = Config(image_models=[listing_entry(info) for info in models])
    dialog = ImagePickerDialog(ImageCatalog(config, None), current=k3e)
    rows = [dialog.table.topLevelItem(i) for i in range(dialog.table.topLevelItemCount())]
    assert sorted(r.text(0) for r in rows) == sorted(
        ["blackforestlabs/flux-3/klein/text-to-image", "gemini-flash-edit", "hidream"]
    )
    by_id = {r.text(0): r for r in rows}
    assert by_id["blackforestlabs/flux-3/klein/text-to-image"].text(REFERENCES) == "up to 10"
    assert by_id["gemini-flash-edit"].text(REFERENCES) == "needs one (up to 3)"
    assert dialog.selected_model() == "blackforestlabs/flux-3/klein/text-to-image", (
        "a saved edit version is its model's row"
    )
    assert k3e in dialog.detail.text(), "and says its edit version is sent for it"
    dialog.search.setText("edit-image")
    assert [r.text(0) for r in rows if not r.isHidden()] == [
        "blackforestlabs/flux-3/klein/text-to-image"
    ]
    dialog.reject()


def test_an_image_model_field_says_whether_it_takes_pictures(app):
    from sealedlore.engine.image_variants import picture_support
    from sealedlore.gui.model_picker import ModelField

    listing = [
        ImageModelInfo(id="blackforestlabs/flux-3/klein/text-to-image", name="K", max_inputs=10),
        ImageModelInfo(id="hidream", name="H", max_inputs=0),
    ]

    def support(model):
        return picture_support(model, listing)

    field = ModelField("blackforestlabs/flux-3/klein/text-to-image", pictures=support)
    assert field.picture_note.text() == "· takes pictures"
    field.setText("hidream")
    assert field.picture_note.text() == "· words only" and "can't" in field.picture_note.toolTip()
    field.setText("not/listed")
    assert field.picture_note.isHidden()
