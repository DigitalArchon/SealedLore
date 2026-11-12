"""A config that doesn't wholly validate loses only what doesn't, and the
file as it was is kept, never over an earlier copy."""

from __future__ import annotations

import json

from sealedlore.models.config import Config, ProviderConfig, SavedPrompt
from sealedlore.storage.paths import config_file
from sealedlore.storage.repository import load_config_or_recover, save_config


def a_newer_config(tmp_path) -> dict:
    """A config as a newer SealedLore might write it, with the author's
    work in it, read by this one."""
    config = Config(
        providers=[ProviderConfig(name="default", base_url="https://nano-gpt.com/api/v1")],
        recall_k=3,
    )
    config.chat_prompts = [SavedPrompt(name="Plain", text="Be brief.")]
    config.phone.shared_stories = ["story-1"]
    save_config(config, root=tmp_path)
    data = json.loads(config_file(tmp_path).read_text(encoding="utf-8"))
    data["reasoning_story"] = "xhigh"  # a level this version doesn't know
    data["recall_k"] = 9  # past this version's limit
    data["providers"][0]["api"] = "gemini"  # a protocol it doesn't speak
    config_file(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    return data


def test_only_the_values_that_dont_validate_go_back_to_their_defaults(tmp_path):
    a_newer_config(tmp_path)

    config, notice = load_config_or_recover(root=tmp_path)

    assert config.reasoning_story == Config().reasoning_story
    assert config.recall_k == Config().recall_k
    assert config.providers[0].api == "auto"
    assert config.providers[0].base_url == "https://nano-gpt.com/api/v1"
    assert [prompt.name for prompt in config.chat_prompts] == ["Plain"]
    assert config.phone.shared_stories == ["story-1"]
    assert notice is not None
    for name in ("reasoning_story", "recall_k", "providers[0].api"):
        assert name in notice


def test_the_file_as_it_was_is_kept_and_never_written_over(tmp_path):
    first = a_newer_config(tmp_path)
    load_config_or_recover(root=tmp_path)
    again, notice = load_config_or_recover(root=tmp_path)  # started again, nothing saved yet
    assert "config.json.broken" in notice
    assert json.loads((tmp_path / "config.json.broken").read_text(encoding="utf-8")) == first
    assert not (tmp_path / "config.json.broken-2").exists()  # the same bytes: one copy

    first["recall_k"] = 12  # another start, another file
    config_file(tmp_path).write_text(json.dumps(first), encoding="utf-8")
    _config, notice = load_config_or_recover(root=tmp_path)
    assert "config.json.broken-2" in notice

    def kept(name: str) -> int:
        return json.loads((tmp_path / name).read_text(encoding="utf-8"))["recall_k"]

    assert kept("config.json.broken") == 9
    assert kept("config.json.broken-2") == 12


def test_a_file_that_isnt_json_starts_over_and_keeps_every_earlier_copy(tmp_path):
    (tmp_path / "config.json.broken").write_text("an earlier copy", encoding="utf-8")
    config_file(tmp_path).write_text("{torn", encoding="utf-8")

    config, notice = load_config_or_recover(root=tmp_path)

    assert config == Config()
    assert "config.json.broken-2" in notice
    assert (tmp_path / "config.json.broken").read_text(encoding="utf-8") == "an earlier copy"
    assert (tmp_path / "config.json.broken-2").read_text(encoding="utf-8") == "{torn"
    assert not config_file(tmp_path).exists()


def test_a_config_that_validates_says_nothing_and_leaves_no_copy(tmp_path):
    save_config(Config(recall_k=3), root=tmp_path)
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.recall_k == 3 and notice is None
    assert not list(tmp_path.glob("config.json.broken*"))
