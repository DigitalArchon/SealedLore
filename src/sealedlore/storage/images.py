"""Image files and images.json.

Every picture lives under its story's `images/` folder: generated ones at the
top, reference pictures in `images/refs/`. Records point at them by a path
relative to the story folder, and every such path is checked to stay inside
it: archives and scenarios carry their own.

images.json has one writer, this module, and is never part of
`save_story_bundle`: a picture finishes on its own thread whenever it
finishes, and a session save rewriting the bundle from memory would drop it.

Pictures are kept with their hidden data removed (`storage.image_meta`);
`clean_story_images` does it for those saved before that was so.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from sealedlore.models.character import Character
from sealedlore.models.image import GeneratedImage, ImageRef
from sealedlore.storage.atomic import atomic_write_json, open_private, read_json, replace
from sealedlore.storage.image_meta import MetadataError, strip_metadata
from sealedlore.storage.paths import story_dir

IMAGES_DIR = "images"
REFS_DIR = "images/refs"
IMAGES_FILE = "images.json"

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def is_safe_relative(relative: str) -> bool:
    """A path inside the story's images folder, or images.json itself.

    On Windows a colon is refused too: "images/a:b.png" names stream b.png of
    a file "a" (NTFS), and "images/C:x" a drive-relative path."""
    if relative == IMAGES_FILE:
        return True
    path = PurePosixPath(relative)
    return (
        not path.is_absolute()
        and "\\" not in relative
        and not (sys.platform == "win32" and ":" in relative)
        and len(path.parts) >= 2
        and path.parts[0] == IMAGES_DIR
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def story_path(story_id: str, relative: str, root: Path | None = None) -> Path:
    if not is_safe_relative(relative):
        raise ValueError(f"not a path inside the story's images: {relative!r}")
    return story_dir(story_id, root) / relative


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    fd = open_private(tmp_path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    replace(tmp_path, path)


def write_story_file(story_id: str, relative: str, data: bytes, root: Path | None = None) -> None:
    write_bytes_atomic(story_path(story_id, relative, root), data)


def read_story_file(story_id: str, relative: str, root: Path | None = None) -> bytes | None:
    try:
        return story_path(story_id, relative, root).read_bytes()
    except (OSError, ValueError):
        return None


# --- generated images ---------------------------------------------------------


def load_generated_images(story_id: str, root: Path | None = None) -> list[GeneratedImage]:
    data = read_json(story_dir(story_id, root) / IMAGES_FILE, [])
    return [GeneratedImage.model_validate(item) for item in data]


def save_generated_images(
    story_id: str, images: Iterable[GeneratedImage], root: Path | None = None
) -> None:
    atomic_write_json(
        story_dir(story_id, root) / IMAGES_FILE, [image.model_dump() for image in images]
    )


def add_generated_images(
    story_id: str, records: Iterable[GeneratedImage], root: Path | None = None
) -> list[GeneratedImage]:
    """Append to that story's images.json, whichever story is open."""
    images = [*load_generated_images(story_id, root), *records]
    save_generated_images(story_id, images, root)
    return images


def remove_generated_image(
    story_id: str, image_id: str, root: Path | None = None
) -> list[GeneratedImage]:
    """Drop a record and its file."""
    images = load_generated_images(story_id, root)
    kept = [image for image in images if image.id != image_id]
    for image in images:
        if image.id == image_id:
            try:
                story_path(story_id, image.file, root).unlink(missing_ok=True)
            except ValueError:
                pass
    save_generated_images(story_id, kept, root)
    return kept


# --- reference pictures -------------------------------------------------------


def owners_refs(
    cast: Iterable[Character], supporting: Iterable[Character], lore: Iterable
) -> list[ImageRef]:
    refs: list[ImageRef] = []
    for owner in [*cast, *supporting, *lore]:
        refs.extend(owner.reference_images)
    return refs


def bundle_refs(bundle) -> list[ImageRef]:
    """Every reference picture a bundle's cards and lore hold, and a simple
    chat's own."""
    return [
        *owners_refs(
            [*bundle.cast, *bundle.story.setup.set_aside],
            bundle.supporting.characters,
            bundle.lore,
        ),
        *bundle.story.reference_images,
    ]


def reference_files(bundle, root: Path | None = None) -> dict[str, bytes]:
    """The bytes of every reference picture in the bundle that is on disk."""
    found: dict[str, bytes] = {}
    for ref in bundle_refs(bundle):
        data = read_story_file(bundle.story.id, ref.file, root)
        if data is not None:
            found[ref.file] = data
    return found


def all_image_files(story_id: str, root: Path | None = None) -> dict[str, bytes]:
    """Everything under images/ and images.json, for an archive."""
    directory = story_dir(story_id, root)
    found: dict[str, bytes] = {}
    images = directory / IMAGES_DIR
    if images.is_dir():
        for path in sorted(images.rglob("*")):
            if path.is_file() and not path.name.endswith(".tmp"):
                found[path.relative_to(directory).as_posix()] = path.read_bytes()
    if (directory / IMAGES_FILE).is_file():
        found[IMAGES_FILE] = (directory / IMAGES_FILE).read_bytes()
    return found


def clean_story_images(story_id: str, root: Path | None = None) -> list[str]:
    """Strip every picture in the story's folder, in place; the pictures
    that can't be, by path. Those stay as they are, and are refused if
    anything tries to send them or put them in a scenario."""
    directory = story_dir(story_id, root)
    failed: list[str] = []
    images = directory / IMAGES_DIR
    if not images.is_dir():
        return failed
    for path in sorted(images.rglob("*")):
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        data = path.read_bytes()
        try:
            clean = strip_metadata(data)
        except MetadataError:
            failed.append(path.relative_to(directory).as_posix())
            continue
        if clean != data:
            write_bytes_atomic(path, clean)
    return failed


def copy_image_files(source_id: str, target_id: str, root: Path | None = None) -> None:
    source = story_dir(source_id, root)
    target = story_dir(target_id, root)
    if (source / IMAGES_DIR).is_dir():
        shutil.copytree(source / IMAGES_DIR, target / IMAGES_DIR, dirs_exist_ok=True)
    if (source / IMAGES_FILE).is_file():
        shutil.copy2(source / IMAGES_FILE, target / IMAGES_FILE)
