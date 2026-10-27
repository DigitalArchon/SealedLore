"""The people of a place drawn when the player comes to it (the author,
Oct 27 2026: "portraits should draw when the player goes into the relevant
region"; Kane stood in the Yard as one of the tile set's generic figures,
a woman's). A mixin for `StorySession` beside the painting runtime
(engine/session_paint.py), on the same switch (`Config.visual_paint`).

For each person standing in the player's area with a card but no walking
sprite of their own: a portrait, when the card has none (the portrait model,
the prompt from the card as the Start dialog's fallback writes it), kept on
the card under images/refs; then the sprite as an edit of the portrait
(the walker prompt the samples' walkers were drawn with), cut out of its
white ground by the window (`cut`, Qt's) and kept as a walker picture
under images/map. One run at a time, in the background; a person whose
pictures failed twice is left with the set's figure.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sealedlore.engine.area import Area
from sealedlore.engine.image_variants import fitting_model
from sealedlore.engine.portraits import fallback_prompt, portrait_size
from sealedlore.engine.world_pictures import MAP_PICTURES_DIR
from sealedlore.ids import new_id
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.node import Usage
from sealedlore.models.world import MapPicture
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import (
    ImageClient,
    build_image_payload,
    data_url,
    loggable_payload,
)
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES, REFS_DIR
from sealedlore.threads import BackgroundThread

PORTRAIT_CAPTION = "portrait"
WALKER_SIZE = "768x768"
# A person whose pictures failed this often is left as the set draws them.
PEOPLE_ART_TRIES = 2
DEFAULT_SPRITE_STYLE = "clean hand-painted pixel art with a crisp dark outline"


class PeopleArtRuntime:
    _people_thread: threading.Thread | None = None
    _people_client: ImageClient | None = None
    _people_drawing: tuple[str, ...] = ()
    # Called (on the drawing's thread) with the names drawn, or given up on.
    on_people_drawn: Callable[[list[str]], None] | None = None

    def people_art_running(self) -> bool:
        thread = self._people_thread
        return thread is not None and thread.is_alive()

    def people_art_wanted(self, area: Area) -> list[Character]:
        """Whose pictures the area is short of: the people standing in it
        with a card and no walking sprite of their own, not given up on."""
        if not self.story.visual or not self.config.visual_paint or self.in_private:
            return []
        if self.config.images() is None or self.people_art_running():
            return []
        state = self.area_state()
        art = self._art_read()
        out: list[Character] = []
        for name, spot in state.spots.items():
            if name == self.held_name() or spot.area != area.id or name in state.people_down:
                continue
            card = self.visual_card(name)
            if card is None or card.plot_hidden:
                continue
            if self.walker_file(name, art) is not None:
                continue
            if art.failures.get(f"walker:{card.id}", 0) >= PEOPLE_ART_TRIES:
                continue
            out.append(card)
        return out

    def start_people_art(
        self, cards: list[Character], cut: Callable[[bytes], bytes | None]
    ) -> bool:
        """Draw these people's pictures in the background: `cut` (the
        window's) takes a picture's bytes and gives the sprite cut out of
        its white ground as PNG, or None."""
        if not cards or self.people_art_running():
            return False
        media = self.config.images()
        if media is None:
            return False
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._people_client = client
        self._people_drawing = tuple(c.name for c in cards)
        thread = BackgroundThread(
            target=self._draw_people,
            args=(list(cards), client, cut),
            name="visual-people-art",
            daemon=True,
        )
        self._people_thread = thread
        thread.start()
        return True

    def _sprite_style(self) -> str:
        info, _read = self.tile_set()
        words = getattr(info, "words", None) or {}
        return words.get("style") or DEFAULT_SPRITE_STYLE

    def _draw_people(self, cards: list[Character], client: ImageClient, cut) -> None:
        drawn: list[str] = []
        try:
            for card in cards:
                if self._closed:
                    break
                try:
                    if self._draw_person(card, client, cut):
                        drawn.append(card.name)
                except Exception as exc:  # noqa: BLE001 - one failure never stops the rest
                    self._log(
                        "map_picture_error",
                        {"job": f"walker:{card.id}", "error": f"{type(exc).__name__}: {exc}"[:300]},
                    )
                    self._people_failed(card)
        finally:
            client.close()
            self._people_client = None
            self._people_drawing = ()
            callback = self.on_people_drawn
            if callback is not None:
                callback(drawn)

    def _people_failed(self, card: Character) -> None:
        art = self.world_art
        with self._lock():
            key = f"walker:{card.id}"
            art.failures[key] = art.failures.get(key, 0) + 1
        self._save_art(art)

    def _draw_person(self, card: Character, client: ImageClient, cut) -> bool:
        """A portrait for a card without one, then the walking sprite from
        it. True when the sprite was kept."""
        portrait = self._portrait_bytes(card)
        if portrait is None:
            portrait = self._draw_portrait(card, client)
        texts = self.texts
        style = self._sprite_style()
        if portrait is not None:
            prompt = texts.fill("visual.walker", style=style)
            shown = data_url(portrait, MEDIA_TYPES.get(picture_kind(portrait), "image/jpeg"))
            model = fitting_model(self.config.portrait_model, 1, self.image_listing())
            payload = build_image_payload(model, prompt, WALKER_SIZE, 1, [shown], api=client.api)
            names = [f"the portrait of {card.name}"]
        else:
            looks = " ".join((card.summary or card.full_description or card.name).split())[:300]
            world = " ".join((self.story.world_bible or "").split())[:120]
            prompt = texts.fill(
                "visual.person", what=looks, world=world or self.story.title, style=style
            )
            model = fitting_model(self.config.portrait_model, 0, self.image_listing())
            payload = build_image_payload(model, prompt, WALKER_SIZE, 1, [], api=client.api)
            names = []
        if client.api == "nanogpt":
            payload["enable_prompt_expansion"] = False
        log_ref = self._log(
            "map_picture_request",
            {"job": f"walker:{card.id}", "payload": loggable_payload(payload, names)},
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
        except (ProviderError, MetadataError, IndexError, ValueError) as exc:
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            self._people_failed(card)
            return False
        sprite = cut(data)
        if not sprite:
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": "nothing cut out"})
            self._people_failed(card)
            return False
        usage = (
            {"cost": reply.cost, "cost_reported": True}
            if reply.cost is not None
            else {"cost_reported": False}
        )
        relative = f"{MAP_PICTURES_DIR}/walker-{new_id()}.png"
        record = MapPicture(
            subject="walker",
            subject_id=card.name,
            name=card.name,
            file=relative,
            prompt=prompt,
            model=model,
            made_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        with self._closing:
            if self._closed or not self.pictures.alive():
                return False
            self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
            self.pictures.write(relative, sprite)
        art = self.world_art
        with self._lock():
            art.pictures.append(record)
        self._save_art(art)
        self.unreported_usage.append(
            Usage(
                cost=float(usage.get("cost") or 0.0), cost_reported=bool(usage.get("cost_reported"))
            )
        )
        return True

    def _portrait_bytes(self, card: Character) -> bytes | None:
        for ref in card.reference_images:
            data = self.pictures.read(ref.file)
            if data:
                return data
        return None

    def _draw_portrait(self, card: Character, client: ImageClient) -> bytes | None:
        """A portrait from the card's words (the Start dialog's fallback
        prompt), kept on the card first. None when it couldn't be drawn."""
        prompt = fallback_prompt(card, image_style=self.story.image_style, texts=self.texts)
        model = fitting_model(self.config.portrait_model, 0, self.image_listing())
        info = next((m for m in self.image_listing() if m.id == model), None)
        size = portrait_size(info)
        payload = build_image_payload(model, prompt, size, 1, [], api=client.api)
        if client.api == "nanogpt":
            payload["enable_prompt_expansion"] = False
        log_ref = self._log(
            "image_request",
            {"card_id": card.id, "payload": loggable_payload(payload, [])},
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
        except (ProviderError, MetadataError, IndexError, ValueError) as exc:
            self._log("image_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return None
        usage = (
            {"cost": reply.cost, "cost_reported": True}
            if reply.cost is not None
            else {"cost_reported": False}
        )
        suffix = picture_kind(data) or ".jpg"
        ref_id = new_id()
        relative = f"{REFS_DIR}/{ref_id}{suffix}"
        with self._closing:
            if self._closed or not self.pictures.alive():
                return None
            self._log("image_response", {"request_log_ref": log_ref, "usage": usage})
            self.pictures.write(relative, data)
            card.reference_images.insert(
                0,
                ImageRef(
                    id=ref_id, file=relative, caption=PORTRAIT_CAPTION, prompt=prompt, model=model
                ),
            )
            self.save()
        self.unreported_usage.append(
            Usage(
                cost=float(usage.get("cost") or 0.0), cost_reported=bool(usage.get("cost_reported"))
            )
        )
        return data

    def stop_people_art(self) -> None:
        client = self._people_client
        if client is not None:
            try:
                client.cancel()
            except Exception:  # noqa: BLE001 - stopping must never fail
                pass
