"""Pictures drawn on a server of the author's own: ComfyUI and AUTOMATIC1111 /
Forge (providers/local_images.py).

Never the network: both servers are played by an httpx MockTransport, with
the requests each really answers (ComfyUI's /object_info, /upload/image,
/prompt, /history, /view, /queue, /interrupt; AUTOMATIC1111's
/sdapi/v1/sd-models, txt2img, img2img, interrupt).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from sealedlore.call_log import CALLS, PLAIN_LOCAL
from sealedlore.engine.images import ImageRequest, run_image_request
from sealedlore.models.config import (
    LOCAL_IMAGE_ADDRESSES,
    Config,
    MediaEndpoint,
    ProviderConfig,
)
from sealedlore.models.image import RefUse
from sealedlore.providers.base import ProviderError, StreamCancelled
from sealedlore.providers.images import (
    ImageClient,
    build_image_payload,
    list_image_models,
    list_own_models,
    loggable_payload,
)
from sealedlore.providers.local_images import (
    LARGE_SIZES,
    SMALL_SIZES,
    WORKFLOW_MODEL,
    LocalDrawing,
    LocalSettings,
    WorkflowProblem,
    builtin_workflow,
    comfy_errors,
    dimensions,
    fill,
    native_sizes,
    pictures_taken,
    placeholders,
    read_workflow,
)
from sealedlore.storage.image_meta import strip_metadata
from sealedlore.storage.paths import story_dir
from sealedlore.storage.picture_store import DiskPictures

COMFY = "http://127.0.0.1:8188"
A1111 = "http://127.0.0.1:7860"
CHECKPOINTS = ["juggernautXL_v9.safetensors", "dreamshaper_8_sd15.safetensors"]


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_with_text(text: str = "a prompt; Seed: 1234") -> bytes:
    """A 1x1 PNG carrying what a local server writes into its pictures: the
    prompt and settings in a tEXt chunk."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"tEXt", b"parameters\x00" + text.encode())
        + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )


PICTURE = png_with_text()


@pytest.fixture(autouse=True)
def a_story(tmp_path: Path) -> None:
    """The story the pictures are kept in (a picture for a story deleted
    meanwhile is refused)."""
    folder = story_dir("s1", tmp_path)
    folder.mkdir(parents=True)
    (folder / "story.json").write_text("{}", encoding="utf-8")


def jane(file: str = "images/refs/jane.png") -> RefUse:
    return RefUse(owner_kind="character", owner_id="c1", owner_name="Jane", ref_id="r1", file=file)


def client(api: str, handler, *, key: str = "", settings: LocalSettings | None = None):
    base = COMFY if api == "comfyui" else A1111
    return ImageClient(
        base,
        key,
        api=api,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        local=settings,
    )


def request(model: str, *, size: str = "1024x1024", n: int = 1, refs=()) -> ImageRequest:
    return ImageRequest(
        story_id="s1",
        model=model,
        prompt="A farmhouse in the snow, at dusk.",
        size=size,
        n=n,
        references=tuple(refs),
        anchor_node_id=None,
    )


# --- the settings -----------------------------------------------------------------


def test_a_server_of_ones_own_starts_where_its_program_does_and_takes_no_chat_key():
    config = Config(
        providers=[ProviderConfig(name="NanoGPT", base_url="https://nano-gpt.com/api/v1",
                                  api_key="chat-key")],
        image_endpoint=MediaEndpoint(api="comfyui"),
    )
    media = config.images()
    assert media.base_url == LOCAL_IMAGE_ADDRESSES["comfyui"] == COMFY
    assert media.api == "comfyui" and media.local
    assert media.api_key == ""  # the chat's key is for the chat's host alone
    config.image_endpoint = MediaEndpoint(api="a1111", base_url="http://192.168.1.20:7860")
    assert config.images().base_url == "http://192.168.1.20:7860"


