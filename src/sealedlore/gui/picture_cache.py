"""Pictures decoded and scaled once, and kept while they may be shown again.

The transcript is rebuilt after every turn, a picture landing, Answers or
Looks switched, and each rebuild read every picture it built from its file,
decoded it whole and scaled it smooth: sixteen 2048-pixel photos were 16
decodes and ~0.3 s a rebuild, offscreen. Kept here, a picture is decoded
again only when its file has been written since (edited, replaced, a name
used again) or it is wanted at another size or shape.

- A picture is kept by its file as it is now (the store's `stamp`: path,
  inode, size and modification time on disk, a write's own number in
  memory), its shape (`WITHIN`, `FIT`, `COVER`, as `ref_images` scales) and
  the size asked for. A store with no stamp is taken at its file's name
  alone, as the cast board always took them (a picture's name is its own
  for good, and `forget` drops one that comes back). The views show a
  pixmap as it is, whatever the screen's pixel ratio, so that is no part of
  the key.
- Bounded by bytes, the least recently used going first. A pixmap held here
  and on screen is one copy (QPixmap is shared), so what this costs beyond
  the pictures shown is at most the budget. A rebuild of more pictures than
  the budget holds reads them as before.
- Read through `ref_images` (`pixmap_of`: `image_codec`'s lock, EXIF
  orientation applied, so a photo is upright), whole and then scaled as the
  view would scale it, never by the decoder.

The transcript asks at once (`picture`): a picture's height is its
message's. Everything else that lists pictures (the cast board, the Media,
Cast and Lore pages, the picture dialogs) reads through `cast_board.Thumbs`,
a few a tick once it is up, so building it decodes nothing.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Collection, Hashable

from PySide6.QtGui import QPixmap

from sealedlore.gui.ref_images import cover, fit, pixmap_of, within
from sealedlore.storage.picture_store import PictureStore

# No larger than the box, never enlarged (the transcript); as large as fits
# it (a thumbnail); filling it, cut from the top (the cast board's cells).
WITHIN = "within"
FIT = "fit"
COVER = "cover"
_SHAPES: dict[str, Callable[[QPixmap, int, int], QPixmap]] = {
    WITHIN: within,
    FIT: fit,
    COVER: cover,
}

# The window's, for the transcript's pictures, faces and video posters: a
# picture the width of the column is 920 x 480 x 4 bytes, 1.8 MB.
SHOWN_BUDGET = 64 * 1024 * 1024


def stamp_of(store: PictureStore | None, file: str) -> Hashable:
    """What `file` in `store` is now: a picture read before it was written
    again is not this one."""
    stamp = getattr(store, "stamp", None)
    return stamp(file) if stamp is not None else ("name", file)


def size_of(pixmap: QPixmap) -> int:
    return pixmap.width() * pixmap.height() * max(1, pixmap.depth()) // 8


class PictureCache:
    """Pictures as shown, decoded once each (see the module's notes)."""

    def __init__(self, budget: int = SHOWN_BUDGET) -> None:
        self._budget = budget
        self._held: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._bytes = 0

    @property
    def held_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._held)

    def picture(
        self,
        store: PictureStore | None,
        file: str,
        width: int,
        height: int,
        shape: str = WITHIN,
    ) -> QPixmap:
        """`file` from `store` at `width` by `height` in `shape`, read now if
        it isn't kept; a null pixmap for no file or none that reads."""
        key = self._key(store, file, width, height, shape)
        pixmap = self._take(key)
        if pixmap is None:
            data = store.read(file) if store is not None and file else None
            pixmap = _SHAPES[shape](pixmap_of(data), width, height) if data else QPixmap()
            self._put(key, pixmap)
        return pixmap

    def peek(
        self,
        store: PictureStore | None,
        file: str,
        width: int,
        height: int,
        shape: str = WITHIN,
    ) -> QPixmap | None:
        """As `picture`, but None for one not read yet: nothing is read."""
        return self._take(self._key(store, file, width, height, shape))

    def forget(self, file: str | None = None) -> None:
        """Everything kept, or every size of `file`."""
        if file is None:
            self._held.clear()
            self._bytes = 0
            return
        for key in [key for key in self._held if key[0] == file]:
            self._bytes -= size_of(self._held.pop(key))

    def keep(self, files: Collection[str]) -> None:
        """Only the pictures of `files`, at every size: what a page lists
        now (another story's, or one taken off, no longer held)."""
        for key in [key for key in self._held if key[0] not in files]:
            self._bytes -= size_of(self._held.pop(key))

    def _key(
        self, store: PictureStore | None, file: str, width: int, height: int, shape: str
    ) -> tuple:
        return (file, stamp_of(store, file) if file else None, shape, width, height)

    def _take(self, key: tuple) -> QPixmap | None:
        pixmap = self._held.get(key)
        if pixmap is not None:
            self._held.move_to_end(key)
        return pixmap

    def _put(self, key: tuple, pixmap: QPixmap) -> None:
        size = size_of(pixmap)
        if size > self._budget:
            return  # shown, but not kept: it would push out everything else
        old = self._held.pop(key, None)
        if old is not None:
            self._bytes -= size_of(old)
        self._held[key] = pixmap
        self._bytes += size
        while self._bytes > self._budget:
            _key, dropped = self._held.popitem(last=False)
            self._bytes -= size_of(dropped)
