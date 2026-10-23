"""The session's side of painted backdrops (engine/backdrops.py): when the
play view's area wants one, painting it in the background from the
window's render of the area, through the expander and the image model,
and keeping it as a map picture the view lays its sprites over.

The window renders (gui/backdrop_render.py: QImage only, on the painting's
thread) and trims the grey bands back off the painting; the engine draws
nothing. One painting at a time; a job that failed twice is left, as the
map's other pictures are. Under the map pictures' switch and the day's limit
(`map_pictures_on`, `map_pictures_left_today`), never asked for one by one:
the author's choice of Klein and the storybook look stands for every area
(Oct 23 2026)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from sealedlore.engine.area import Area
from sealedlore.engine.backdrops import (
    backdrop_job,
    backdrop_size,
    current_backdrop,
    expanded_prompt,
    expander_messages,
    failed_enough,
)
from sealedlore.engine.world_pictures import PictureJob, size_pixels
from sealedlore.models.world import MapPicture
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import ImageClient, data_url
from sealedlore.providers.wire import without_pictures
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES
from sealedlore.threads import BackgroundThread

# The expander's reply is one paragraph; room for a model that thinks first.
EXPANDER_MAX_TOKENS = 2500


class BackdropRuntime:
    _backdrop_thread: threading.Thread | None = None
    _backdrop_area: str | None = None
    _backdrop_client: ImageClient | None = None
    _backdrop_text: ChatProvider | None = None

    # --- what is wanted ---------------------------------------------------------------

    def backdrop_of(self, area: Area) -> MapPicture | None:
        """The backdrop the play view lays its sprites over for this area,
        painted of the area as it is now, or None."""
        return current_backdrop(self.world(), self._art_read(), area)

    def backdrop_painting(self) -> str | None:
        """The id of the area whose backdrop is being painted, or None."""
        thread = self._backdrop_thread
        return self._backdrop_area if thread is not None and thread.is_alive() else None

    def backdrop_wanted(self, area: Area) -> bool:
        """Whether the area should have a backdrop painted now: a plan of a
        place (never a window of open country), none current, map pictures
        on with room today, nothing painting, and not given up on."""
        if area.window or not self.map_first or self.in_private or self._map_closed:
            return False
        if not self.map_pictures_on or self.map_pictures_left_today() <= 0:
            return False
        if self.backdrop_painting() is not None:
            return False
        world, art = self.world(), self._art_read()
        if area.place_id not in world.places:
            return False
        if current_backdrop(world, art, area) is not None:
            return False
        return not failed_enough(art, area, world)

    # --- painting ----------------------------------------------------------------------

    def start_backdrop(
        self,
        area: Area,
        render: Callable[[int], bytes | None],
        trim: Callable[[bytes], bytes],
    ) -> bool:
        """Paint the area's backdrop in the background. `render(side)`
        gives the program's picture of the area padded onto a grey square
        `side` wide, as PNG; `trim` cuts the bands back off the painting.
        Both run on the painting's thread (the window's, QImage only)."""
        with self._starting():
            if not self.backdrop_wanted(area):
                return False
            media = self.config.images()
            if media is None:
                return False
            world, art = self.world(), self._art_read()
            job = backdrop_job(
                world,
                art,
                area,
                words=self._backdrop_words(area),
                style=self.texts["map.backdrop.style"],
                texts=self.texts,
            )
            client = ImageClient(media.base_url, media.api_key, api=media.api)
            detached = getattr(self.provider, "detached", None)
            text = detached() if callable(detached) else None
            self._backdrop_area = area.id
            self._backdrop_client = client
            self._backdrop_text = text
            thread = BackgroundThread(
                target=self._paint_backdrop,
                args=(job, area, client, text, render, trim),
                name="map-backdrop",
                daemon=True,
            )
            self._backdrop_thread = thread
            thread.start()
            return True

    def _backdrop_words(self, area: Area) -> str:
        """The place in words for the request: its name, the place's own
        words and the world's setting line."""
        world = self.world()
        place = world.places.get(area.place_id)
        parts = [area.title]
        if place is not None and place.description:
            parts.append(" ".join(place.description.split())[:400])
        setting = self.map_setting()
        if setting:
            parts.append(f"in {setting}")
        return ", ".join(p.rstrip(".") for p in parts if p)

    def _paint_backdrop(
        self,
        job: PictureJob,
        area: Area,
        client: ImageClient,
        text: ChatProvider | None,
        render: Callable[[int], bytes | None],
        trim: Callable[[bytes], bytes],
    ) -> None:
        try:
            model = self.config.map_picture_model
            self._doing("backdrop", f"painting the backdrop of {job.name}")
            size = backdrop_size(area)
            side = size_pixels(size)[0]
            picture = render(side)
            if picture is None:
                self._log("map_picture_error", {"job": job.key, "error": "nothing to paint from"})
                return
            prompt, expanded = self._expand_backdrop(job, picture, text)
            self._doing(
                "backdrop", f"painting the backdrop of {job.name}, by {self.model_name(model)}"
            )
            self._picture_one(
                replace(job, prompt=prompt),
                client,
                size,
                model=model,
                info=self._listed_image_model(model),
                plan=picture,
                extra={"enable_prompt_expansion": not expanded},
                finish=trim,
            )
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log(
                "map_picture_error", {"job": job.key, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
        finally:
            self._doing("backdrop", None)
            client.close()
            if text is not None:
                try:
                    text.close()
                except Exception:  # noqa: BLE001
                    pass
            self._backdrop_client = None
            self._backdrop_text = None
            callback = self.on_map_art
            if callback is not None:
                callback()

    def _expand_backdrop(
        self, job: PictureJob, picture: bytes, text: ChatProvider | None
    ) -> tuple[str, bool]:
        """The painter's instruction from the request: the prompt writer
        with BFL's upsampler instruction and the render in front of it (or
        the request alone for a writer that reads no pictures); the request
        as it is, with the endpoint's own expansion on, when there is no
        writer or it fails. (the prompt, whether a writer wrote it)."""
        if text is None:
            return job.prompt, False
        model = self.role_model("image_prompt")
        if not model:
            return job.prompt, False
        reads = self.reads_pictures(model)
        shown = data_url(picture, MEDIA_TYPES[picture_kind(picture)]) if reads else None
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("image_prompt", model),
            messages=expander_messages(job.prompt, shown, self.texts),
            params=self.side_params(model, max_tokens=EXPANDER_MAX_TOKENS),
        )
        log_ref = self._log(
            "backdrop_prompt_request",
            {"job": job.key, "payload": without_pictures(self.provider.build_payload(request))},
        )
        try:
            reply, completed = text.complete(request)
        except (ProviderError, ValueError) as exc:
            self._log(
                "backdrop_prompt_error", {"request_log_ref": log_ref, "error": str(exc)[:300]}
            )
            return job.prompt, False
        self._log(
            "backdrop_prompt_response",
            {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, model))
        prompt = expanded_prompt(reply)
        return (prompt, True) if prompt else (job.prompt, False)

    def stop_backdrop(self) -> None:
        for client in (self._backdrop_client, self._backdrop_text):
            if client is not None:
                try:
                    client.cancel()
                except Exception:  # noqa: BLE001 - stopping must never fail
                    pass

    # --- keeping the painting -----------------------------------------------------------

    @staticmethod
    def trimmed(data: bytes, trim: Callable[[bytes], bytes] | None) -> bytes:
        """The painting with its padding cut off and its hidden data
        stripped again (the trim re-encodes it)."""
        if trim is None:
            return data
        try:
            return strip_metadata(trim(data))
        except (MetadataError, ValueError):
            return data