def test_its_workflows_and_settings_reach_the_resolved_endpoint():
    config = Config(
        image_endpoint=MediaEndpoint(
            api="comfyui",
            workflow='{"1": {}}',
            workflow_edit='{"2": {}}',
            strength=0.45,
            extra_body={"steps": 30},
        )
    )
    media = config.images()
    assert (media.workflow, media.workflow_edit, media.strength) == ('{"1": {}}', '{"2": {}}', 0.45)
    assert media.extra_body == {"steps": 30}
    with pytest.raises(ValueError):
        MediaEndpoint(api="a1111", strength=0.0)


def test_plain_http_to_somewhere_on_the_internet_is_still_refused():
    with pytest.raises(ValueError):
        MediaEndpoint(api="comfyui", base_url="http://example.com:8188")
    # An https server elsewhere (a rented one) is fine.
    MediaEndpoint(api="comfyui", base_url="https://comfy.example.com")


# --- sizes ------------------------------------------------------------------------


def test_sizes_follow_the_checkpoints_family():
    assert native_sizes("juggernautXL_v9.safetensors") == LARGE_SIZES
    assert native_sizes("ponyDiffusionV6.safetensors") == LARGE_SIZES
    assert native_sizes("dreamshaper_8_sd15.safetensors") == SMALL_SIZES
    assert native_sizes("v1-5-pruned-emaonly.safetensors") == SMALL_SIZES
    assert native_sizes("something.ckpt") == LARGE_SIZES


def test_a_size_is_pixels_a_ratio_its_nearest_or_a_tier_its_square():
    xl = "juggernautXL_v9.safetensors"
    assert dimensions("832x1216", xl) == (832, 1216)
    assert dimensions("833x1217", xl) == (832, 1216)  # to a multiple of 8
    assert dimensions("16:9", xl) == (1344, 768)
    assert dimensions("3:4", xl) == (896, 1152)
    assert dimensions("1k", xl) == (1024, 1024)
    assert dimensions("16:9", "dreamshaper_8_sd15.safetensors") == (768, 512)


# --- AUTOMATIC1111 / Forge ----------------------------------------------------------


def a1111_server(seen: list[httpx.Request], *, models=CHECKPOINTS, pictures: int = 1):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/sdapi/v1/sd-models":
            return httpx.Response(200, json=[{"title": m, "model_name": m} for m in models])
        if req.url.path in ("/sdapi/v1/txt2img", "/sdapi/v1/img2img"):
            # One picture a batch, and a ControlNet-style map after them.
            body = json.loads(req.content)
            made = [base64.b64encode(PICTURE).decode()] * body["batch_size"]
            info = json.dumps({"all_seeds": list(range(body["batch_size"]))})
            return httpx.Response(200, json={"images": made + ["bWFw"], "info": info})
        if req.url.path == "/sdapi/v1/interrupt":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "Not Found"})

    return handler


def test_automatic1111_lists_its_checkpoints_as_free_models_that_take_a_picture():
    seen: list[httpx.Request] = []
    http = httpx.Client(transport=httpx.MockTransport(a1111_server(seen)))
    models = list_image_models(A1111, api="a1111", client=http)
    assert [m.id for m in models] == CHECKPOINTS
    xl, sd15 = models
    assert xl.resolutions == LARGE_SIZES and sd15.resolutions == SMALL_SIZES
    assert xl.max_inputs == 1 and xl.price("1024x1024") == 0.0


def test_a_server_elsewhere_has_no_price_rather_than_a_free_one():
    seen: list[httpx.Request] = []
    http = httpx.Client(transport=httpx.MockTransport(a1111_server(seen)))
    (model, *_rest) = list_own_models("https://sd.example.com", api="a1111", client=http)
    assert model.price("1024x1024") is None


