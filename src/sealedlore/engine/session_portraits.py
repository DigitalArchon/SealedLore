"""Pictures for the cards: the session's side. A mixin for `StorySession`
(engine/portraits.py is the pure half).

The author asks (when a story starts, on the Cast and Lore pages, or under
Story), the prompt writer writes a prompt for each card from what it says,
the author sees and may change every one, and each is then drawn as an
ordinary approved picture (engine/images.py, on its own thread). A drawn
picture goes onto its card (`take_card_pictures`): a new portrait first, a
picture drawn again, edited or of the character in the scene after the
others, so the author can keep the one they like and remove the rest.

Only cards the models may see are offered: a plot-hidden character's
portrait, or a lore entry still held back, would be the first a model heard
of them. Never in a private scene or a simple chat (a chat has no cards).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from sealedlore.engine.images import ImageRequest
from sealedlore.engine.portraits import (
    PORTRAIT_BATCH,
    PORTRAIT_MAX_TOKENS,
    Card,
    build_portrait_messages,
    card_size,
    edit_prompt,
    fallback_prompt,
    parse_portrait_prompts,
    where_it_begins,
)
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef, RefUse
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ChatProvider, ChatRequest
from sealedlore.providers.images import ImageModelInfo
from sealedlore.storage.images import REFS_DIR

# The caption a drawn portrait is given on its card.
PORTRAIT_CAPTION = "portrait"


class PortraitRuntime:
    def picture_cards(self) -> tuple[list[Character], list[LoreEntry]]:
        """Every card that may have a picture now: the characters and the
        enabled lore entries the models may see."""
        if self.story.chat:
            return [], []
        characters = [*self.visible_cast(), *self.visible_supporting()]
        return characters, [e for e in self.visible_lore() if e.enabled]

    def without_pictures(self) -> tuple[list[Character], list[LoreEntry]]:
        """Those of them with no picture at all."""
        characters, lore = self.picture_cards()
        return (
            [c for c in characters if not c.reference_images],
            [e for e in lore if not e.reference_images],
        )

    def card(self, card_id: str) -> Card | None:
        """A character or lore entry by id, any of them."""
        for card in (*self.cast, *self.supporting, *self.bundle.lore):
            if card.id == card_id:
                return card
        return None

    def write_card_prompts(
        self,
        characters: Sequence[Character] = (),
        lore: Sequence[LoreEntry] = (),
        *,
        model: str | None = None,
    ) -> tuple[dict[str, str], str | None]:
        """A prompt for each card, by id ("" for a lore entry with nothing
        to see), and what went wrong if the writer didn't answer for all of
        them (their prompts are then built from their cards). A few cards a
        call (`PORTRAIT_BATCH`), on the prompt writer's model, two calls at a
        time when the provider has a sibling (a Stop reaches both)."""
        self._refuse_in_private("Writing picture prompts")
        texts = self.texts
        story = self.story
        allowed_people, allowed_lore = self.picture_cards()
        people_ids = {c.id for c in allowed_people}
        lore_ids = {e.id for e in allowed_lore}
        characters = [c for c in characters if c.id in people_ids]
        lore = [e for e in lore if e.id in lore_ids]
        prompts: dict[str, str] = {}
        problems: list[str] = []
        if characters or lore:
            model = model or self.image_prompt_model
            params = self.side_params(model, max_tokens=PORTRAIT_MAX_TOKENS, temperature=0.7)
            batches = [
                (characters[i : i + PORTRAIT_BATCH], [])
                for i in range(0, len(characters), PORTRAIT_BATCH)
            ] + [([], lore[i : i + PORTRAIT_BATCH]) for i in range(0, len(lore), PORTRAIT_BATCH)]
            queue = list(batches)
            lock = threading.Lock()

            def write(people: list[Character], entries: list[LoreEntry], client) -> None:
                request = ChatRequest(
                    model=model,
                    extra_body=self.route_for("image_prompt", model),
                    messages=build_portrait_messages(
                        people,
                        world_bible=story.world_bible or "",
                        title=story.title,
                        image_style=story.image_style,
                        begins=where_it_begins(story),
                        lore=entries,
                        texts=texts,
                    ),
                    params=params,
                )
                log_ref = self._log(
                    "portrait_prompt_request", {"payload": client.build_payload(request)}
                )
                try:
                    text, completed = client.complete(request)
                except Exception as exc:  # the author can still edit and draw
                    self._log(
                        "portrait_prompt_error", {"request_log_ref": log_ref, "error": str(exc)}
                    )
                    with lock:
                        problems.append(f"The prompt writer failed ({exc})")
                    return
                self._log(
                    "portrait_prompt_response",
                    {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, model))
                try:
                    written = parse_portrait_prompts(text, people, entries) if text else {}
                except ValueError as exc:
                    with lock:
                        problems.append(f"The prompt writer's reply couldn't be read ({exc})")
                    return
                with lock:
                    prompts.update(written)

            def lane(client: ChatProvider) -> None:
                while True:
                    with lock:
                        if not queue:
                            return
                        people, entries = queue.pop(0)
                    write(people, entries, client)

            sibling = getattr(self.provider, "sibling", None)
            helper = None
            if callable(sibling) and len(batches) > 1:
                helper = threading.Thread(target=lane, args=(sibling(),), daemon=True)
                helper.start()
            lane(self.provider)
            if helper is not None:
                helper.join()
        problem = problems[0] if problems else None
        missing = [c for c in (*characters, *lore) if c.id not in prompts]
        if missing and problem is None:
            names = [c.name if isinstance(c, Character) else c.title for c in missing]
            problem = "The prompt writer left out " + ", ".join(names)
        for card in missing:
            prompts[card.id] = fallback_prompt(card, image_style=story.image_style, texts=texts)
        if problem:
            problem += ": their prompts are built from their cards."
        return prompts, problem

    def card_request(
        self,
        card: Card,
        prompt: str,
        *,
        model: str,
        info: ImageModelInfo | None,
        first: bool,
        caption: str = "",
        source: ImageRef | None = None,
    ) -> ImageRequest:
        """The request for one picture of a card, its prompt exactly as
        approved. `first`: it becomes the card's first picture (a new
        portrait); else it goes after the others. `source`: the picture
        an edit is of, sent with the prompt."""
        size = card_size(card, info)
        references: tuple[RefUse, ...] = ()
        if source is not None:
            person = isinstance(card, Character)
            references = (
                RefUse(
                    owner_kind="character" if person else "lore",
                    owner_id=card.id,
                    owner_name=card.name if person else card.title,
                    ref_id=source.id,
                    file=source.file,
                    caption=source.caption,
                ),
            )
        return ImageRequest(
            story_id=self.story.id,
            model=model,
            prompt=prompt,
            size=size,
            n=1,
            references=references,
            anchor_node_id=None,
            listed_price=info.price(size) if info is not None else None,
            card_id=card.id,
            card_first=first,
            card_caption=caption or (PORTRAIT_CAPTION if first else ""),
        )

    def card_edit_prompt(self, card: Card, change: str) -> str:
        """The prompt for an edit of a card's picture."""
        keep = "person" if isinstance(card, Character) else "thing"
        return edit_prompt(change, keep=keep, texts=self.texts)

    def scene_direction(self, character: Character) -> str:
        """The direction for a picture of one character as they are now."""
        return self.texts.fill("pictures.scene_of", name=character.name)

    def take_card_pictures(self) -> list[str]:
        """Put each picture drawn for this story's cards and not yet taken
        onto its card; the names of the cards that changed. Each is taken
        once (`Story.card_pictures_taken`): one the author later removes
        stays removed. A card gone since is skipped."""
        if self.story.chat:
            return []
        taken = set(self.story.card_pictures_taken)
        names: list[str] = []
        changed = False
        for image in self.generated_images():
            if image.card_id is None or image.id in taken:
                continue
            card = self.card(image.card_id)
            data = self.pictures.read(image.file)
            if card is not None and data is not None:
                ref_id = new_id()
                suffix = image.file[image.file.rfind(".") :] if "." in image.file else ".png"
                relative = f"{REFS_DIR}/{ref_id}{suffix}"
                # A copy: deleting the picture from Media leaves the card's.
                self.pictures.write(relative, data)
                ref = ImageRef(
                    id=ref_id,
                    file=relative,
                    caption=image.card_caption,
                    prompt=image.prompt,
                    model=image.model,
                    drawn_as=image.id,
                )
                if image.card_first:
                    card.reference_images.insert(0, ref)
                else:
                    card.reference_images.append(ref)
                names.append(card.name if isinstance(card, Character) else card.title)
            self.story.card_pictures_taken.append(image.id)
            taken.add(image.id)
            changed = True
        if changed:
            self.save()
        return names
