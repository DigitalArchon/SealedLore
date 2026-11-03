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

import base64
import threading
from collections.abc import Sequence

from sealedlore.engine.images import ImageRequest
from sealedlore.engine.picture_styles import (
    STYLE_OWNER,
    STYLE_REF_ID,
    style_note,
    style_picture_applies,
    style_words,
)
from sealedlore.engine.picture_use import from_picture_direction, keep_picture
from sealedlore.engine.portraits import (
    DEFAULT_FRAMING,
    PORTRAIT_BATCH,
    PORTRAIT_MAX_TOKENS,
    Card,
    build_portrait_messages,
    card_size,
    edit_prompt,
    fallback_prompt,
    parse_portrait_prompts,
    portrait_lead,
    portrait_style,
    restyle_prompt,
    where_it_begins,
    with_lead,
)
from sealedlore.ids import new_id
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef, RefUse
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ChatProvider, ChatRequest
from sealedlore.providers.images import ImageModelInfo
from sealedlore.providers.wire import without_pictures
from sealedlore.storage.images import REFS_DIR
from sealedlore.threads import BackgroundThread

# Room for a style's words from a model that may reason first.
STYLE_MAX_TOKENS = 1500

# The caption a drawn portrait is given on its card.
PORTRAIT_CAPTION = "portrait"


