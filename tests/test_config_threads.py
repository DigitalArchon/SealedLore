"""The config is shared by every thread: what each puts in it reaches
config.json, whichever thread saves last, and no save breaks on another
thread's change."""

from __future__ import annotations

import json
import random
import sys
import threading
import time
from pathlib import Path

from sealedlore.engine.session import StorySession, TurnRequest
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.config import Config, ModelPrice, ProviderConfig
from sealedlore.models.node import Usage
from sealedlore.storage import repository
from sealedlore.storage.paths import config_file
from sealedlore.storage.repository import (
    CONFIG_LOCK,
    StoryBundle,
    save_config,
    save_story_bundle,
)
from tests.test_reasoning import KIMI_ENTRY, ListedMock

ROUNDS = 120


def written(root: Path) -> dict:
    return json.loads(config_file(root).read_text(encoding="utf-8"))


def test_threads_learning_and_saving_at_once_lose_nothing_and_never_break(tmp_path, monkeypatch):
    """Three threads as the engine's (each puts a value in under the lock,
    then saves) and one as the window's (changes a dict without it, and
    saves), all at once. Once a save is done, what it saved is in the file,
    whoever saves after, and no save breaks on another thread's change."""
    real_write = repository.atomic_write_json
    failures: list[BaseException | str] = []
    landed = threading.Lock()
    most = [0]

    def slow_write(path, data, **kwargs) -> None:
        time.sleep(random.random() / 500)  # a disk that takes its time
        with landed:
            # Values are only ever put in: a write holding fewer than one
            # before it is an older reading landing last.
            count = len(data["token_correction_factors"]) + len(data["model_prices"])
            if count < most[0]:
                failures.append(f"a reading of {count} landed after one of {most[0]}")
            most[0] = max(most[0], count)
            real_write(path, data, **kwargs)

    monkeypatch.setattr(repository, "atomic_write_json", slow_write)
    config = Config()
    save_config(config, root=tmp_path)
    start = threading.Barrier(4)

    def engine(number: int) -> None:
        try:
            start.wait()
            for round_ in range(ROUNDS):
                key = f"model-{number}-{round_}"
                with CONFIG_LOCK:
                    config.token_correction_factors[key] = 1.0 + round_
                save_config(config, root=tmp_path)
                if key not in written(tmp_path)["token_correction_factors"]:
                    failures.append(f"{key} saved, then an older reading landed over it")
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    def window() -> None:
        try:
            start.wait()
            for round_ in range(ROUNDS):
                key = f"listed-{round_}"
                config.model_prices[key] = ModelPrice(prompt=1.0, completion=2.0)
                save_config(config, root=tmp_path)
                if key not in written(tmp_path)["model_prices"]:
                    failures.append(f"{key} saved, then an older reading landed over it")
        except BaseException as exc:  # noqa: BLE001 - reported below
            failures.append(exc)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # threads change places as often as they can
    try:
        threads = [threading.Thread(target=engine, args=(n,)) for n in range(3)]
        threads.append(threading.Thread(target=window))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
    finally:
        sys.setswitchinterval(interval)

    assert failures == []
    assert len(written(tmp_path)["token_correction_factors"]) == 3 * ROUNDS
    assert len(written(tmp_path)["model_prices"]) == ROUNDS


class Watched(dict):
    """A dict that notes whether the config's lock was held for each value
    put in it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.unlocked: list[str] = []

    def __setitem__(self, key, value) -> None:
        if not CONFIG_LOCK._is_owned():
            self.unlocked.append(key)
        super().__setitem__(key, value)


def test_what_the_engine_learns_goes_into_the_config_under_its_lock(tmp_path, story, cast):
    priced = {**KIMI_ENTRY, "pricing": {"prompt": 3, "completion": 15}}
    provider = ListedMock(
        ["Reply."],
        reasoning="Let me think about this at length. " * 80,
        usage=Usage(prompt_tokens=900, completion_tokens=5),
        listing={"moonshotai/kimi-k3": priced},
    )
    story.defaults.main_model = "moonshotai/kimi-k3"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="n",
    )
    config.token_correction_factors = Watched()
    config.model_prices = Watched()
    config.model_reasoning = Watched()
    session = StorySession(
        StoryBundle(story=story, cast=cast),
        config,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter, factors=config.token_correction_factors),
    )

    list(
        session.send(
            TurnRequest(
                speaker_id="char-serrik", user_text="Go.", controlled_character_id="char-serrik"
            )
        )
    )

    learned = [config.token_correction_factors, config.model_prices, config.model_reasoning]
    assert all(learned), "a factor, a price and the model's reasoning were all learned"
    assert [dict_.unlocked for dict_ in learned] == [[], [], []]
    assert Path(config_file(tmp_path)).exists()
