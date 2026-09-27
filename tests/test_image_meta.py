"""Pictures lose their hidden data on the way in and on the way out.

The samples are real pictures written by Qt, with a colour profile, and
with EXIF (a camera, an orientation, a thumbnail), XMP, comments, C2PA and
a trailer spliced into their structure the way cameras and editors put them
there. Stripped, none of it may be left; the pixels, the profile and the
orientation must be, byte for byte where the image is.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QColorSpace, QImage, QImageWriter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.images import (  # noqa: E402
    ImageRequest,
    UnstrippableReference,
    run_image_request,
)
from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.engine.tokens import TokenEstimator, fallback_counter  # noqa: E402
from sealedlore.gui.ref_images import MAX_SIDE, load_image, picture_bytes  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.image import RefUse  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.archive import import_archive, write_archive  # noqa: E402
from sealedlore.storage.image_meta import (  # noqa: E402
    MetadataError,
    picture_kind,
    strip_metadata,
)
from sealedlore.storage.images import (  # noqa: E402
    all_image_files,
    read_story_file,
    write_story_file,
)
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from sealedlore.storage.scenario import (  # noqa: E402
    ScenarioError,
    bundle_from_scenario,
    read_scenario,
    scenario_from_bundle,
    write_scenario,
)
from tests.conftest import make_exchange  # noqa: E402
from tests.test_images import image_client, reply  # noqa: E402

# What a camera, an editor or a generator leaves behind: none may survive.
SECRETS = (
    b"SECRET-CAMERA",
    b"SECRET-THUMBNAIL",
    b"SECRET-XMP",
    b"SECRET-COMMENT",
    b"SECRET-C2PA",
    b"SECRET-IPTC",
    b"SECRET-MPF",
    b"SECRET-TRAILER",
    b"SECRET-TEXT",
    b"SECRET-TIME",
    b"SECRET-CHUNK",
)
# One transparent pixel, with a comment block before its image.
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAA") + (
    b"\x21\xfe\x0eSECRET-COMMENT\x00" + base64.b64decode("LAAAAAABAAEAAAIBRAA7")
)


@pytest.fixture(scope="module", autouse=True)
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def leftovers(data: bytes) -> list[bytes]:
    return [secret for secret in SECRETS if secret in data]


def qt_picture(fmt: str, width: int = 64, height: int = 48, progressive: bool = False) -> bytes:
    """A noisy picture, so a JPEG's coded data holds stuffed 0xFF bytes,
    with a Display P3 profile."""
    image = QImage(width, height, QImage.Format_RGB32)
    for y in range(height):
        for x in range(width):
            image.setPixel(x, y, (x * 7919 + y * 104729) * 2654435761 & 0xFFFFFF)
    image.setColorSpace(QColorSpace(QColorSpace.DisplayP3))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    writer = QImageWriter(buffer, fmt.encode())
    writer.setProgressiveScanWrite(progressive)
    writer.setQuality(95)
    assert writer.write(image)
    buffer.close()
    return bytes(data.data())


def exif_tiff(orientation: int, order: str = "little") -> bytes:
    """IFD0 with Make and Orientation, and an IFD1 thumbnail after it."""
    fmt = "<" if order == "little" else ">"
    make = b"SECRET-CAMERA\x00"
    thumbnail = b"SECRET-THUMBNAIL"
    ifd0_at, ifd1_at = 8, 8 + 2 + 2 * 12 + 4
    make_at = ifd1_at + 2 + 12 + 4
    thumb_at = make_at + len(make)
    return (
        (b"II" if order == "little" else b"MM")
        + struct.pack(fmt + "HI", 42, ifd0_at)
        + struct.pack(fmt + "H", 2)
        + struct.pack(fmt + "HHII", 0x010F, 2, len(make), make_at)
        + struct.pack(fmt + "HHIHH", 0x0112, 3, 1, orientation, 0)
        + struct.pack(fmt + "I", ifd1_at)
        + struct.pack(fmt + "H", 1)
        + struct.pack(fmt + "HHII", 0x0201, 4, 1, thumb_at)
        + struct.pack(fmt + "I", 0)
        + make
        + thumbnail
    )


def jpeg_segment(code: int, body: bytes) -> bytes:
    return bytes((0xFF, code)) + struct.pack(">H", len(body) + 2) + body


def dirty_jpeg(orientation: int = 6, progressive: bool = False, **size) -> bytes:
    clean = qt_picture("JPEG", progressive=progressive, **size)
    blocks = (
        jpeg_segment(0xE1, b"Exif\x00\x00" + exif_tiff(orientation))
        + jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>SECRET-XMP</x:xmpmeta>")
        + jpeg_segment(0xE2, b"MPF\x00SECRET-MPF")
        + jpeg_segment(0xEB, b"JP\x00\x00SECRET-C2PA")
        + jpeg_segment(0xED, b"Photoshop 3.0\x00SECRET-IPTC")
        + jpeg_segment(0xFE, b"SECRET-COMMENT")
    )
    # After Qt's own JFIF block, as a camera would; a phone's motion photo
    # appends its video after the end of the image.
    at = clean.index(b"\xff", 2)
    at = clean.index(b"\xff", at + 2)
    return clean[:at] + blocks + clean[at:] + b"ftypmp42SECRET-TRAILER"


def png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def dirty_png(orientation: int = 8) -> bytes:
    clean = qt_picture("PNG")
    header_end = 8 + 12 + 13  # the signature and IHDR
    extra = (
        png_chunk(b"tEXt", b"Comment\x00SECRET-TEXT")
        + png_chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00SECRET-XMP")
        + png_chunk(b"zTXt", b"Author\x00\x00" + zlib.compress(b"x") + b"SECRET-COMMENT")
        + png_chunk(b"tIME", b"SECRET-TIME")
        + png_chunk(b"eXIf", exif_tiff(orientation, order="big"))
        + png_chunk(b"caBX", b"SECRET-C2PA")
    )
    return clean[:header_end] + extra + clean[header_end:] + b"SECRET-TRAILER"


def riff_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    chunks, pos = [], 12
    while pos < len(data):
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        chunks.append((data[pos : pos + 4], data[pos + 8 : pos + 8 + size]))
        pos += 8 + size + (size & 1)
    return chunks


def riff(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = b"WEBP" + b"".join(
        kind + struct.pack("<I", len(data)) + data + b"\x00" * (len(data) & 1)
        for kind, data in chunks
    )
    return b"RIFF" + struct.pack("<I", len(body)) + body


def dirty_webp(orientation: int = 3, exif_prefix: bool = False) -> bytes:
    chunks = riff_chunks(qt_picture("WEBP"))
    assert chunks[0][0] == b"VP8X"
    header = chunks[0][1]
    chunks[0] = (b"VP8X", bytes((header[0] | 0x08 | 0x04,)) + header[1:])
    tiff = exif_tiff(orientation)
    chunks += [
        (b"EXIF", (b"Exif\x00\x00" if exif_prefix else b"") + tiff),
        (b"XMP ", b"<x:xmpmeta>SECRET-XMP</x:xmpmeta>"),
        (b"SECR", b"SECRET-CHUNK"),
    ]
    return riff(chunks) + b"SECRET-TRAILER"


def oriented_size(data: bytes, tmp_path: Path) -> tuple[int, int]:
    """The size a viewer that honours the orientation shows."""
    path = tmp_path / f"shown{picture_kind(data)}"
    path.write_bytes(data)
    return load_image(path).size().toTuple()


# --- the stripper ------------------------------------------------------------------


@pytest.mark.parametrize("progressive", [False, True])
def test_a_jpeg_keeps_its_image_profile_and_orientation_and_nothing_else(
    tmp_path: Path, progressive: bool
):
    dirty = dirty_jpeg(orientation=6, progressive=progressive)
    assert len(leftovers(dirty)) == 8
    clean = strip_metadata(dirty)
    assert leftovers(clean) == []
    # Not re-encoded: the coded image is the same bytes, so it decodes the same.
    scan = dirty.index(b"\xff\xda")
    assert clean[clean.index(b"\xff\xda") :] == dirty[scan : dirty.rindex(b"\xff\xd9") + 2]
    assert QImage.fromData(clean) == QImage.fromData(dirty)
    assert QImage.fromData(clean).colorSpace() == QColorSpace(QColorSpace.DisplayP3)
    # Turned a quarter: 64 x 48 stored, shown 48 x 64.
    assert oriented_size(clean, tmp_path) == (48, 64)
    assert strip_metadata(clean) == clean


def test_a_png_keeps_its_image_profile_and_orientation_and_nothing_else(tmp_path: Path):
    dirty = dirty_png(orientation=8)
    assert len(leftovers(dirty)) == 8
    clean = strip_metadata(dirty)
    assert leftovers(clean) == []
    assert QImage.fromData(clean) == QImage.fromData(dirty)
    assert QImage.fromData(clean).colorSpace() == QColorSpace(QColorSpace.DisplayP3)
    assert b"eXIf" in clean and b"\x01\x12\x00\x03\x00\x00\x00\x01\x00\x08" in clean
    assert clean.endswith(png_chunk(b"IEND", b""))
    assert strip_metadata(clean) == clean


@pytest.mark.parametrize("exif_prefix", [False, True])
def test_a_webp_keeps_its_image_profile_and_orientation_and_nothing_else(exif_prefix: bool):
    dirty = dirty_webp(orientation=3, exif_prefix=exif_prefix)
    clean = strip_metadata(dirty)
    assert leftovers(clean) == []
    assert QImage.fromData(clean) == QImage.fromData(dirty)
    assert QImage.fromData(clean).colorSpace() == QColorSpace(QColorSpace.DisplayP3)
    chunks = riff_chunks(clean)
    assert [kind for kind, _ in chunks if kind not in (b"VP8 ", b"VP8L", b"ALPH")] == [
        b"VP8X",
        b"ICCP",
        b"EXIF",
    ]
    flags = chunks[0][1][0]
    assert flags & 0x08 and not flags & 0x04 and flags & 0x20
    assert b"\x01\x12\x00\x03\x00\x00\x00\x01\x00\x03" in dict(chunks)[b"EXIF"]
    assert strip_metadata(clean) == clean


def test_an_upright_photo_keeps_no_exif_at_all():
    clean = strip_metadata(dirty_jpeg(orientation=1))
    assert b"Exif" not in clean and leftovers(clean) == []


def test_the_small_png_the_other_tests_use_is_already_clean():
    from tests.test_images import PNG

    assert strip_metadata(PNG) == PNG


@pytest.mark.parametrize(
    "data",
    [
        GIF,
        b"not a picture",
        b"",
        qt_picture("PNG")[:-12],  # no IEND: cut off
        b"\xff\xd8" + jpeg_segment(0xDB, b"\x00" * 65)[:30],  # a block past the end
        b"\xff\xd8" + jpeg_segment(0xFE, b"only a comment") + b"\xff\xd9",  # no image
        b"\xff\xd8\xff\xf7\x00\x04ab",  # a JPEG-LS frame this app doesn't know
        riff([(b"XMP ", b"no image")]),
    ],
)
def test_anything_that_cant_be_followed_is_refused(data: bytes):
    with pytest.raises(MetadataError):
        strip_metadata(data)


# --- on the way in -------------------------------------------------------------------


def test_a_picture_added_from_disk_is_stripped_without_re_encoding(tmp_path: Path):
    source = tmp_path / "IMG_2041.jpg"
    source.write_bytes(dirty_jpeg())
    data, suffix = picture_bytes(source)
    assert suffix == ".jpg" and data == strip_metadata(source.read_bytes())


def test_a_large_photo_is_stored_upright_and_clean(tmp_path: Path):
    source = tmp_path / "IMG_2042.jpg"
    source.write_bytes(dirty_jpeg(orientation=6, width=3000, height=2000))
    data, suffix = picture_bytes(source)
    assert suffix == ".jpg" and leftovers(data) == [] and b"Exif" not in data
    stored = QImage.fromData(data)
    # The orientation is in the pixels: tall, as the camera was held.
    assert (stored.width(), stored.height()) == (1365, MAX_SIDE)


def test_a_re_encoded_png_carries_none_of_its_text(tmp_path: Path):
    image = QImage(3000, 1000, QImage.Format_ARGB32)
    image.fill(QColor(20, 40, 60, 128))
    image.setText("Comment", "SECRET-TEXT")
    image.setText("XML:com.adobe.xmp", "SECRET-XMP")
    source = tmp_path / "big.png"
    assert image.save(str(source))
    data, suffix = picture_bytes(source)
    assert suffix == ".png" and leftovers(data) == []


def test_a_picture_that_cant_be_stripped_as_it_is_is_re_encoded(tmp_path: Path):
    # A .gif is always re-encoded; a JPEG whose blocks can't be followed
    # would be too. Either way what is kept is clean.
    source = tmp_path / "tiny.gif"
    source.write_bytes(GIF)
    data, suffix = picture_bytes(source)
    assert suffix == ".png" and strip_metadata(data) == data and leftovers(data) == []


@pytest.fixture
def saved(tmp_path: Path, story, cast) -> StoryBundle:
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    return bundle


def test_a_drawn_picture_is_kept_clean(tmp_path: Path, saved: StoryBundle):
    drawn = dirty_png()

    def handler(_request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(drawn).decode()}]})

    request = ImageRequest(
        story_id=saved.story.id, model="m", prompt="p", size="1k", n=1,
        references=(), anchor_node_id=None,
    )  # fmt: skip
    records = run_image_request(image_client(handler), request, tmp_path)
    assert read_story_file(saved.story.id, records[0].file, tmp_path) == strip_metadata(drawn)
    assert "metadata_kept" not in read_api_log(saved.story.id, root=tmp_path)[-1]


def test_a_drawn_picture_that_cant_be_stripped_is_kept_and_logged(
    tmp_path: Path, saved: StoryBundle
):
    def handler(_request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(GIF).decode()}]})

    request = ImageRequest(
        story_id=saved.story.id, model="m", prompt="p", size="1k", n=1,
        references=(), anchor_node_id=None,
    )  # fmt: skip
    records = run_image_request(image_client(handler), request, tmp_path)
    # Paid for, so kept; refused if it is ever sent on.
    assert read_story_file(saved.story.id, records[0].file, tmp_path) == GIF
    assert read_api_log(saved.story.id, root=tmp_path)[-1]["metadata_kept"] == [records[0].file]


def test_an_imported_scenario_and_backup_bring_clean_pictures(tmp_path: Path, saved: StoryBundle):
    dirty = dirty_jpeg()
    scenario = scenario_from_bundle(saved, {"images/refs/r.jpg": dirty})
    path = tmp_path / "s.sealedlore-scenario.json"
    # Written by some other program: the pictures as they came.
    path.write_text(json.dumps(scenario.model_dump()))
    imported = bundle_from_scenario(read_scenario(path))
    save_story_bundle(imported, root=tmp_path)
    assert read_story_file(imported.story.id, "images/refs/r.jpg", tmp_path) == strip_metadata(
        dirty
    )

    backup = tmp_path / "b.sealedlore-archive.json"
    write_archive(backup, saved, [], {"images/g.jpg": dirty, "images/refs/odd.gif": GIF})
    restored = import_archive(backup, root=tmp_path)
    assert read_story_file(restored.story.id, "images/g.jpg", tmp_path) == strip_metadata(dirty)
    # One that can't be stripped comes in as it was: it is the author's own.
    assert read_story_file(restored.story.id, "images/refs/odd.gif", tmp_path) == GIF


# --- a story saved before this -------------------------------------------------------


def load(tmp_path: Path, story_id: str) -> StorySession:
    return StorySession.load(
        story_id,
        Config(scene_reads="manual"),
        MockChatProvider([]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_an_older_story_is_cleaned_once_when_it_opens(tmp_path: Path, saved: StoryBundle):
    assert not saved.story.images_cleaned
    dirty = dirty_jpeg()
    write_story_file(saved.story.id, "images/refs/r.jpg", dirty, tmp_path)
    write_story_file(saved.story.id, "images/background.png", dirty_png(), tmp_path)
    write_story_file(saved.story.id, "images/refs/odd.gif", GIF, tmp_path)

    session = load(tmp_path, saved.story.id)
    files = all_image_files(saved.story.id, tmp_path)
    assert files["images/refs/r.jpg"] == strip_metadata(dirty)
    assert leftovers(files["images/background.png"]) == []
    assert files["images/refs/odd.gif"] == GIF
    assert any("images/refs/odd.gif" in notice for notice in session.bundle.notices)
    assert load_story_bundle(saved.story.id, root=tmp_path).story.images_cleaned

    # Once: the flag is saved, and the folder isn't read again.
    write_story_file(saved.story.id, "images/refs/r.jpg", dirty, tmp_path)
    load(tmp_path, saved.story.id)
    assert read_story_file(saved.story.id, "images/refs/r.jpg", tmp_path) == dirty


# --- on the way out ------------------------------------------------------------------


def ref_use(file: str) -> RefUse:
    return RefUse(
        owner_kind="character", owner_id="c", owner_name="Serrik Vaun", ref_id="r", file=file
    )


def test_a_reference_is_sent_stripped(tmp_path: Path, saved: StoryBundle):
    # Written straight to the folder, as a story from before this kept it.
    dirty = dirty_jpeg()
    write_story_file(saved.story.id, "images/refs/r.jpg", dirty, tmp_path)
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return reply()

    request = ImageRequest(
        story_id=saved.story.id, model="m", prompt="p", size="1k", n=1,
        references=(ref_use("images/refs/r.jpg"),), anchor_node_id=None,
    )  # fmt: skip
    run_image_request(image_client(handler), request, tmp_path)
    url = sent[0]["imageDataUrls"][0]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.partition(",")[2]) == strip_metadata(dirty)


def test_a_reference_that_cant_be_stripped_sends_nothing(tmp_path: Path, saved: StoryBundle):
    # A GIF under a .png name: not a JPEG, PNG or WebP, whatever it is called.
    write_story_file(saved.story.id, "images/refs/odd.png", GIF, tmp_path)
    calls: list = []
    request = ImageRequest(
        story_id=saved.story.id, model="m", prompt="p", size="1k", n=1,
        references=(ref_use("images/refs/odd.png"),), anchor_node_id=None,
    )  # fmt: skip
    with pytest.raises(UnstrippableReference, match="Serrik Vaun"):
        run_image_request(image_client(lambda r: calls.append(r) or reply()), request, tmp_path)
    assert calls == []
    assert [entry["kind"] for entry in read_api_log(saved.story.id, root=tmp_path)] == []


def test_a_scenario_is_written_with_clean_pictures_or_not_at_all(
    tmp_path: Path, saved: StoryBundle
):
    dirty = dirty_png()
    path = tmp_path / "s.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(saved, {"images/refs/r.png": dirty}))
    written = json.loads(path.read_text())["reference_files"]["images/refs/r.png"]
    assert base64.b64decode(written) == strip_metadata(dirty)

    refused = tmp_path / "refused.sealedlore-scenario.json"
    scenario = scenario_from_bundle(saved, {"images/refs/r.png": dirty, "images/refs/o.gif": GIF})
    with pytest.raises(ScenarioError, match="images/refs/o.gif"):
        write_scenario(refused, scenario)
    assert not refused.exists()
