"""The session's side of {{user}} and {{char}} (engine/placeholders.py): whom
they name in this story now, and the lore a model or a keyword reads with the
names in. The story's own records keep the placeholders as written."""

from __future__ import annotations

from sealedlore.engine.placeholders import Names, names_for


class PlaceholdersRuntime:
    def placeholder_names(self) -> Names:
        """Whom {{user}} and {{char}} name now (`names_for`)."""
        return names_for(self.story, (*self.cast, *self.supporting))

    def settle_placeholder_user(self) -> None:
        """When a story begins with nobody named as its {{user}}: whom the
        author plays then, for good (they may switch later: {{user}} stays)."""
        chosen = self.story.setup.placeholders
        held = self.story.held_character_id
        if chosen.user_id is None and held is not None and not self.story.chat:
            chosen.user_id = held