def test_txt2img_gets_the_prompt_as_approved_and_the_checkpoint_stays_loaded(tmp_path: Path):
    seen: list[httpx.Request] = []
    settings = LocalSettings(extra_body={"sampler_name": "DPM++ 2M", "steps": 30})
    image = client("a1111", a1111_server(seen), settings=settings)
    records = run_image_request(image, request(CHECKPOINTS[0], size="16:9", n=2), tmp_path)
    sent = json.loads(next(r for r in seen if r.url.path == "/sdapi/v1/txt2img").content)
    assert sent["prompt"] == "A farmhouse in the snow, at dusk."
    assert (sent["width"], sent["height"], sent["batch_size"]) == (1344, 768, 2)
    assert sent["override_settings"] == {"sd_model_checkpoint": CHECKPOINTS[0]}
    assert sent["override_settings_restore_afterwards"] is False
    assert sent["steps"] == 30 and sent["sampler_name"] == "DPM++ 2M"
    assert "init_images" not in sent
    # Two pictures, not the extension's map after them; nothing charged here.
    assert len(records) == 2
    assert all(r.cost == 0.0 and r.cost_reported for r in records)


def test_what_the_server_writes_into_its_pictures_is_taken_out(tmp_path: Path):
    seen: list[httpx.Request] = []
    image = client("a1111", a1111_server(seen))
    (record,) = run_image_request(image, request(CHECKPOINTS[0]), tmp_path)
    kept = DiskPictures("s1", tmp_path).read(record.file)
    assert b"parameters" not in kept and b"Seed" not in kept
    assert kept == strip_metadata(PICTURE)


def test_img2img_takes_the_picture_and_how_far_it_may_change(tmp_path: Path):
    seen: list[httpx.Request] = []
    store = DiskPictures("s1", tmp_path)
    store.write("images/refs/jane.png", PICTURE)
    image = client("a1111", a1111_server(seen), settings=LocalSettings(strength=0.4))
    use = jane()
    run_image_request(image, request(CHECKPOINTS[0], refs=[use]), tmp_path, store=store)
    sent = json.loads(next(r for r in seen if r.url.path == "/sdapi/v1/img2img").content)
    assert sent["denoising_strength"] == 0.4
    (picture,) = sent["init_images"]
    assert not picture.startswith("data:")
    assert base64.b64decode(picture) == strip_metadata(PICTURE)


def test_a_model_the_server_hasnt_got_is_said_before_anything_is_drawn(tmp_path: Path):
    seen: list[httpx.Request] = []
    image = client("a1111", a1111_server(seen))
    with pytest.raises(ProviderError) as caught:
        run_image_request(image, request("bytedance/seedream-v5.0-pro"), tmp_path)
    assert "has no model called 'bytedance/seedream-v5.0-pro'" in str(caught.value)
    assert "Choose beside each model" in str(caught.value)
    assert not any(r.url.path.endswith("txt2img") for r in seen)


def test_api_auth_is_basic_from_a_user_password_key():
    seen: list[httpx.Request] = []
    image = client("a1111", a1111_server(seen), key="jane:secret")
    image.generate(build_image_payload(CHECKPOINTS[0], "p", "1k", 1, [], api="a1111"))
    assert all(r.headers["authorization"] == "Basic amFuZTpzZWNyZXQ=" for r in seen)


def test_an_api_not_switched_on_is_said_how_to_switch_on():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="--api"):
        list_own_models(A1111, api="a1111", client=http)
    # The quiet listing (the picker's) is just empty.
    assert list_image_models(A1111, api="a1111", client=http) == []


def test_nothing_answering_is_said_as_such():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=req)

    image = client("comfyui", handler)
    with pytest.raises(ProviderError, match="Nothing answered at http://127.0.0.1:8188"):
        image.generate(build_image_payload("m", "p", "1k", 1, [], api="comfyui"))


def test_stop_asks_automatic1111_to_stop_drawing():
    seen: list[httpx.Request] = []
    image: ImageClient

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/sdapi/v1/txt2img":
            image.cancel()
            raise httpx.ReadError("socket shut", request=req)
        return a1111_server([])(req)

    image = client("a1111", handler)
    with pytest.raises(StreamCancelled):
        image.generate(build_image_payload(CHECKPOINTS[0], "p", "1k", 1, [], api="a1111"))
    assert seen[-1].url.path == "/sdapi/v1/interrupt"


# --- ComfyUI ------------------------------------------------------------------------


