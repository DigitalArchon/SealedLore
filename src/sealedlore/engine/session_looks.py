"""Looks on the session's side: which picture each character is shown with
as the story stands, and the author's choice of one. A mixin for
`StorySession`; the pure part is engine/looks.py.

A choice goes on the message at the end of the story, as a scene correction
does, so it lasts from there and follows branches and takes. In a private
scene it goes only on the scene's own messages, and counts only while the
scene is open: the looks are read from the path as the models see it, the
open scene's messages in and an ended one's out. On the public message
before it, or carried on by the next choice made after it, a look chosen in
private would outlast the scene and say something of what happened there.
"""

from __future__ import annotations

from sealedlore.engine.looks import looks_at


class LooksRuntime:
    def _looks_path(self) -> list:
        """The path looks are read from: an ended private scene's messages
        left out, the open one's in."""
        span = self.open_span
        return self.model_nodes(self.full_path(), include_span=span.id if span else None)

    def looks_now(self) -> dict[str, str]:
        """Who is shown with which picture at the end of the story
        (character id → picture id)."""
        return looks_at(self._looks_path())

    def show_look(self, character_id: str, ref_id: str | None) -> None:
        """Show a character with one of their pictures from here on; None
        puts them back to their portrait. ValueError, in words, when there is
        nowhere to keep it yet."""
        path = self.full_path()
        if not path:
            raise ValueError("A look can be chosen once the story has begun.")
        span = self.open_span
        if span is not None and path[-1].meta.private_span != span.id:
            raise ValueError(
                "In a private scene a look is chosen once the scene has a message of its own: "
                "on the story's message before it, it would outlast the scene."
            )
        character = next((c for c in (*self.cast, *self.supporting) if c.id == character_id), None)
        if character is None:
            raise ValueError("That character isn't in the story.")
        if ref_id is not None and all(ref.id != ref_id for ref in character.reference_images):
            raise ValueError(f"That picture isn't one of {character.name}'s any more.")
        looks = looks_at(self._looks_path())
        if ref_id is None:
            looks.pop(character_id, None)
        else:
            looks[character_id] = ref_id
        path[-1].meta.looks = looks
        self.save()
