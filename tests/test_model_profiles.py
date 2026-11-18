"""Model profiles (engine/model_profiles.py, gui/profile_row.py): a named set
of the text models, one for each job, put back with one choice in Settings →
Models or a story's Setup → Models. Default, Recommended and Private come
with the app; the author's own are kept in config.json. Offscreen, no
network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sealedlore.engine.model_profiles import (
    BUILT_IN,
    DEFAULT,
    NO_NANOGPT,
    NO_PRIVATE,
    PRIVATE,
    PROFILE_ROLES,
    RECOMMENDED,
    all_profiles,
    built_in_profiles,
    filled,
    from_saved,
    matching,
    name_problem,
    renamed,
    to_save,
)
from sealedlore.models.config import (
    DEFAULT_STORY_MODEL,
    RECOMMENDED_MODELS,
    Config,
    ModelProfile,
    ProviderConfig,
)
from sealedlore.models.route import ModelRoute
from sealedlore.providers.privacy import privacy_of

NANO = "https://nano-gpt.com/api/v1"
ANTHROPIC = "https://api.anthropic.com"
TINFOIL = "https://inference.tinfoil.sh/v1"


def nano(name: str = "NanoGPT") -> ProviderConfig:
    return ProviderConfig(name=name, base_url=NANO, api_key="k", model=DEFAULT_STORY_MODEL)


def by_name(profiles):
    return {p.name: p for p in profiles}


# --- the three that come with the app -------------------------------------------


def test_default_is_what_a_new_setup_starts_with() -> None:
    profile = by_name(built_in_profiles([nano()], "NanoGPT"))[DEFAULT]
    assert profile.models["story"] == DEFAULT_STORY_MODEL
    assert profile.models["scene"] == RECOMMENDED_MODELS["scene_model"]
    assert profile.models["plot"] == RECOMMENDED_MODELS["plot_model"]
    assert profile.models["lore"] == RECOMMENDED_MODELS["lore_model"]
    assert all(not profile.models[r] for r in ("summarisation", "authoring", "image_prompt"))
    assert not profile.problem and profile.built_in


def test_recommended_takes_each_jobs_premium_choice() -> None:
    profile = by_name(built_in_profiles([nano()], "NanoGPT"))[RECOMMENDED]
    assert profile.models["story"] == "anthropic/claude-opus-5.5"
    assert profile.models["scene"] == "moonshotai/kimi-k3"
    assert profile.models["lore"] == "mistralai/mistral-medium-3.5"
    # No premium choice measured for the plot: its recommended one.
    assert profile.models["plot"] == RECOMMENDED_MODELS["plot_model"]
    # Claude through Anthropic's own API where there is one.
    with_anthropic = [nano(), ProviderConfig(name="Anthropic", base_url=ANTHROPIC, api_key="a")]
    claude = by_name(built_in_profiles(with_anthropic, "NanoGPT"))[RECOMMENDED]
    assert claude.models["story"] == "Anthropic::claude-opus-5-5"


def test_private_is_end_to_end_encrypted_for_every_job() -> None:
    config = Config(providers=[nano()], active_provider_name="NanoGPT")
    profile = by_name(built_in_profiles(config.providers, "NanoGPT"))[PRIVATE]
    assert profile.models["story"] == "private/glm-5-3"
    for role, (model, _route) in filled(profile).items():
        assert privacy_of(model, config).level == "highest", role
    # On Tinfoil's own endpoint, its names.
    tinfoil = ProviderConfig(name="Tinfoil", base_url=TINFOIL, api_key="t")
    config = Config(providers=[tinfoil], active_provider_name="Tinfoil")
    profile = by_name(built_in_profiles(config.providers, "Tinfoil"))[PRIVATE]
    assert profile.models["story"] == "glm-5-3" and not profile.problem
    for role, (model, _route) in filled(profile).items():
        assert privacy_of(model, config).level == "highest", role
    # Both: the default endpoint's service.
    both = [nano(), tinfoil]
    assert by_name(built_in_profiles(both, "Tinfoil"))[PRIVATE].models["story"] == "glm-5-3"
    assert by_name(built_in_profiles(both, "NanoGPT"))[PRIVATE].models["story"] == (
        "private/glm-5-3"
    )


def test_a_profile_says_when_the_endpoints_cant_serve_it() -> None:
    other = ProviderConfig(name="Local", base_url="http://127.0.0.1:8080/v1")
    profiles = by_name(built_in_profiles([other], "Local"))
    assert profiles[DEFAULT].problem == NO_NANOGPT
    assert profiles[RECOMMENDED].problem == NO_NANOGPT
    assert profiles[PRIVATE].problem == NO_PRIVATE


# --- the author's own -------------------------------------------------------------


def test_a_saved_profile_names_its_endpoints_and_comes_back_as_the_fields_held_it() -> None:
    route = ModelRoute(priority="latency")
    models = {"story": "z-ai/glm-5.3", "scene": "Anthropic::claude-haiku-4-5", "lore": ""}
    kept = to_save(" Quick ", models, {"story": route, "scene": None}, "NanoGPT")
    assert kept.name == "Quick"
    assert kept.models == {
        "story": "NanoGPT::z-ai/glm-5.3",
        "scene": "Anthropic::claude-haiku-4-5",
    }
    assert kept.routes == {"story": route}
    back = from_saved(kept, "NanoGPT")
    assert back.models["story"] == "z-ai/glm-5.3" and back.models["lore"] == ""
    # Another endpoint made the default: the profile holds the same models.
    assert from_saved(kept, "Anthropic").models["story"] == "NanoGPT::z-ai/glm-5.3"
    assert from_saved(kept, "Anthropic").models["scene"] == "claude-haiku-4-5"
    assert matching([back], {**models, "lore": ""}, {"story": route}) == back
    assert matching([back], models, {}) is None  # the route differs


def test_names_that_come_with_the_app_are_kept_for_them() -> None:
    assert name_problem("  ", []) == "Give the profile a name."
    for name in BUILT_IN:
        assert name_problem(name.lower(), []) is not None
    assert name_problem("Horror on Opus", []) is None


def test_filled_takes_each_blank_from_the_job_it_falls_back_to() -> None:
    profile = from_saved(
        ModelProfile(
            name="Mix",
            models={"story": "A::story-model", "scene": "A::scene-model"},
            routes={"scene": ModelRoute(priority="speed")},
        ),
        "A",
    )
    full = filled(profile)
    assert set(full) == set(PROFILE_ROLES)
    assert full["summarisation"][0] == "story-model"
    assert full["plot"] == ("scene-model", ModelRoute(priority="speed"))
    assert full["record"][0] == "scene-model" and full["image_prompt"][0] == "story-model"


def test_a_renamed_endpoint_takes_its_profiles_along() -> None:
    kept = [ModelProfile(name="Mix", models={"story": "Old::m", "scene": "Other::n"})]
    [moved] = renamed(kept, "Old", "New")
    assert moved.models == {"story": "New::m", "scene": "Other::n"}


def test_the_author_s_follow_the_three(tmp_path: Path) -> None:
    saved = [ModelProfile(name="Mine", models={"story": "NanoGPT::x"})]
    names = [p.name for p in all_profiles(saved, [nano()], "NanoGPT")]
    assert names == [DEFAULT, RECOMMENDED, PRIVATE, "Mine"]


def test_profiles_are_kept_in_config_json(tmp_path: Path) -> None:
    from sealedlore.storage.repository import load_config, save_config

    config = Config(model_profiles=[ModelProfile(name="Mine", models={"story": "NanoGPT::x"})])
    save_config(config, root=tmp_path)
    assert load_config(tmp_path).model_profiles == config.model_profiles


# --- the window -----------------------------------------------------------------------


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _config(**changes) -> Config:
    return Config(
        providers=[nano()],
        active_provider_name="NanoGPT",
        scene_model=RECOMMENDED_MODELS["scene_model"],
        plot_model=RECOMMENDED_MODELS["plot_model"],
        **changes,
    )


def test_settings_shows_which_profile_the_fields_hold_and_fills_one(app) -> None:
    from sealedlore.gui.profile_row import OWN_MIX
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config()
    dialog = SettingsDialog(config, None)
    row = dialog.profiles
    assert row.combo.currentText() == DEFAULT
    dialog.authoring_model.setText("z-ai/glm-5.3")
    assert row.combo.currentText() == OWN_MIX
    row._activated(row.combo.findData(PRIVATE))
    assert dialog.model.text() == "private/glm-5-3" and dialog.authoring_model.text() == ""
    assert row.combo.currentText() == PRIVATE
    assert "Still sent unencrypted" in dialog.recommended_note.text()
    assert "pictures and videos" in dialog.recommended_note.text()
    # Nothing is kept until Save.
    assert config.active_provider().model == DEFAULT_STORY_MODEL
    dialog._save()
    assert config.active_provider().model == "private/glm-5-3"
    assert config.scene_model == "private/glm-5-3"
    dialog.deleteLater()


def test_save_as_keeps_the_fields_under_a_name_until_settings_is_saved(app, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui import profile_row
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config()
    dialog = SettingsDialog(config, None)
    dialog.model.setText("anthropic/claude-opus-5.5")
    monkeypatch.setattr(profile_row, "ask_profile_name", lambda _parent: "Opus nights")
    dialog.profiles.save_as.click()
    assert dialog.profiles.combo.currentText() == "Opus nights"
    assert dialog.profiles.delete.isEnabled()
    assert config.model_profiles == []
    # A name the app's own have is refused, said.
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **_k: warned.append(a[2]))
    assert not dialog.profiles.keep("Private")
    assert warned and "comes with SealedLore" in warned[0]
    # The same name again replaces it, asked first.
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.No)
    dialog.model.setText("z-ai/glm-5.3")
    assert not dialog.profiles.keep("opus nights")
    dialog._save()
    [kept] = config.model_profiles
    assert kept.name == "Opus nights"
    assert kept.models["story"] == "NanoGPT::anthropic/claude-opus-5.5"
    # Opened again, it is offered, and putting it back fills the field.
    again = SettingsDialog(config, None)
    row = again.profiles
    row._activated(row.combo.findData("Opus nights"))
    assert again.model.text() == "anthropic/claude-opus-5.5"
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Yes)
    row.delete.click()
    assert row.combo.findData("Opus nights") == -1
    again._save()
    assert config.model_profiles == []
    dialog.deleteLater()
    again.deleteLater()


def test_cancel_after_saving_a_profile_asks(app, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(_config(), None)
    dialog.show()
    app.processEvents()
    dialog._opened = dialog._state()
    dialog.profiles.keep("Mine")
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_a, **_k: asked.append(1) or QMessageBox.Cancel
    )
    dialog.reject()
    assert asked and dialog.isVisible()
    dialog.deleteLater()


def test_a_renamed_endpoint_takes_the_profiles_with_it(app) -> None:
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = _config(model_profiles=[ModelProfile(name="Mine", models={"story": "NanoGPT::x"})])
    dialog = SettingsDialog(config, None)
    dialog.provider_name.setText("Nano")
    dialog._carry_name()
    dialog._save()
    assert config.model_profiles[0].models["story"] == "Nano::x"
    dialog.deleteLater()


def test_setup_fills_every_job_of_a_profile_into_the_story(app) -> None:
    from sealedlore.gui.setup_dialog import ModelChoices, SetupDialog
    from sealedlore.models.story import Story

    story = Story(title="Doomsville")
    config = _config()
    dialog = SetupDialog(story, [], models=ModelChoices(config))
    actions = {a.text(): a for a in dialog.use_profile.menu().actions()}
    assert list(actions) == [DEFAULT, RECOMMENDED, PRIVATE]
    actions[PRIVATE].trigger()
    # Every job, a blank one too: blank here would follow Settings.
    for role, field in dialog.model_fields.items():
        assert field.text().startswith("private/"), role
    dialog._save_models(story)
    assert story.defaults.summarization_model == "private/glm-5-3"
    assert story.defaults.lore_model == "private/deepseek-v4-1-flash"
    dialog.deleteLater()