class Comfy:
    """A ComfyUI that keeps what it was sent: the workflows queued, the
    pictures uploaded, and how often its history was asked."""

    def __init__(self, *, finish_after: int = 1, error: dict | None = None) -> None:
        self.queued: list[dict] = []
        self.uploads: list[bytes] = []
        self.history_asks = 0
        self.finish_after = finish_after
        self.error = error
        self.seen: list[httpx.Request] = []
        self.running: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        path = req.url.path
        if path == "/object_info/CheckpointLoaderSimple":
            spec = {"required": {"ckpt_name": [CHECKPOINTS, {}]}}
            return httpx.Response(200, json={"CheckpointLoaderSimple": {"input": spec}})
        if path == "/object_info/UNETLoader":
            options = {"options": ["flux1-dev.safetensors"]}
            spec = {"required": {"unet_name": ["COMBO", options]}}
            return httpx.Response(200, json={"UNETLoader": {"input": spec}})
        if path == "/upload/image":
            self.uploads.append(req.content)
            return httpx.Response(200, json={"name": "up.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            if self.error is not None:
                return httpx.Response(400, json=self.error)
            body = json.loads(req.content)
            self.queued.append(body["prompt"])
            prompt_id = f"p{len(self.queued)}"
            self.running.append(prompt_id)
            return httpx.Response(200, json={"prompt_id": prompt_id, "number": 1})
        if path.startswith("/history/"):
            self.history_asks += 1
            prompt_id = path.rsplit("/", 1)[1]
            if self.history_asks < self.finish_after:
                return httpx.Response(200, json={})
            outputs = {"7": {"images": [{"filename": "a.png", "subfolder": "", "type": "temp"}]}}
            entry = {"outputs": outputs, "status": {"status_str": "success", "completed": True}}
            return httpx.Response(200, json={prompt_id: entry})
        if path == "/view":
            assert req.url.params["type"] == "temp"
            return httpx.Response(200, content=PICTURE)
        if path == "/queue":
            if req.method == "GET":
                running = [[0, pid, {}, {}, []] for pid in self.running]
                return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
            return httpx.Response(200, json={})
        if path == "/interrupt":
            return httpx.Response(200, json={})
        return httpx.Response(404, text="404: Not Found")


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr("sealedlore.providers.local_images.POLL_SECONDS", 0.001)


def test_comfyui_lists_the_checkpoints_its_workflow_takes():
    http = httpx.Client(transport=httpx.MockTransport(Comfy()))
    models = list_own_models(COMFY, api="comfyui", client=http)
    assert [m.id for m in models] == CHECKPOINTS
    assert models[0].max_inputs == 1


def test_the_built_in_workflow_is_filled_and_its_picture_fetched(tmp_path: Path):
    comfy = Comfy(finish_after=3)
    image = client("comfyui", comfy)
    (record,) = run_image_request(image, request(CHECKPOINTS[0], size="16:9"), tmp_path)
    (graph,) = comfy.queued
    assert graph["1"]["inputs"]["ckpt_name"] == CHECKPOINTS[0]
    assert graph["2"]["inputs"]["text"] == "A farmhouse in the snow, at dusk."
    latent = graph["4"]["inputs"]
    assert (latent["width"], latent["height"], latent["batch_size"]) == (1344, 768, 1)
    sampler = graph["5"]["inputs"]
    assert isinstance(sampler["seed"], int) and sampler["denoise"] == 1.0
    assert sampler["steps"] == 25 and sampler["sampler_name"] == "dpmpp_2m"
    assert graph["7"]["class_type"] == "PreviewImage"  # nothing left in its output folder
    assert comfy.history_asks == 3
    assert record.cost == 0.0
    assert DiskPictures("s1", tmp_path).read(record.file) == strip_metadata(PICTURE)


def test_from_a_picture_it_is_uploaded_and_sampled_at_the_strength(tmp_path: Path):
    comfy = Comfy()
    store = DiskPictures("s1", tmp_path)
    store.write("images/refs/jane.png", PICTURE)
    image = client("comfyui", comfy, settings=LocalSettings(strength=0.55))
    use = jane()
    run_image_request(image, request(CHECKPOINTS[0], refs=[use]), tmp_path, store=store)
    (graph,) = comfy.queued
    assert graph["8"]["inputs"]["image"] == "up.png"
    assert graph["5"]["inputs"]["denoise"] == 0.55
    assert graph["5"]["inputs"]["latent_image"] == ["10", 0]
    (upload,) = comfy.uploads
    assert strip_metadata(PICTURE) in upload  # its hidden data taken out first


def test_a_workflow_of_ones_own_is_filled_at_its_placeholders(tmp_path: Path):
    mine = {
        "10": {"class_type": "UNETLoader", "inputs": {"unet_name": "{{model}}"}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}, film grain"}},
        "12": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": 1},
        },
        "13": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}"}},
        "14": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
    }
    comfy = Comfy()
    image = client("comfyui", comfy, settings=LocalSettings(workflow=json.dumps(mine)))
    http = httpx.Client(transport=httpx.MockTransport(comfy))
    listed = list_own_models(COMFY, api="comfyui", client=http, local=image.local)
    assert [m.id for m in listed] == ["flux1-dev.safetensors"]
    # Two pictures asked of a workflow that draws one: it runs twice.
    records = run_image_request(image, request("flux1-dev.safetensors", n=2), tmp_path)
    assert len(records) == 2 and len(comfy.queued) == 2
    first, second = comfy.queued
    assert first["10"]["inputs"]["unet_name"] == "flux1-dev.safetensors"
    assert first["11"]["inputs"]["text"] == "A farmhouse in the snow, at dusk., film grain"
    assert (first["12"]["inputs"]["width"], first["12"]["inputs"]["height"]) == (1024, 1024)
    assert first["13"]["inputs"]["seed"] != second["13"]["inputs"]["seed"]


