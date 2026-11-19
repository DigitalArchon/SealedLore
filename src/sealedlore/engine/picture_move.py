"""A picture copied or moved from one character to another (the author, Nov
19 2026: several versions of one character, some aspects different and some
of the same pictures). Pure: the cards change, never a file. The copy is a
picture of its own on the other card, with the same file, labels, caption and
how it was drawn; a file two cards name is kept while either names it (Your
characters forgets only files no card names)."""

from __future__ import annotations

from sealedlore.engine.look_sheet import keep_order
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef


def give_picture(
    source: Character, target: Character, ref_id: str, *, move: bool
) -> ImageRef | None:
    """`source`'s picture `ref_id` onto `target`'s card, after their others
    (their first if they have none, so their portrait), and off `source`'s
    when `move`. None when there is no such picture, or `target` already has
    that file (nothing to add; a move still takes it off `source`)."""
    ref = next((r for r in source.reference_images if r.id == ref_id), None)
    if ref is None or source is target:
        return None
    # Their sheets' rows and columns kept where they are first.
    keep_order(source)
    keep_order(target)
    given = None
    if not any(r.file == ref.file for r in target.reference_images):
        given = ref.model_copy(deep=True, update={"id": new_id()})
        target.reference_images.append(given)
    if move:
        source.reference_images.remove(ref)
    return given
