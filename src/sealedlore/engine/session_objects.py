"""Objects the author makes and places on the map (the author, Oct 18 2026:
"The user just says what object or objects they want and they are created.
Then we need an interface where the user can just place or remove any
object they want. The idea is to mainly use it at the start of a game to
setup an area, but optionally it could be used whenever"). A mixin for
`StorySession`.

Before it, the only way to have an armchair by the hearth was to write it
into a turn and hope the map read made one: in AfterDark the read took "the
armchair" for the room's heavy rocking chair and made nothing, while the
furnisher's own ten pieces were drawn.

- Made: each object is a thing of the tile set's, drawn in its style by the
  map's sprite model as the tile set editor draws one (`items/<slug>` in
  `WorldArt.tile_overrides`, counted against the day's map pictures), the
  price shown first; one the set already has a picture of is not drawn
  again. The names are kept (`WorldArt.objects`) for placing later.
- Placed and removed: a correction of the map (`correct_put_thing`,
  `correct_remove_thing`), so on the latest message, undoable, carried by
  the next turn, and told to the storyteller as the map's things are. Any
  thing on the play view can be removed, the plan's and the furnisher's
  included.
"""

from __future__ import annotations

from dataclasses import dataclass

from sealedlore.engine.objects import parse_objects
from sealedlore.engine.tile_sets import closest_item, item_slugs
from sealedlore.models.node import DIRECTOR_SPEAKER_ID


@dataclass(frozen=True)
class ObjectChoice:
    """One object that can be placed: its name, the slug its picture is
    under, whether the author made it (else it is the tile set's own),
    whether it has a picture of its own yet, and whether it is being drawn."""

    name: str
    slug: str
    mine: bool
    drawn: bool
    drawing: bool


@dataclass(frozen=True)
class ObjectsOffer:
    """What making these objects would cost: those to be drawn (the rest
    the set has already), the model, its listed price for one (None: the
    listing doesn't give it), and why they can't be drawn now (None: they
    can; they are still made, and stand in as the set's nearest thing)."""

    to_draw: list[str]
    model: str
    price_each: float | None
    problem: str | None


def object_role(name: str) -> str:
    """The tile set role an object's picture is drawn under."""
    slugs = item_slugs(name)
    return f"items/{slugs[-1] if slugs else 'object'}"