def test_a_workflow_naming_its_own_model_is_the_one_model_offered():
    mine = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.ckpt"}}}
    mine["2"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}}
    http = httpx.Client(transport=httpx.MockTransport(Comfy()))
    (only,) = list_own_models(
        COMFY, api="comfyui", client=http, local=LocalSettings(workflow=json.dumps(mine))
    )
    assert only.id == WORKFLOW_MODEL
    # The built-in way from a picture needs a checkpoint, which it doesn't give.
    assert only.max_inputs == 0


def test_the_editors_own_save_is_refused_with_the_way_to_export_for_the_api():
    with pytest.raises(WorkflowProblem, match="Export \\(API\\)"):
        read_workflow(json.dumps({"nodes": [], "links": []}))
    with pytest.raises(WorkflowProblem, match="isn't JSON"):
        read_workflow("{not json")
    with pytest.raises(WorkflowProblem, match="class_type"):
        read_workflow(json.dumps({"1": {"inputs": {}}}))
    # A whole /prompt body saved is read for its workflow.
    assert read_workflow(json.dumps({"prompt": {"1": {"class_type": "A", "inputs": {}}}}))


def test_placeholders_are_listed_known_first_and_pictures_counted():
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "{{image2}}"}},
        "3": {"class_type": "X", "inputs": {"text": "{{prompt}} {{mood}}"}},
    }
    assert placeholders(graph) == ["prompt", "image", "image2", "mood"]
    assert pictures_taken(graph) == 2
    assert pictures_taken(builtin_workflow(with_picture=True)) == 1
    assert pictures_taken(builtin_workflow(with_picture=False)) == 0


def test_a_value_fills_a_whole_placeholder_as_it_is_and_a_part_as_text():
    graph = {"1": {"class_type": "X", "inputs": {"a": "{{width}}", "b": "w={{width}}", "c": [
        "{{seed}}"]}}}
    filled = fill(graph, {"width": 832, "seed": 7})
    assert filled["1"]["inputs"] == {"a": 832, "b": "w=832", "c": [7]}
    # One with no value is left, for ComfyUI to say where it is.
    assert fill(graph, {})["1"]["inputs"]["a"] == "{{width}}"