class PortraitRuntime:
    # The characters whose portrait the last `take_card_pictures` put first
    # (by id): the window offers a new map avatar from it.
    portraits_taken: list[str] = []

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
        framing: str = DEFAULT_FRAMING,
        style: tuple[str, str] | None = None,
        style_picture: bool | None = None,
    ) -> tuple[dict[str, str], str | None]:
        """A prompt for each card, by id ("" for a lore entry with nothing
        to see), and what went wrong if the writer didn't answer for all of
        them (their prompts are then built from their cards). A few cards a
        call (`PORTRAIT_BATCH`), on the prompt writer's model, two calls at a
        time when the provider has a sibling (a Stop reaches both). In the
        story's picture style, or `style` (a ready-made style's id, the
        author's own words: one chosen in the portraits dialog, not yet the
        story's); the portraits show `framing` of each character
        (engine/portraits.py FRAMINGS). `style_picture`: whether the style's
        own picture goes with them, as image 1 (None: as the story has it,
        `style_picture_applies`); their opening line then names it."""
        self._refuse_in_private("Writing picture prompts")
        texts = self.texts
        story = self.story
        chosen, own = style if style is not None else (story.picture_style, story.image_style)
        if style_picture is None:
            style_picture = self.style_reference((chosen, own)) is not None
        reference = 1 if style_picture else 0
        note = style_note(chosen, texts)
        style = style_words(chosen, own, texts)
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
                        image_style=style,
                        begins=where_it_begins(story),
                        lore=entries,
                        texts=texts,
                        framing=framing,
                        note=note,
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
                helper = BackgroundThread(target=lane, args=(sibling(),), daemon=True)
                helper.start()
            lane(self.provider)
            if helper is not None:
                helper.join()
        no_writing = texts["portraits.no_writing"]
        for card_id, prompt in prompts.items():
            lore_entry = card_id in lore_ids
            lead = portrait_lead(
                style, texts, lore=lore_entry, framing=framing, reference=reference
            )
            core = portrait_style(style, texts, lore=lore_entry, framing=framing)
            prompts[card_id] = with_lead(prompt, lead, no_writing, core=core)
        problem = problems[0] if problems else None
        missing = [c for c in (*characters, *lore) if c.id not in prompts]
        if missing and problem is None:
            names = [c.name if isinstance(c, Character) else c.title for c in missing]
            problem = "The prompt writer left out " + ", ".join(names)
        for card in missing:
            prompts[card.id] = fallback_prompt(
                card, image_style=style, texts=texts, framing=framing, reference=reference
            )
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
        references: Sequence[RefUse] = (),
        look: str = "",
        expression: str = "",
        framing: str = "",
        style: str = "",
        replaces: str = "",
        style_reference: RefUse | None = None,
    ) -> ImageRequest:
        """The request for one picture of a card, its prompt exactly as
        approved. `first`: it becomes the card's first picture (a new
        portrait); else it goes after the others. `source`: the picture
        an edit is of, sent with the prompt; else `references`, the pictures
        a picture drawn again was drawn from. `look` and `expression`: its
        labels on the card (engine/looks.py). `framing`: how much of a
        character it shows (engine/portraits.py FRAMINGS), which sets its
        size; blank, the source's when it is an edit, else a
        head-and-shoulders portrait's size, unlabelled. `style`: the style
        it is drawn in (engine/picture_styles.style_key), an edit's its
        source's when blank. `replaces`: the picture on the card it takes the
        place of (a restyle). `style_reference`: the picture the story's own
        style was taken from (`style_reference()`), sent after the rest, as
        the prompt names it; left out for a model that takes no picture."""
        person = isinstance(card, Character)
        references = tuple(references)
        if source is not None and not framing:
            # An edit shows as much of them as the picture it changes.
            framing = source.framing
        if source is not None and not style:
            style = source.style
        sending = 1 if source is not None else len(references)
        if style_reference is not None:
            sending += 1
        # The model's text or edit version, for what goes with the prompt.
        model, info = self.fitting_image_model(model, sending, info)
        size = card_size(card, info, framing or DEFAULT_FRAMING)
        if source is not None:
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
        if style_reference is not None and (info is None or info.accepts_images):
            references = (*references, style_reference)
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
            # A lore entry's first picture is no portrait (the Lore page
            # captioned Hellsville so, and the writer's catalogue named it so).
            card_caption=caption or (PORTRAIT_CAPTION if first and person else ""),
            card_look=look if person else "",
            card_expression=expression if person else "",
            card_framing=framing if person else "",
            card_style=style,
            card_replaces=replaces,
        )

    def describe_picture_style(self, data: bytes, mime: str, *, model: str | None = None) -> str:
        """A picture's visual style in words, for the story's own style
        (`pictures.style.from_picture`), by `model` (the prompt writer's by
        default), which must see pictures. Logged without the picture's
        bytes. Raises ProviderError, or ValueError for an empty reply."""
        self._refuse_in_private("Describing a picture's style")
        model = model or self.image_prompt_model
        url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("image_prompt", model),
            messages=[
                PromptMessage(
                    role="user",
                    parts=(ContentPart(text=self.texts["pictures.style.from_picture"], image=url),),
                )
            ],
            params=self.side_params(model, max_tokens=STYLE_MAX_TOKENS, temperature=0.3),
        )
        log_ref = self._log(
            "style_from_picture_request",
            {"payload": without_pictures(self.provider.build_payload(request))},
        )
        try:
            text, completed = self.provider.complete(request)
        except Exception as exc:
            self._log("style_from_picture_error", {"request_log_ref": log_ref, "error": str(exc)})
            raise
        self._log(
            "style_from_picture_response",
            {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, model))
        words = " ".join((text or "").split()).strip().strip('"“”').rstrip(".")
        if not words:
            raise ValueError("the model described no style")
        return words

    def style_reference(self, style: tuple[str, str] | None = None) -> RefUse | None:
        """The picture the story's own style was taken from, as a reference,
        when it goes with a picture in `style` (None: the story's;
        `style_picture_applies`) and is still there."""
        story = self.story
        if not style_picture_applies(story, style):
            return None
        if self.pictures.read(story.style_picture) is None:
            return None
        return RefUse(
            owner_kind="story",
            owner_id=story.id,
            owner_name=STYLE_OWNER,
            ref_id=STYLE_REF_ID,
            file=story.style_picture,
            caption="its style",
        )

    def set_style_picture(self, data: bytes, suffix: str) -> None:
        """The picture the story's own style was taken from (From a
        picture…), kept with the story (hidden data already removed); saved."""
        self._refuse_in_private("Changing the picture style")
        relative = f"{REFS_DIR}/{STYLE_REF_ID}-{new_id()}{suffix}"
        self.pictures.write(relative, data)
        self.story.style_picture = relative
        self.forget_review_undo()
        self.save()

    def clear_style_picture(self) -> None:
        """No picture with the story's style any more (its file stays)."""
        if self.story.style_picture:
            self.story.style_picture = ""
            self.forget_review_undo()
            self.save()

    def set_picture_style(self, picture_style: str, image_style: str) -> None:
        """The story's picture style (engine/picture_styles.py), as chosen in
        the portraits dialog; saved."""
        self._refuse_in_private("Changing the picture style")
        story = self.story
        if (story.picture_style, story.image_style) == (picture_style, image_style):
            return
        story.picture_style, story.image_style = picture_style, image_style
        self.forget_review_undo()
        self.save()

    def card_restyle_prompt(
        self, card: Card, style: str, change: str = "", *, reference: int = 0
    ) -> str:
        """The prompt for a card's picture redrawn in `style` (its words);
        `reference`, the style picture's number when it goes too."""
        keep = "person" if isinstance(card, Character) else "thing"
        return restyle_prompt(
            style, keep=keep, change=change, texts=self.texts, reference=reference
        )

    def card_edit_prompt(self, card: Card, change: str) -> str:
        """The prompt for an edit of a card's picture."""
        keep = "person" if isinstance(card, Character) else "thing"
        return edit_prompt(change, keep=keep, texts=self.texts)

    def scene_direction(self, character: Character) -> str:
        """The direction for a picture of one character as they are now."""
        return self.texts.fill("pictures.scene_of", name=character.name)

    def picture_from_direction(self, card: Card) -> str:
        """The direction for a picture drawn from a part of a picture of the
        story kept on the card (engine/picture_use.py): a portrait of the
        person shown, or the place shown with nobody in it."""
        return from_picture_direction(card, self.texts)

    def keep_card_picture(
        self, card: Card, data: bytes, suffix: str, *, caption: str, how: str
    ) -> ImageRef:
        """A picture the story made (or a part of it, already prepared: hidden
        data removed, scaled) kept on the card as `how` says
        (`picture_use.KEEPS`), written under images/refs and saved. The card
        moved on, as by hand: the review's undo is forgotten, as when a drawn
        picture lands."""
        ref_id = new_id()
        relative = f"{REFS_DIR}/{ref_id}{suffix}"
        self.pictures.write(relative, data)
        ref = ImageRef(id=ref_id, file=relative, caption=caption.strip())
        keep_picture(card, ref, how)
        self.forget_review_undo()
        self.save()
        return ref

    def portraits_landed(self) -> set[str]:
        """The cards with a portrait drawn and not yet taken onto them: it
        comes back while the worker owns the story, and is taken once the
        job is done (live, Oct 18 2026: the map drew those cards' avatars
        from their words in between, and again from the portraits)."""
        if self.story.chat:
            return set()
        taken = set(self.story.card_pictures_taken)
        return {
            image.card_id
            for image in self.generated_images()
            if image.card_id and image.card_first and image.id not in taken
        }

    def take_card_pictures(self) -> list[str]:
        """Put each picture drawn for this story's cards and not yet taken
        onto its card; the names of the cards that changed. Each is taken
        once (`Story.card_pictures_taken`): one the author later removes
        stays removed. A card gone since is skipped."""
        self.portraits_taken = []
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
                    look=image.card_look,
                    expression=image.card_expression,
                    framing=image.card_framing,
                    style=image.card_style,
                )
                pictures = card.reference_images
                old = next((r for r in pictures if r.id == image.card_replaces), None)
                if old is not None:
                    # A restyle takes the old one's place, labels and all; the
                    # old one stays at the end, unlabelled (the cast board's
                    # tray), to keep or remove.
                    at = pictures.index(old)
                    pictures.remove(old)
                    pictures.insert(at, ref)
                    old.look, old.expression = "", ""
                    pictures.append(old)
                    if at == 0 and isinstance(card, Character):
                        self.portraits_taken.append(card.id)
                elif image.card_first:
                    pictures.insert(0, ref)
                    if isinstance(card, Character):
                        self.portraits_taken.append(card.id)
                else:
                    pictures.append(ref)
                names.append(card.name if isinstance(card, Character) else card.title)
            self.story.card_pictures_taken.append(image.id)
            taken.add(image.id)
            changed = True
        if names:
            # The cards moved on, as by hand: an Undo review restored them
            # from before, and the picture, taken once, was gone for good.
            self.forget_review_undo()
        if changed:
            self.save()
        return names