class ObjectsRuntime:
    # --- making ----------------------------------------------------------------

    @staticmethod
    def parse_objects(text: str) -> list[str]:
        return parse_objects(text)

    def _has_own_picture(self, name: str, read=None) -> bool:
        """The set or the story has a picture of exactly this thing (never
        the nearest of its kind, which only stands in)."""
        if read is None:
            _info, read = self.tile_set()
        return any(read(f"items/{s}") for s in item_slugs(name))

    def objects_offer(self, names: list[str]) -> ObjectsOffer:
        _info, read = self.tile_set()
        to_draw = [n for n in names if not self._has_own_picture(n, read)]
        role = object_role(to_draw[0] if to_draw else (names[0] if names else "object"))
        model, price, problem = self.tile_asset_offer(role, names[0] if names else "")
        if problem is not None and problem.startswith("It is being drawn"):
            problem = None  # that one joins the run; the others are new
        if not self.map_first:
            problem = "This story keeps no map (Story → Setup → About → Keep a map)."
        return ObjectsOffer(to_draw=to_draw, model=model, price_each=price, problem=problem)

    def create_objects(self, names: list[str]) -> list[str]:
        """Make these objects: kept for placing, and those with no picture
        of their own drawn, one after another, in the background. The names
        being drawn ([] when none can be: they stand in as the set's nearest
        thing until a picture is drawn for them)."""
        if not self.map_first or not names:
            return []
        art = self.world_art
        _info, read = self.tile_set()
        known = {n.lower() for n in art.objects}
        with self._lock():
            for name in names:
                if name.lower() not in known:
                    art.objects.append(name)
                    known.add(name.lower())
            self._save_art(art)
        offer = self.objects_offer(names)
        if offer.problem is not None or not offer.to_draw:
            return []
        wanted = [(object_role(n), n) for n in offer.to_draw]
        return offer.to_draw if self.redraw_tile_assets(wanted) else []

    def forget_object(self, name: str) -> bool:
        """Take an object the author made off the list (its picture stays:
        anything already placed still shows it)."""
        art = self.world_art
        kept = [n for n in art.objects if n.lower() != name.lower()]
        if len(kept) == len(art.objects):
            return False
        with self._lock():
            art.objects = kept
            self._save_art(art)
        return True

    def objects_drawing(self) -> set[str]:
        """The roles of objects being drawn now or waiting their turn."""
        thread = self._asset_thread
        if thread is None or not thread.is_alive():
            return set()
        roles = {role for role, _words in self._asset_queue}
        if self._asset_role:
            roles.add(self._asset_role)
        return {r for r in roles if r.startswith("items/")}

    def object_choices(self) -> list[ObjectChoice]:
        """Everything that can be placed: the author's objects first, newest
        first, then each thing the tile set has a picture of."""
        art = self.map_art_copy()
        _info, read = self.tile_set(art)
        drawing = self.objects_drawing()
        out: list[ObjectChoice] = []
        taken: set[str] = set()
        for name in reversed(art.objects):
            role = object_role(name)
            slug = role[len("items/") :]
            taken.update(item_slugs(name))
            out.append(
                ObjectChoice(
                    name=name,
                    slug=slug,
                    mine=True,
                    drawn=self._has_own_picture(name, read),
                    drawing=role in drawing,
                )
            )
        listing = getattr(read, "items", None)
        for slug in listing() if callable(listing) else []:
            if slug in taken:
                continue
            taken.add(slug)
            name = slug.replace("-", " ")
            out.append(ObjectChoice(name=name, slug=slug, mine=False, drawn=True, drawing=False))
        return out

    def object_picture(self, choice: ObjectChoice) -> bytes | None:
        """The picture an object is drawn with: its own, else the tile set's
        nearest thing it stands in as meanwhile (`closest_item`), else None."""
        _info, read = self.tile_set()
        for slug in (choice.slug, *item_slugs(choice.name)):
            data = read(f"items/{slug}")
            if data:
                return data
        listing = getattr(read, "items", None)
        found = closest_item(choice.name, listing()) if callable(listing) else None
        return read(f"items/{found}") if found else None

    # --- placing and removing ----------------------------------------------------

    def place_object(self, name: str, area_id: str, x: int, y: int) -> str | None:
        """One of the objects put on this tile (a correction: undoable, and
        carried by the next turn). Its id on the map, or None (the tile is
        taken or can't be stood on)."""
        return self.correct_put_thing(name, area_id, x, y)

    def remove_object(self, thing_id: str, area_id: str) -> bool:
        """Any thing on the play view taken off the map, the plan's and the
        furnisher's too."""
        return self.correct_remove_thing(thing_id, area_id)

    # --- the offer while an area is set up -----------------------------------------

    def _played(self) -> bool:
        """The author has written a turn of their own (the opening's
        Direction, the story's root, is not one)."""
        for index, node in enumerate(self.path()):
            if node.kind != "user":
                continue
            if index == 0 and node.speaker_id == DIRECTOR_SPEAKER_ID:
                continue
            return True
        return False

    def objects_offer_line(self) -> str | None:
        """The line the play view shows while a story's first area is set
        up: until the author's first turn, or until answered."""
        if self.story.objects_offered or not self.map_first or self.in_private:
            return None
        if not self.path() or self._played():
            return None
        if self.story.world_base is None:
            return None  # a map the story grows opens in Edit Mode (`map_setup_due`)
        return (
            "Want anything changed before you play? Edit the map: walls, doors and floors, "
            "buildings, and the things in each place."
        )

    def objects_offered(self) -> None:
        if not self.story.objects_offered:
            self.story.objects_offered = True
            self.save()
