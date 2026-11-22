"""The session's side of {{user}} and {{char}} (engine/placeholders.py): whom
they name in this story now, and the lore a model or a keyword reads with the
names in. The story's own records keep the placeholders as written."""

from __future__ import annotations

from sealedlore.engine.placeholders import Names, names_for


class PlaceholdersRuntime:
    def placeholder_names(self) -> Names:
        """Whom {{user}} and {{char}} name now (`names_for`)."""
        return names_for(self.story, (*self.cast, *self.supporting))

    def settle_placeholder_user(self, playing: str | None = None) -> None:
        """When a story has begun with nobody named as its {{user}}: whom the
        author plays then, for good (they may switch later: {{user}} stays).
        Also for a story already under way with none (begun by an older
        version, or with nobody played): when it is opened, when a turn is
        sent (`playing`: whom that turn is played as) and around a change of
        who is played, so it is whom {{user}} has meant until now, not
        whoever is played next ("Marcus owes Marcus", and the cached system
        block rewritten at every switch)."""
        chosen = self.story.setup.placeholders
        held = self.story.held_character_id or playing
        if chosen.user_id is not None or held is None or self.story.chat:
            return
        # A held id no card answers to names nobody: left for one that does.
        if any(character.id == held for character in (*self.cast, *self.supporting)):
            chosen.user_id = held
