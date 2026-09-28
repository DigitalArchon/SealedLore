"""Pictures: the image client, the prompt writer, and where pictures are kept.

Never the network: the client runs on an httpx MockTransport, the writer on
the scripted mock provider.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.image_prompt import (
    build_image_question,
    mentioned_numbers,
    parse_image_prompt,
    ref_choices,
)
from sealedlore.engine.images import ImageRequest, MissingReference, run_image_request
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.image import GeneratedImage, ImageRef, RefUse
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import (
    ImageClient,
    build_image_payload,
    loggable_payload,
    parse_image_models,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.archive import import_archive, read_archive, write_archive
from sealedlore.storage.images import (
    add_generated_images,
    all_image_files,
    is_safe_relative,
    load_generated_images,
    read_story_file,
    reference_files,
    remove_generated_image,
    write_story_file,
)
from sealedlore.storage.repository import (
    StoryBundle,
    copy_story,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.storage.scenario import (
    bundle_from_scenario,
    fresh_playthrough,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from tests.conftest import make_exchange

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

LISTING = {
    "data": [
        {
            "id": "bytedance/seedream-v5.0-pro",
            "name": "Seedream 5.0 Pro",
            "pricing": {"per_image": {"16:9": 0.09, "1k": 0.045, "2k": 0.09}},
            "supported_parameters": {
                "resolutions": ["1:1", "16:9", "1k", "2k"],
                "max_images": 4,
                "max_output_images": 4,
                "max_input_images": 10,
            },
        },
        {
            "id": "hidream",
            "name": "Hidream",
            "pricing": {"per_image": {"1024x1024": 0.0153}},
            "supported_parameters": {"resolutions": ["1024x1024"], "max_images": 20},
        },
    ]
}


def image_client(handler) -> ImageClient:
    return ImageClient(
        "https://nano-gpt.com/api/v1",
        "key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def reply(images: int = 1, cost: float | None = 0.09) -> httpx.Response:
    body: dict = {"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}] * images}
    if cost is not None:
        body["cost"] = cost
    return httpx.Response(200, json=body)


# --- the listing and the client ---------------------------------------------------


def test_the_listing_gives_sizes_limits_and_prices():
    seedream, hidream = parse_image_models(LISTING)
    assert seedream.resolutions == ("1:1", "16:9", "1k", "2k")
    assert seedream.max_inputs == 10 and seedream.accepts_images
    assert seedream.max_outputs == 4
    assert seedream.price("1k") == 0.045
    assert not hidream.accepts_images
    assert hidream.max_outputs == 20


def test_references_go_as_data_urls_only_when_there_are_some():
    assert "imageDataUrls" not in build_image_payload("m", "p", "1k", 1, [])
    payload = build_image_payload("m", "p", "1k", 1, ["data:image/png;base64,AA"])
    assert payload["imageDataUrls"] == ["data:image/png;base64,AA"]
    assert payload["response_format"] == "b64_json"
    # The log keeps the prompt and names the pictures, not megabytes of base64.
    logged = loggable_payload(payload, ["Jane: images/refs/a.png"])
    assert logged["imageDataUrls"] == ["<Jane: images/refs/a.png>"]
    assert logged["prompt"] == "p"


def test_the_client_posts_to_images_generations_and_reads_the_cost():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(cost=0.045)

    result = image_client(handler).generate(build_image_payload("m", "p", "1k", 1, []))
    assert str(seen[0].url) == "https://nano-gpt.com/api/v1/images/generations"
    assert seen[0].headers["authorization"] == "Bearer key"
    assert result.images == [PNG]
    assert result.cost == 0.045


def test_a_url_reply_is_fetched_at_once():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/x.png"}]})

    assert image_client(handler).generate({"model": "m"}).images == [PNG]


def test_a_rate_limit_is_waited_out(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return reply()

    client = image_client(handler)
    monkeypatch.setattr(client._wake, "wait", lambda _delay: False)
    assert client.generate({"model": "m"}).images == [PNG]
    assert len(calls) == 2


def test_an_error_is_a_provider_error_and_no_picture_is_one_too():
    with pytest.raises(ProviderError):
        image_client(lambda r: httpx.Response(400, text="bad size")).generate({"model": "m"})
    with pytest.raises(ProviderError):
        image_client(lambda r: httpx.Response(200, json={"data": []})).generate({"model": "m"})


# --- sending an approved request ---------------------------------------------------


@pytest.fixture
def saved(tmp_path: Path, story, cast) -> StoryBundle:
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    write_story_file(story.id, "images/refs/serrik.png", PNG, tmp_path)
    return bundle


def serrik_ref() -> RefUse:
    return RefUse(
        owner_kind="character",
        owner_id="char-serrik",
        owner_name="Serrik Vaun",
        ref_id="ref-1",
        file="images/refs/serrik.png",
    )


def test_the_prompt_is_sent_exactly_as_approved(tmp_path: Path, saved: StoryBundle):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return reply(images=2, cost=0.18)

    # Leading and trailing whitespace and odd spacing are the author's to keep.
    approved = "  The man in image 1 stands in the undercroft.\n\n"
    request = ImageRequest(
        story_id=saved.story.id,
        model="bytedance/seedream-v5.0-pro",
        prompt=approved,
        size="16:9",
        n=2,
        references=(serrik_ref(),),
        anchor_node_id="a1",
        direction="Serrik, brooding",
    )
    records = run_image_request(image_client(handler), request, tmp_path)

    assert sent[0]["prompt"] == approved
    assert sent[0]["imageDataUrls"][0].startswith("data:image/png;base64,")
    assert sent[0]["n"] == 2 and sent[0]["size"] == "16:9"
    assert len(records) == 2
    assert all(record.prompt == approved for record in records)
    assert records[0].cost == pytest.approx(0.09) and records[0].cost_reported
    assert read_story_file(saved.story.id, records[0].file, tmp_path) == PNG

    log = read_api_log(saved.story.id, root=tmp_path)
    request_entry, response_entry = log[-2], log[-1]
    assert request_entry["kind"] == "image_request"
    assert request_entry["payload"]["prompt"] == approved
    assert request_entry["payload"]["imageDataUrls"] == ["<Serrik Vaun: images/refs/serrik.png>"]
    assert response_entry["kind"] == "image_response"
    report = usage_report(log)
    images = next(line for line in report.lines if line.label == "Images")
    assert images.cost == pytest.approx(0.18) and images.estimated == 0


def test_an_unreported_cost_is_estimated_from_the_listing(tmp_path: Path, saved: StoryBundle):
    request = ImageRequest(
        story_id=saved.story.id,
        model="hidream",
        prompt="p",
        size="1024x1024",
        n=1,
        references=(),
        anchor_node_id="a1",
        listed_price=0.0153,
    )
    records = run_image_request(image_client(lambda r: reply(cost=None)), request, tmp_path)
    assert records[0].cost == pytest.approx(0.0153) and not records[0].cost_reported
    images = next(
        line
        for line in usage_report(read_api_log(saved.story.id, root=tmp_path)).lines
        if line.label == "Images"
    )
    assert images.estimated == 1 and images.cost == pytest.approx(0.0153)


def test_a_missing_reference_sends_nothing(tmp_path: Path, saved: StoryBundle):
    calls = []
    use = serrik_ref().model_copy(update={"file": "images/refs/gone.png"})
    request = ImageRequest(
        story_id=saved.story.id,
        model="m",
        prompt="p",
        size="1k",
        n=1,
        references=(use,),
        anchor_node_id=None,
    )
    with pytest.raises(MissingReference):
        run_image_request(image_client(lambda r: calls.append(r) or reply()), request, tmp_path)
    assert calls == []


def test_a_story_deleted_meanwhile_is_not_brought_back(tmp_path: Path, saved: StoryBundle):
    request = ImageRequest(
        story_id=saved.story.id,
        model="m",
        prompt="p",
        size="1k",
        n=1,
        references=(),
        anchor_node_id=None,
    )
    directory = tmp_path / "stories" / saved.story.id

    def handler(_request):
        (directory / "story.json").unlink()
        return reply()

    with pytest.raises(ProviderError):
        run_image_request(image_client(handler), request, tmp_path)
    assert not list((directory / "images").glob("*.png"))


# --- the prompt writer --------------------------------------------------------------


def test_the_question_numbers_nothing_itself_and_the_reply_chooses_in_order():
    choices = ref_choices(
        [
            (serrik_ref(), True),
            (serrik_ref().model_copy(update={"ref_id": "ref-2", "owner_name": "The Keep"}), False),
        ]
    )
    question = build_image_question(
        direction="Serrik alone",
        held_name="Serrik Vaun",
        style="oil painting",
        choices=choices,
        max_refs=10,
    )
    assert "R1 — Serrik Vaun (character, in the scene now)" in question
    assert "# THE AUTHOR ASKS FOR\nSerrik alone" in question
    assert "oil painting" in question

    draft = parse_image_prompt(
        '{"references": ["R2", "R1", "R9"], "prompt": "The keep in image 1, the man in image 2."}',
        choices,
        max_refs=10,
    )
    assert [use.ref_id for use in draft.references] == ["ref-2", "ref-1"]
    assert draft.dropped == ["R9"]
    capped = parse_image_prompt('{"references": ["R1", "R2"], "prompt": "x"}', choices, max_refs=1)
    assert [use.ref_id for use in capped.references] == ["ref-1"]
    assert mentioned_numbers("the man in Image 2 and image 10") == {2, 10}


def make_session(tmp_path: Path, bundle: StoryBundle, replies: list[str]) -> StorySession:
    config = Config(
        providers=[
            ProviderConfig(
                name="nano",
                base_url="https://nano-gpt.com/api/v1",
                model="anthropic/claude-sonnet-4.6",
            )
        ],
        active_provider_name="nano",
        scene_reads="manual",
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )


def test_the_writer_rides_the_story_prompt_and_offers_only_visible_pictures(
    tmp_path: Path, saved: StoryBundle
):
    saved.cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    saved.cast[1].reference_images.append(ImageRef(id="ref-2", file="images/refs/maela.png"))
    saved.lore.append(
        LoreEntry(
            title="The Keep",
            content="Old.",
            reference_images=[ImageRef(id="ref-3", file="images/refs/keep.png")],
        )
    )
    session = make_session(
        tmp_path, saved, ['{"references": ["R1"], "prompt": "A duellist, image 1."}']
    )
    # Maela is hidden by a plot: her picture must reach no model.
    session.hidden_ids = lambda *args, **kwargs: {"char-maela"}
    offered = session.image_ref_choices()
    assert [choice.use.ref_id for choice in offered] == ["ref-1", "ref-3"]
    assert offered[0].present

    draft = session.write_image_prompt("Serrik, brooding")
    assert draft.prompt == "A duellist, image 1."
    assert [use.owner_name for use in draft.references] == ["Serrik Vaun"]
    tail = session.provider.requests[-1].messages[-1].text
    assert "# IMAGE PROMPT" in tail
    assert "Maela" not in tail.split("# REFERENCE PICTURES")[1]
    kinds = [entry["kind"] for entry in read_api_log(saved.story.id, root=tmp_path)]
    assert kinds[-2:] == ["image_prompt_request", "image_prompt_response"]
    assert session.unreported_usage


def test_a_picture_added_from_disk_is_offered_in_a_story_too(tmp_path: Path, saved: StoryBundle):
    """It was a chat's alone. A picture that belongs to no card (a place, an
    object) is offered after the cards', and the writer is told what it is:
    it used to be called a character."""
    saved.cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    saved.cast[1].reference_images.append(ImageRef(id="ref-2", file="images/refs/maela.png"))
    saved.story.reference_images.append(
        ImageRef(id="ref-9", file="images/refs/gate.png", caption="the north gate")
    )
    session = make_session(tmp_path, saved, ['{"references": ["R2"], "prompt": "The gate."}'])
    session.hidden_ids = lambda *args, **kwargs: {"char-maela"}
    offered = session.image_ref_choices()
    assert [choice.use.ref_id for choice in offered] == ["ref-1", "ref-9"]
    assert offered[-1].use.owner_kind == "story" and not offered[-1].present

    draft = session.write_image_prompt("the gate at dusk")
    assert [use.ref_id for use in draft.references] == ["ref-9"]
    catalogue = session.provider.requests[-1].messages[-1].text.split("# REFERENCE PICTURES")[1]
    assert "R1 — Serrik Vaun (character, in the scene now)" in catalogue
    assert "R2 — the north gate (added picture): the north gate" in catalogue
    assert "Maela" not in catalogue


def test_rewriting_around_the_authors_pictures_keeps_their_order(
    tmp_path: Path, saved: StoryBundle
):
    saved.cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    session = make_session(tmp_path, saved, ['{"references": [], "prompt": "Redone."}'])
    fixed = [serrik_ref()]
    draft = session.write_image_prompt("", fixed=fixed)
    assert draft.prompt == "Redone."
    assert draft.references == fixed


# --- where pictures are kept ----------------------------------------------------------


def test_only_paths_inside_the_images_folder_are_accepted():
    assert is_safe_relative("images/refs/a.png")
    assert is_safe_relative("images.json")
    for bad in ("../x.png", "/etc/passwd", "images/../story.json", "story.json", "images"):
        assert not is_safe_relative(bad)


def generated(anchor: str | None = "a1", file: str = "images/g1.png") -> GeneratedImage:
    return GeneratedImage(anchor_node_id=anchor, file=file, prompt="p", model="m", size="1k")


def test_images_json_is_kept_apart_from_the_bundle_save(tmp_path: Path, saved: StoryBundle):
    record = generated()
    write_story_file(saved.story.id, record.file, PNG, tmp_path)
    add_generated_images(saved.story.id, [record], tmp_path)
    # A session save rewriting the bundle from memory mustn't drop it.
    save_story_bundle(load_story_bundle(saved.story.id, root=tmp_path), root=tmp_path)
    assert [image.id for image in load_generated_images(saved.story.id, tmp_path)] == [record.id]
    remove_generated_image(saved.story.id, record.id, tmp_path)
    assert load_generated_images(saved.story.id, tmp_path) == []
    assert read_story_file(saved.story.id, record.file, tmp_path) is None


def test_deleting_passages_leaves_their_pictures(tmp_path: Path, saved: StoryBundle):
    session = make_session(tmp_path, saved, [])
    add_generated_images(saved.story.id, [generated("a1")], tmp_path)
    session.delete_from("u1")
    assert len(session.generated_images()) == 1


def test_a_copy_takes_the_pictures(tmp_path: Path, saved: StoryBundle):
    record = generated()
    write_story_file(saved.story.id, record.file, PNG, tmp_path)
    add_generated_images(saved.story.id, [record], tmp_path)
    copy = copy_story(saved.story.id, title="Copy", root=tmp_path)
    assert load_generated_images(copy.story.id, tmp_path)[0].id == record.id
    assert read_story_file(copy.story.id, record.file, tmp_path) == PNG
    assert read_story_file(copy.story.id, "images/refs/serrik.png", tmp_path) == PNG


def test_a_restart_and_a_scenario_keep_the_reference_pictures(tmp_path: Path, saved: StoryBundle):
    saved.cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    saved.story.image_style = "oil painting"
    record = generated()
    write_story_file(saved.story.id, record.file, PNG, tmp_path)
    add_generated_images(saved.story.id, [record], tmp_path)
    files = reference_files(saved, tmp_path)
    assert files == {"images/refs/serrik.png": PNG}

    again = fresh_playthrough(saved, "Again", files)
    save_story_bundle(again, root=tmp_path)
    assert read_story_file(again.story.id, "images/refs/serrik.png", tmp_path) == PNG
    assert again.story.image_style == "oil painting"
    # Pictures made in play belong to that playthrough.
    assert load_generated_images(again.story.id, tmp_path) == []

    path = tmp_path / "s.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(saved, files))
    imported = bundle_from_scenario(read_scenario(path))
    save_story_bundle(imported, root=tmp_path)
    assert imported.cast[0].reference_images[0].file == "images/refs/serrik.png"
    assert read_story_file(imported.story.id, "images/refs/serrik.png", tmp_path) == PNG


def test_an_archive_carries_every_picture(tmp_path: Path, saved: StoryBundle):
    record = generated()
    write_story_file(saved.story.id, record.file, PNG, tmp_path)
    add_generated_images(saved.story.id, [record], tmp_path)
    path = tmp_path / "a.sealedlore-archive.json"
    write_archive(path, saved, [], all_image_files(saved.story.id, tmp_path))

    imported = import_archive(path, root=tmp_path)
    assert imported.story.id != saved.story.id  # the original is still on disk
    assert load_generated_images(imported.story.id, tmp_path)[0].id == record.id
    assert read_story_file(imported.story.id, record.file, tmp_path) == PNG
    assert read_story_file(imported.story.id, "images/refs/serrik.png", tmp_path) == PNG


def test_an_archive_cannot_write_outside_the_story(tmp_path: Path, saved: StoryBundle):
    path = tmp_path / "a.sealedlore-archive.json"
    write_archive(path, saved, [], {"images/ok.png": PNG})
    data = json.loads(path.read_text())
    data["files"]["../../escape.png"] = base64.b64encode(PNG).decode()
    data["files"]["story.json"] = base64.b64encode(b"{}").decode()
    path.write_text(json.dumps(data))
    bundle, _ = read_archive(path)
    assert set(bundle.pending_files) == {"images/ok.png"}


def test_a_version_one_archive_still_imports(tmp_path: Path, saved: StoryBundle):
    path = tmp_path / "old.sealedlore-archive.json"
    write_archive(path, saved, [])
    data = json.loads(path.read_text())
    data["version"] = 1
    del data["files"]
    path.write_text(json.dumps(data))
    imported = import_archive(path, root=tmp_path)
    assert load_generated_images(imported.story.id, tmp_path) == []
