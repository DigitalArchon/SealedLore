"""A visual story's places painted (the author, Oct 26 2026: "use our
current method of generated art to make each area nicer"; design §19:
"painting a new area in the background, tiles first"). A mixin for
`StorySession`.

An area with no painting (a cell of the overmap made as it was entered, a
kit-made inside, anything the sample didn't ship painted) is painted in the
background the first time the player is in it: the window's render of the
area (its ground and the things that stay put), padded onto a grey square;
the prompt writer turning the archived request into the painter's
instruction with the render in front of it (BFL's upsampler instruction);
FLUX.3 Klein's edit model repainting the render keeping its layout; the
bands trimmed off and the painting kept as a map picture, which the play
view lays its sprites over (engine/backdrops.py). The tiles stand in until
it comes. One at a time; a job that failed twice is left. Off with
`Config.visual_paint` (the game's Game menu).

The window renders and trims (gui/backdrop_render.py, QImage only, on the
painting's thread); the engine draws nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sealedlore.engine.area import Area
from sealedlore.engine.backdrops import (
    BACKDROP,
    backdrop_size,
    backdrop_state,
    current_backdrop,
    expanded_prompt,
    expander_messages,
    failed_enough,
    job_key,
    paint_request,
)
from sealedlore.engine.image_variants import fitting_model
from sealedlore.engine.world_pictures import MAP_PICTURES_DIR, area_plan
from sealedlore.ids import new_id
from sealedlore.models.node import Usage
from sealedlore.models.world import MapPicture
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import (
    ImageClient,
    build_image_payload,
    data_url,
    loggable_payload,
)
from sealedlore.providers.wire import without_pictures
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES
from sealedlore.threads import BackgroundThread

# The weather the play view draws, as the painter is told it.
WEATHER_WORDS = {
    "snow": "in deep winter, everything under snow",
    "rain": "in the rain, the ground wet",
    "fog": "in fog",
    "dust": "in blowing dust",
}
# The expander's reply is one paragraph; room for a model that thinks first.
EXPANDER_MAX_TOKENS = 2500


class PaintRuntime:
    _paint_thread: threading.Thread | None = None
    _paint_area: str | None = None
    _paint_client: ImageClient | None = None
    _paint_text: ChatProvider | None = None
    # Called (on the painting's thread) once a painting is kept or given up.
    on_painted: Callable[[str], None] | None = None

    def painting(self) -> str | None:
        """The id of the area being painted, or None."""
        thread = self._paint_thread
        return self._paint_area if thread is not None and thread.is_alive() else None

    def paint_wanted(self, area: Area) -> bool:
        """Whether the area should be painted now: a visual story with
        painting on and pictures set up, an area of a place (never a window
        of open country) with no painting of it as it is, nothing painting,
        and not given up on."""
        if not self.story.visual or not self.config.visual_paint or self.in_private:
            return False
        if area.window or self.config.images() is None or self.painting() is not None:
            return False
        world, art = self.world(), self._art_read()
        if area.place_id not in world.places:
            return False
        if current_backdrop(world, art, area) is not None:
            return False
        return not failed_enough(art, area, world)

    def start_paint(
        self,
        area: Area,
        render: Callable[[int], bytes | None],
        trim: Callable[[bytes], bytes],
    ) -> bool:
        """Paint the area in the background. `render(side)` gives the
        program's picture of the area padded onto a grey square `side` wide
        (JPEG); `trim` cuts the bands back off the painting. Both run on the
        painting's thread."""
        if not self.paint_wanted(area):
            return False
        media = self.config.images()
        if media is None:
            return False
        world = self.world()
        place = world.places.get(area.place_id)
        words = ", ".join(
            p.rstrip(".")
            for p in (
                area.title,
                " ".join((place.what if place is not None else "").split())[:400],
                self._paint_setting(),
            )
            if p
        )
        life = ""
        if area.indoor and self.place_abandoned(area.id):
            life = self.texts["visual.paint.abandoned.indoor"]
        elif area.indoor and self.cell_lived_in(area.id):
            life = self.texts["visual.paint.lived_in.indoor"]
        elif not area.indoor and area.buildings:
            lived = "visual.paint.lived_in" if self.lit_buildings(area) else "visual.paint.deserted"
            life = self.texts[lived]
        request = paint_request(area, words=words, things=False, life=life, texts=self.texts)
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else None
        self._paint_area = area.id
        self._paint_client = client
        self._paint_text = text
        thread = BackgroundThread(
            target=self._paint,
            args=(area, request, client, text, render, trim),
            name="visual-paint",
            daemon=True,
        )
        self._paint_thread = thread
        thread.start()
        return True

    def lit_buildings(self, area: Area) -> set[str]:
        """The buildings of an outdoor area whose windows show a light: one
        with someone (not the player) standing inside it, or every one of a
        made cell that rolled someone living there. Every window was warm
        lit glass before (the author, Oct 28 2026: a farmstead "full of
        stuff, lights on and so on", and nobody there)."""
        if area.indoor or not area.buildings or not self.story.visual:
            return set()
        if self.cell_lived_in(area.id):
            return set(area.buildings)
        state = self.area_state()
        held = self.held_name()
        inside = {
            sp.area.split("#", 1)[0]
            for n, sp in state.spots.items()
            if n != held and n not in state.people_down
        }
        return {pid for pid in area.buildings if pid in inside}

    def _paint_setting(self) -> str:
        """The world's opening words, and the weather the play view draws
        (live, Brannock came back as dry dirt: the painter was never told it
        was winter), for the request."""
        words = " ".join((self.story.world_bible or "").split())
        first = words.split(". ")[0].strip().rstrip(".")
        weather = WEATHER_WORDS.get((self.visual_design().weather or "").strip().lower(), "")
        return ", ".join(p for p in (f"in {first}" if first else "", weather) if p)

    def _paint(
        self,
        area: Area,
        request: str,
        client: ImageClient,
        text: ChatProvider | None,
        render: Callable[[int], bytes | None],
        trim: Callable[[bytes], bytes],
    ) -> None:
        world, art = self.world(), self._art_read()
        state = backdrop_state(world, area)
        key = job_key(area, state)
        try:
            size = backdrop_size(area)
            side = int(size.split("x")[0])
            picture = render(side)
            if picture is None:
                self._log("map_picture_error", {"job": key, "error": "nothing to paint from"})
                return
            picture = strip_metadata(picture)
            shown = data_url(picture, MEDIA_TYPES[picture_kind(picture)])
            prompt, expanded = self._paint_prompt(key, request, shown, text)
            model = fitting_model(self.config.visual_paint_model, 1, self.image_listing())
            payload = build_image_payload(model, prompt, size, 1, [shown], api=client.api)
            if client.api == "nanogpt":
                payload["enable_prompt_expansion"] = not expanded
            log_ref = self._log(
                "map_picture_request",
                {"job": key, "payload": loggable_payload(payload, [f"the render of {area.title}"])},
            )
            try:
                reply = client.generate(payload)
                data = strip_metadata(trim(reply.images[0]))
                data = strip_metadata(data)
            except (ProviderError, MetadataError, IndexError, ValueError) as exc:
                art = self.world_art
                with self._lock():
                    art.failures[key] = art.failures.get(key, 0) + 1
                self._save_art(art)
                self._log(
                    "map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]}
                )
                return
            usage = (
                {"cost": reply.cost, "cost_reported": True}
                if reply.cost is not None
                else {"cost_reported": False}
            )
            relative = f"{MAP_PICTURES_DIR}/{new_id()}{picture_kind(data) or '.jpg'}"
            plan = area_plan(world, art, area.place_id)
            record = MapPicture(
                subject=BACKDROP,
                subject_id=area.id,
                name=area.title,
                state=state,
                things_painted=False,
                file=relative,
                prompt=prompt,
                model=model,
                made_at=datetime.now(UTC).isoformat(timespec="seconds"),
                plan=plan.made_at if plan is not None else "",
            )
            with self._closing:
                if self._closed or not self.pictures.alive():
                    return  # the story was left meanwhile: nothing written
                self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
                self.pictures.write(relative, data)
            art = self.world_art
            with self._lock():
                art.pictures.append(record)
            self._save_art(art)
            self.unreported_usage.append(
                Usage(
                    cost=float(usage.get("cost") or 0.0),
                    cost_reported=bool(usage.get("cost_reported")),
                )
            )
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log(
                "map_picture_error", {"job": key, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
        finally:
            client.close()
            if text is not None:
                try:
                    text.close()
                except Exception:  # noqa: BLE001
                    pass
            self._paint_client = None
            self._paint_text = None
            callback = self.on_painted
            if callback is not None:
                callback(area.id)

    def _paint_prompt(
        self, key: str, request: str, shown: str, text: ChatProvider | None
    ) -> tuple[str, bool]:
        """The painter's instruction from the request: the prompt writer
        with BFL's upsampler instruction and the render in front of it (the
        request alone for a writer that reads no pictures); the request as
        it is, with the endpoint's own expansion on, when there is no
        writer or it fails. (the prompt, whether a writer wrote it)."""
        if text is None:
            return request, False
        model = self.role_model("image_prompt")
        if not model:
            return request, False
        reads = self.reads_pictures(model) is not False
        chat = ChatRequest(
            model=model,
            extra_body=self.route_for("image_prompt", model),
            messages=expander_messages(request, shown if reads else None, self.texts),
            params=self.side_params(model, max_tokens=EXPANDER_MAX_TOKENS),
        )
        log_ref = self._log(
            "paint_prompt_request",
            {"job": key, "payload": without_pictures(self.provider.build_payload(chat))},
        )
        try:
            reply, completed = text.complete(chat)
        except (ProviderError, ValueError) as exc:
            self._log("paint_prompt_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return request, False
        self._log(
            "paint_prompt_response",
            {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
        )
        self.unreported_usage.append(self.priced(completed.usage, model))
        prompt = expanded_prompt(reply)
        return (prompt, True) if prompt else (request, False)

    def stop_paint(self) -> None:
        for client in (self._paint_client, self._paint_text):
            if client is not None:
                try:
                    client.cancel()
                except Exception:  # noqa: BLE001 - stopping must never fail
                    pass
