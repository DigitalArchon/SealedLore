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
    assert seedream.text(REFERENCES) == "10" and rows["hidream"].text(REFERENCES) == "none"
    assert seedream.text(SIZES) == "1k, 2k, 4k"
    assert seedream.text(PRICE) == "0.030–0.090" and rows["hidream"].text(PRICE) == "0.010"
    assert seedream.text(AT_ONCE) == "4"
    assert "Keeps characters" in seedream.toolTip(0) and "4k $0.090" in seedream.toolTip(0)
    assert dialog.selected_model() == "hidream", "opens on the current model"
    assert "no reference pictures" in dialog.detail.text()

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
