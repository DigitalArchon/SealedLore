"""Portraits of the characters (engine/portraits.py, engine/session_portraits.py):
the writer's request and reply, who may have one, the request as approved,
and a drawn portrait taken onto its card once. No network: the mock."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.images import run_image_request
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.engine.portraits import (
    build_portrait_messages,
    fallback_prompt,
    parse_portrait_prompts,
    portrait_size,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config
from sealedlore.providers.images import ImageModelInfo, ImageReply
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import read_api_log, save_story_bundle
from sealedlore.storage.scenario import bundle_from_scenario

DOOMSVILLE = Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville.md"


def _png() -> bytes:
    """A 2x2 grey PNG, valid to the byte."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = b"".join(b"\0" + b"\x80\x80\x80" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = _png()


def session_on(tmp_path: Path, replies: list[str]) -> tuple[StorySession, MockChatProvider]:
    """Doomsville, with its pictures taken off so everyone needs a portrait."""
    bundle = bundle_from_scenario(parse_plot_markdown(DOOMSVILLE.read_text()).scenario)
    for character in [*bundle.cast, *bundle.supporting.characters]:
        character.reference_images.clear()
    save_story_bundle(bundle, root=tmp_path)
    provider = MockChatProvider(replies)
    session = StorySession(
        bundle,
        Config(),
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    return session, provider


def reply(*pairs: tuple[str, str]) -> str:
    return json.dumps({"portraits": [{"name": n, "prompt": p} for n, p in pairs]})


def test_only_characters_the_models_may_see_are_offered_or_written_for(tmp_path: Path):
    """Doomsville's Ben, Evan, Marcus and Elena are hidden until the plot
    brings them in: a portrait of one would be the first a model heard of
    them."""
    session, provider = session_on(tmp_path, [reply(("John", "A man."), ("Jane", "A woman."))])
    names = {c.name for c in session.without_portraits()}
    assert names == {"John", "Jane"}
    ben = next(c for c in session.cast if c.name == "Ben")
    prompts, problem = session.write_portrait_prompts([*session.cast])
    assert set(prompts) == {c.id for c in session.without_portraits()} and problem is None
    sent = json.dumps(provider.payloads)
    assert "hermit" not in sent and ben.id not in prompts


def test_the_writer_is_asked_for_what_can_be_seen_and_never_a_name():
    john = Character(name="John", summary="A 30-year-old ex-soldier.")
    messages = build_portrait_messages(
        [john], world_bible="Four years after the dead rose.", title="T", image_style=""
    )
    system, user = (m.parts[0].text for m in messages)
    assert DEFAULT_TEXTS["portraits.style"] in system  # the story set no style
    assert "something the camera can see" in system and "no names" in system
    assert DEFAULT_TEXTS["portraits.no_writing"] in system
    assert "## John" in user and "Four years after the dead rose." in user
    assert "WHERE THE STORY BEGINS" not in user  # none given
    # The story's own style when it has one.
    styled = build_portrait_messages([john], world_bible="", title="T", image_style="Oil paint.")
    assert "this style: Oil paint." in styled[0].parts[0].text


def test_the_writer_is_told_where_the_story_begins(tmp_path: Path):
    """Doomsville's winter is in its opening, not its world text."""
    session, provider = session_on(tmp_path, [reply(("John", "A man."))])
    session.write_portrait_prompts(session.without_portraits()[:1])
    sent = json.dumps(provider.payloads[-1])
    assert "WHERE THE STORY BEGINS" in sent and "deep winter" in sent


def test_a_reply_is_read_by_name_and_whoever_it_left_out_gets_a_prompt_from_the_card(
    tmp_path: Path,
):
    session, _provider = session_on(tmp_path, [reply(("john", "  A tired   man. "), ("Zed", "?"))])
    john, jane = session.without_portraits()
    prompts, problem = session.write_portrait_prompts([john, jane])
    assert prompts[john.id] == "A tired man."  # by name, any case, spaces closed up
    assert prompts[jane.id] == fallback_prompt(jane, image_style="")
    assert "left out Jane" in problem
    assert prompts[jane.id].endswith(DEFAULT_TEXTS["portraits.no_writing"])
    # The fallback is the card's looks: the world bible's words aren't a picture.
    assert session.story.world_bible.split(".")[0] not in prompts[jane.id]


def test_a_failed_writer_still_leaves_prompts_to_edit(tmp_path: Path):
    session, provider = session_on(tmp_path, ["not json at all"])
    people = session.without_portraits()
    prompts, problem = session.write_portrait_prompts(people)
    assert set(prompts) == {c.id for c in people} and "couldn't be read" in problem
    kinds = [entry["kind"] for entry in read_api_log(session.story.id, root=tmp_path)]
    assert "portrait_prompt_request" in kinds and "portrait_prompt_response" in kinds
    with pytest.raises(ValueError):
        parse_portrait_prompts("nothing", people)


def test_portraits_wait_for_a_private_scene_to_end(tmp_path: Path):
    session, _provider = session_on(tmp_path, ["x"])
    session.enter_private(keep="memory", provider=MockChatProvider(["y"]), model="local/m")
    with pytest.raises(ValueError, match="private scene"):
        session.write_portrait_prompts(session.without_portraits())


def test_the_portrait_size_is_the_largest_square_up_to_1024():
    info = ImageModelInfo(id="m", name="M", resolutions=("512x512", "1024x1024", "1920x1088"))
    assert portrait_size(info) == "1024x1024"
    assert portrait_size(ImageModelInfo(id="m", name="M", resolutions=("1024*1024",))) == (
        "1024*1024"
    )
    assert portrait_size(ImageModelInfo(id="m", name="M", resolutions=("1k", "2k"))) == "1k"
    assert portrait_size(None) == "1024x1024"


class _Images:
    api = "nanogpt"

    def __init__(self):
        self.sent: list[dict] = []

    def generate(self, payload):
        self.sent.append(payload)
        return ImageReply(images=[PNG], raw={}, cost=0.01)


def test_a_drawn_portrait_is_sent_as_approved_and_becomes_the_first_picture_once(tmp_path: Path):
    session, _provider = session_on(tmp_path, ["x"])
    john = session.without_portraits()[0]
    info = ImageModelInfo(
        id="flux-2-klein-9b", name="F", resolutions=("1024*1024",), per_image={"*": 0.01}
    )
    approved = "  A tired man, frost on his collar.\n"
    request = session.portrait_request(john, approved, model=info.id, info=info)
    assert request.prompt == approved and request.size == "1024*1024"
    assert request.portrait_of == john.id and request.references == ()
    client = _Images()
    records = run_image_request(client, request, store=session.pictures)
    assert client.sent[0]["prompt"] == approved  # not even trimmed
    session.pictures.add_images(records)
    assert session.take_portraits() == ["John"]
    first = john.reference_images[0]
    assert first.caption == "portrait" and session.pictures.read(first.file) == PNG
    assert first.file != records[0].file  # its own copy: Media's Delete leaves it
    # Taken once: removed from the card, it stays removed.
    john.reference_images.clear()
    assert session.take_portraits() == [] and not john.reference_images
