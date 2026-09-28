"""Where a story's pictures are kept: its folder, or memory.

Every picture read and write goes through a store: the generated pictures
and images.json, reference pictures, the background, and the log entries a
picture's job writes. A story on disk has `DiskPictures`, which is
`storage.images` and the story's API log as they always were. A chat kept in
memory only has `MemoryPictures`, which holds the same things in this process
and nowhere else (the author, Sept 2026: pictures in a memory chat, kept in
memory, with a way to export them).

A picture's job runs on its own thread and writes through the store while the
window reads it, so `MemoryPictures` takes a lock. Records are still added on
the GUI thread only (`add_images`), as images.json has always had one writer.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.image import GeneratedImage
from sealedlore.storage.images import (
    IMAGES_FILE,
    add_generated_images,
    all_image_files,
    is_safe_relative,
    load_generated_images,
    read_story_file,
    remove_generated_image,
    story_path,
    write_story_file,
)
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import append_api_log


class PictureStore(Protocol):
    def read(self, relative: str) -> bytes | None: ...
    def write(self, relative: str, data: bytes) -> None: ...
    def remove(self, relative: str) -> None: ...
    def images(self) -> list[GeneratedImage]: ...
    def add_images(self, records: Iterable[GeneratedImage]) -> None: ...
    def remove_image(self, image_id: str) -> None: ...
    def all_files(self) -> dict[str, bytes]: ...
    def alive(self) -> bool: ...
    def log(self, kind: str, body: dict[str, Any]) -> str: ...


def file_name(relative: str) -> str:
    """A picture's name, as a save dialog offers it."""
    return PurePosixPath(relative).name


class DiskPictures:
    """A story's own folder (`storage.images`), and its API log."""

    def __init__(self, story_id: str, root: Path | None = None) -> None:
        self.story_id = story_id
        self.root = root

    def read(self, relative: str) -> bytes | None:
        return read_story_file(self.story_id, relative, self.root)

    def write(self, relative: str, data: bytes) -> None:
        write_story_file(self.story_id, relative, data, self.root)

    def remove(self, relative: str) -> None:
        try:
            story_path(self.story_id, relative, self.root).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def images(self) -> list[GeneratedImage]:
        # Read each time: a job may have added one since.
        return load_generated_images(self.story_id, self.root)

    def add_images(self, records: Iterable[GeneratedImage]) -> None:
        add_generated_images(self.story_id, records, self.root)

    def remove_image(self, image_id: str) -> None:
        remove_generated_image(self.story_id, image_id, self.root)

    def all_files(self) -> dict[str, bytes]:
        return all_image_files(self.story_id, self.root)

    def alive(self) -> bool:
        # Deleted while a picture was drawn: don't bring its folder back.
        return (story_dir(self.story_id, self.root) / "story.json").is_file()

    def log(self, kind: str, body: dict[str, Any]) -> str:
        entry_id = new_id()
        entry = {"id": entry_id, "kind": kind, "at": utc_now_iso(), **body}
        append_api_log(self.story_id, entry, self.root)
        return entry_id


class MemoryPictures:
    """A memory-only chat's pictures, in this process only.

    `log` is the session's own (`StorySession._log`), which keeps a memory
    chat's log in memory. Once closed, with the chat, it holds nothing and
    takes nothing: a picture that finishes afterwards is dropped.
    """

    def __init__(
        self,
        log: Callable[[str, dict[str, Any]], str],
        files: Mapping[str, bytes] | None = None,
    ) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._files: dict[str, bytes] = {}
        self._images: list[GeneratedImage] = []
        self._closed = False
        for relative, data in (files or {}).items():
            if relative == IMAGES_FILE:
                self._images = [
                    GeneratedImage.model_validate(item) for item in json.loads(data or b"[]")
                ]
            elif is_safe_relative(relative):
                self._files[relative] = data

    @staticmethod
    def _checked(relative: str) -> str:
        if relative == IMAGES_FILE or not is_safe_relative(relative):
            raise ValueError(f"not a path inside the story's images: {relative!r}")
        return relative

    def read(self, relative: str) -> bytes | None:
        with self._lock:
            return self._files.get(relative)

    def write(self, relative: str, data: bytes) -> None:
        relative = self._checked(relative)
        with self._lock:
            if not self._closed:
                self._files[relative] = data

    def remove(self, relative: str) -> None:
        with self._lock:
            self._files.pop(relative, None)

    def images(self) -> list[GeneratedImage]:
        with self._lock:
            return list(self._images)

    def add_images(self, records: Iterable[GeneratedImage]) -> None:
        with self._lock:
            if not self._closed:
                self._images.extend(records)

    def remove_image(self, image_id: str) -> None:
        with self._lock:
            for image in self._images:
                if image.id == image_id:
                    self._files.pop(image.file, None)
            self._images = [image for image in self._images if image.id != image_id]

    def all_files(self) -> dict[str, bytes]:
        """As `storage.images.all_image_files` has them, for a backup."""
        with self._lock:
            found = dict(sorted(self._files.items()))
            if self._images:
                found[IMAGES_FILE] = json.dumps(
                    [image.model_dump() for image in self._images], indent=2
                ).encode("utf-8")
            return found

    def alive(self) -> bool:
        return not self._closed

    def log(self, kind: str, body: dict[str, Any]) -> str:
        return self._log(kind, body)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._files.clear()
            self._images.clear()
