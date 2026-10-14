"""Portraits of the characters: the session's side. A mixin for
`StorySession` (engine/portraits.py is the pure half).

The author asks (when a story starts, or on the Cast page), the prompt
writer writes a prompt for each character from their card, the author sees
and may change every one, and each is then drawn as an ordinary approved
picture (engine/images.py, on its own thread). A drawn portrait becomes its
character's first picture, the one the Cast page shows large
(`take_portraits`).

Only characters the models may see are offered: a plot-hidden character's
portrait would be the first a model heard of them. Never in a private scene
or a simple chat (a chat has no cast).
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.images import ImageRequest
from sealedlore.engine.portraits import (
    PORTRAIT_MAX_TOKENS,
    build_portrait_messages,
    fallback_prompt,
    parse_portrait_prompts,
    portrait_size,
)
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.providers.base import ChatRequest
from sealedlore.providers.images import ImageModelInfo
from sealedlore.storage.images import REFS_DIR

# The caption a drawn portrait is given on its card.
PORTRAIT_CAPTION = "portrait"


class PortraitRuntime:
    def portrait_candidates(self) -> list[Character]:
        """Everyone who may have a portrait now: the cast and supporting
        characters the models may see."""
        if self.story.chat:
            return []
        return [*self.visible_cast(), *self.visible_supporting()]

    def without_portraits(self) -> list[Character]:
        """Those of them with no picture at all."""
        return [c for c in self.portrait_candidates() if not c.reference_images]

    def write_portrait_prompts(
        self, characters: Sequence[Character], *, model: str | None = None
    ) -> tuple[dict[str, str], str | None]:
        """A prompt for each character, by id, and what went wrong if the
        writer didn't answer for all of them (their prompts are then built
        from their cards). One call, on the prompt writer's model."""
        self._refuse_in_private("Writing portrait prompts")
        texts = self.texts
        story = self.story
        allowed = {c.id for c in self.portrait_candidates()}
        characters = [c for c in characters if c.id in allowed]
        prompts: dict[str, str] = {}
        problem: str | None = None
        if characters:
            model = model or self.image_prompt_model
            request = ChatRequest(
                model=model,
                extra_body=self.route_for("image_prompt", model),
                messages=build_portrait_messages(
                    characters,
                    world_bible=story.world_bible or "",
                    title=story.title,
                    image_style=story.image_style,
                    texts=texts,
                ),
                params=self.side_params(model, max_tokens=PORTRAIT_MAX_TOKENS, temperature=0.7),
            )
            log_ref = self._log(
                "portrait_prompt_request", {"payload": self.provider.build_payload(request)}
            )
            try:
                text, completed = self.provider.complete(request)
            except Exception as exc:  # the author can still edit and draw
                self._log("portrait_prompt_error", {"request_log_ref": log_ref, "error": str(exc)})
                text, problem = "", f"The prompt writer failed ({exc})"
            else:
                self._log(
                    "portrait_prompt_response",
                    {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, model))
            if text:
                try:
                    prompts = parse_portrait_prompts(text, characters)
                except ValueError as exc:
                    problem = f"The prompt writer's reply couldn't be read ({exc})"
        missing = [c for c in characters if c.id not in prompts]
        if missing and problem is None:
            problem = "The prompt writer left out " + ", ".join(c.name for c in missing)
        for character in missing:
            prompts[character.id] = fallback_prompt(
                character, image_style=story.image_style, texts=texts
            )
        if problem:
            problem += ": their prompts are built from their cards."
        return prompts, problem

    def portrait_request(
        self,
        character: Character,
        prompt: str,
        *,
        model: str,
        info: ImageModelInfo | None,
    ) -> ImageRequest:
        """The request for one portrait, its prompt exactly as approved."""
        size = portrait_size(info)
        return ImageRequest(
            story_id=self.story.id,
            model=model,
            prompt=prompt,
            size=size,
            n=1,
            references=(),
            anchor_node_id=None,
            listed_price=info.price(size) if info is not None else None,
            portrait_of=character.id,
        )

    def take_portraits(self) -> list[str]:
        """Make each portrait drawn for this story and not yet taken its
        character's first picture; the names of those that changed. Each is
        taken once (`Story.portraits_taken`): one the author later removes
        from the card stays removed. A character gone since is skipped."""
        if self.story.chat:
            return []
        taken = set(self.story.portraits_taken)
        people = {c.id: c for c in [*self.cast, *self.supporting]}
        names: list[str] = []
        changed = False
        for image in self.generated_images():
            if image.portrait_of is None or image.id in taken:
                continue
            character = people.get(image.portrait_of)
            data = self.pictures.read(image.file)
            if character is not None and data is not None:
                ref_id = new_id()
                suffix = image.file[image.file.rfind(".") :] if "." in image.file else ".png"
                relative = f"{REFS_DIR}/{ref_id}{suffix}"
                # A copy: deleting the picture from Media leaves the card's.
                self.pictures.write(relative, data)
                character.reference_images.insert(
                    0, ImageRef(id=ref_id, file=relative, caption=PORTRAIT_CAPTION)
                )
                names.append(character.name)
            self.story.portraits_taken.append(image.id)
            taken.add(image.id)
            changed = True
        if changed:
            self.save()
        return names