def test_comfyuis_refusal_of_a_workflow_names_the_node(tmp_path: Path):
    error = {
        "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed"},
        "node_errors": {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "errors": [{"message": "Value not in list", "details": "ckpt_name: 'x'"}],
            }
        },
    }
    image = client("comfyui", Comfy(error=error))
    with pytest.raises(ProviderError) as caught:
        run_image_request(image, request(CHECKPOINTS[0]), tmp_path)
    said = str(caught.value)
    assert "Prompt outputs failed" in said
    assert "CheckpointLoaderSimple 1: Value not in list: ckpt_name: 'x'" in said
    assert comfy_errors("not json") == "not json"


def test_an_error_while_it_runs_is_said(tmp_path: Path):
    comfy = Comfy()
    base = comfy.__call__

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/history/"):
            status = {
                "status_str": "error",
                "messages": [["execution_error", {"node_type": "KSampler",
                                                  "exception_message": "out of memory"}]],
            }
            return httpx.Response(200, json={"p1": {"outputs": {}, "status": status}})
        return base(req)

    with pytest.raises(ProviderError, match="KSampler: out of memory"):
        run_image_request(client("comfyui", handler), request(CHECKPOINTS[0]), tmp_path)


def test_stop_takes_ours_out_of_the_queue_and_interrupts_only_ours():
    comfy = Comfy(finish_after=10**6)
    image: ImageClient
    base = comfy.__call__

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/history/") and comfy.history_asks >= 2:
            image.cancel()
        return base(req)

    image = client("comfyui", handler)
    with pytest.raises(StreamCancelled):
        image.generate(build_image_payload(CHECKPOINTS[0], "p", "1k", 1, [], api="comfyui"))
    deleted = next(r for r in comfy.seen if r.url.path == "/queue" and r.method == "POST")
    assert json.loads(deleted.content) == {"delete": ["p1"]}
    interrupt = next(r for r in comfy.seen if r.url.path == "/interrupt")
    assert json.loads(interrupt.content) == {"prompt_id": "p1"}


def test_someone_elses_running_job_is_not_interrupted():
    comfy = Comfy(finish_after=10**6)
    image: ImageClient
    base = comfy.__call__

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/history/"):
            comfy.running = ["theirs"]  # ours waits in the queue behind it
            image.cancel()
        return base(req)

    image = client("comfyui", handler)
    with pytest.raises(StreamCancelled):
        image.generate(build_image_payload(CHECKPOINTS[0], "p", "1k", 1, [], api="comfyui"))
    assert not any(r.url.path == "/interrupt" for r in comfy.seen)


def test_the_check_refuses_a_workflow_model_where_a_workflow_wants_one():
    mine = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}}}
    drawing = LocalDrawing(
        "comfyui",
        ImageClient(COMFY, "", api="comfyui", client=httpx.Client(
            transport=httpx.MockTransport(Comfy()))).http,
        COMFY,
        "",
        LocalSettings(workflow=json.dumps(mine)),
    )
    drawing.check_model(WORKFLOW_MODEL, with_picture=False)  # its own: fine
    with pytest.raises(ProviderError, match="Give both workflows a"):
        drawing.check_model(WORKFLOW_MODEL, with_picture=True)


# --- the log --------------------------------------------------------------------------


def test_the_log_names_the_pictures_and_the_call_log_the_server():
    payload = build_image_payload("m", "p", "1k", 1, ["data:image/png;base64,AA"], api="comfyui")
    assert payload == {"model": "m", "prompt": "p", "size": "1k", "n": 1,
                       "images": ["data:image/png;base64,AA"]}
    assert loggable_payload(payload, ["Jane: a.png"])["images"] == ["<Jane: a.png>"]
    before = CALLS.version
    image = client("comfyui", Comfy())
    image.generate(build_image_payload(CHECKPOINTS[0], "p", "1k", 1, [], api="comfyui"))
    assert CALLS.version != before
    call = CALLS.calls()[-1]
    assert call.endpoint == "ComfyUI" and call.protection == PLAIN_LOCAL
    assert call.cost == 0.0
