"""Pictures of the story: the session's side. A mixin for `StorySession`.

Writing the prompt is a question on the story's cached prefix, like an aside
or the plot's skipped-over events (`assemble_question(rendered=...)`), so
the writer has read everything the storyteller has. It runs while the window
is otherwise idle and changes no story state; only the API log hears of it.

Sending the approved prompt is not the session's business: that job runs on
its own thread for as long as it takes (`engine.images.run_image_request`)
while the author plays on.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.image_prompt import (
    IMAGE_PROMPT_MAX_TOKENS,
    ImagePromptDraft,
    RefChoice,
    build_image_question,
    parse_image_prompt,
    ref_choices,
)
from sealedlore.engine.prompt import assemble_chat, assemble_question
from sealedlore.engine.scene_state import log_on_path, scene_at
from sealedlore.engine.video_prompt import (
    build_video_question,
    parse_video_prompt,
    seconds_words,
)
from sealedlore.models.generation import GenerationParams
from sealedlore.models.image import GeneratedImage, ImageRef, RefUse
from sealedlore.models.node import Node
from sealedlore.models.video import GeneratedVideo
from sealedlore.providers.base import ChatRequest
from sealedlore.providers.images import ImageClient
from sealedlore.providers.tee import is_tee
from sealedlore.providers.videos import VideoClient
from sealedlore.storage.images import clean_story_images
from sealedlore.tree import index_nodes, path_to

# The most reference pictures offered when the model's own limit is unknown.
DEFAULT_MAX_REFS = 4


class ImageRuntime:
    @property
    def image_prompt_model(self) -> str:
        return self.config.image_prompt_model or self.model

    def image_path(self, anchor_id: str | None) -> list[Node]:
        """The path a picture is of: the active one, or up to a passage on it.

        In a private scene, the scene's own messages are in it: the prompt is
        written by the private model, which may see them."""
        span = self.open_span
        if span is not None:
            nodes = self.full_path() if anchor_id is None else path_to(self.nodes, anchor_id)
            return self.model_nodes(nodes, include_span=span.id)
        if anchor_id is None or anchor_id == self.story.active_leaf_id:
            return self.path()
        return self._path_to(anchor_id)

    # --- what can be pictured ------------------------------------------------

    def image_ref_choices(self, anchor_id: str | None = None) -> list[RefChoice]:
        """Every reference picture on a card or lore entry the models may see.

        A character the plot hasn't brought in yet is never offered: their
        picture would be the first a model heard of them. Pictures added from
        disk (`Story.reference_images`) belong to no card and come last. A
        simple chat has no cards: it offers those and every picture it made.
        """
        added = [
            (_use("story", self.story.id, ref.caption or "Added picture", ref), False)
            for ref in self.story.reference_images
        ]
        if self.story.chat:
            uses = added
            for image in self.generated_images():
                shown = ImageRef(id=image.id, file=image.file, caption=image.prompt[:80])
                uses.append((_use("picture", image.id, "A picture from this chat", shown), False))
            return ref_choices(uses)
        path = self.image_path(anchor_id)
        scene = scene_at(path, self.story.scene) if path else self.story.scene
        present_ids = set(scene.present_character_ids)
        present_names = {name.lower() for name in scene.present_others}
        uses: list[tuple[RefUse, bool]] = []
        for character in [*self.visible_cast(path), *self.visible_supporting()]:
            present = character.id in present_ids or character.name.lower() in present_names
            for ref in character.reference_images:
                uses.append((_use("character", character.id, character.name, ref), present))
        for entry in self.visible_lore():
            if entry.enabled:
                for ref in entry.reference_images:
                    uses.append((_use("lore", entry.id, entry.title, ref), False))
        # Those in the scene first: they are the likeliest to be in the picture.
        uses.sort(key=lambda item: not item[1])
        return ref_choices(uses + added)

    def write_image_prompt(
        self,
        direction: str,
        *,
        anchor_id: str | None = None,
        max_refs: int = DEFAULT_MAX_REFS,
        fixed: Sequence[RefUse] | None = None,
        model: str | None = None,
    ) -> ImagePromptDraft:
        """Ask for an image prompt. Raises ProviderError or ValueError.

        `fixed` is the author's own choice of references, in order, when they
        ask for the prompt to be written again around it. `model` is the
        writer the author picked in the dialog (None: the setting, else the
        story model); a private scene's own model writes regardless.
        """
        self._tee_guard()
        # In a private scene the private model writes the prompt, from the
        # scene; the author is warned that the picture itself is not private.
        choices = self.image_ref_choices(anchor_id)
        fixed_choices = None
        if fixed is not None:
            by_ref = {choice.use.ref_id: choice for choice in choices}
            fixed_choices = [
                by_ref.get(use.ref_id) or RefChoice(key=f"F{i}", use=use, present=False)
                for i, use in enumerate(fixed, start=1)
            ]
        held = self._character(self.story.held_character_id)
        chat = self.story.chat
        question = build_image_question(
            direction=direction,
            held_name=held.name if held is not None else None,
            style=self.story.image_style,
            choices=choices,
            max_refs=max_refs,
            fixed=fixed_choices,
            anchor_is_leaf=anchor_id is None or anchor_id == self.story.active_leaf_id,
            chat=chat,
            texts=self.texts,
        )
        text = self._ask_prompt_writer(
            question, direction=direction, anchor_id=anchor_id, model=model, kind="image_prompt"
        )
        draft = parse_image_prompt(text, fixed_choices or choices, max_refs=max_refs)
        if fixed is not None:
            # The author's choice stands whatever the writer listed.
            return ImagePromptDraft(prompt=draft.prompt, references=list(fixed), dropped=[])
        return draft

    def _ask_prompt_writer(
        self,
        question: str,
        *,
        direction: str,
        anchor_id: str | None,
        model: str | None,
        kind: str,
    ) -> str:
        """Ask the prompt writer `question` on the story's cached prefix (or
        a chat's), as the storyteller reads it; its reply's text. `kind`
        names the log entries ("image_prompt", "video_prompt")."""
        span = self.open_span
        path = self.image_path(anchor_id)
        chat = self.story.chat
        split = self.split(path)
        if chat:
            # The chat as its model reads it, the request last. A TEE chat's
            # prompt is written by its own model: the chat never leaves it.
            prompt = assemble_chat(
                story=self.story,
                history_nodes=split.verbatim,
                tail=question,
                summaries=split.summaries,
                nodes_by_id=index_nodes(self.nodes),
                estimator=self.estimator,
                options=self.assembly_options(),
                # The chat's tail is for its replies, not the picture's prompt.
                with_chat_tail=False,
            )
            if is_tee(self.model):
                model = self.model
        else:
            lore = (
                self.last_retrieval.entries
                if span is None and self.last_retrieval is not None and path == self.path()
                else self.retrieve(
                    self._question_as_turn(direction or (path[-1].content if path else "")),
                    path,
                    allow_network=False,
                ).entries
            )
            prompt = assemble_question(
                story=self.story,
                cast=self.visible_cast(path),
                history_nodes=split.verbatim,
                question="",
                summaries=split.summaries,
                lore=lore,
                standing_lore=self.lore_layout().standing,
                supporting=self.supporting_for(path[-1].content if path else "", path),
                on_file=self.visible_supporting(),
                scene_log=log_on_path(self.story.scene_log, path),
                estimator=self.estimator,
                options=self.private_options() if span is not None else self.assembly_options(),
                rendered=question,
            )
        if span is not None:
            if self.private_provider is None:
                raise ValueError("the private scene has no model to write the prompt")
            provider, model = self.private_provider, span.model
        else:
            provider, model = self.provider, model or self.image_prompt_model
        request = ChatRequest(
            model=model,
            # None in a private scene: its prompt is the private model's.
            extra_body=self.route_for("image_prompt", model),
            messages=prompt.messages,
            params=GenerationParams(max_tokens=IMAGE_PROMPT_MAX_TOKENS, temperature=0.7),
            use_cache_control=self._cache_control_for(model) and model == self.model,
            cache_ttl=self.config.cache_ttl,
        )
        body = {
            "node_id": path[-1].id if path else None,
            "direction": direction,
            "payload": provider.build_payload(request),
        }
        log_ref = (
            self._log_turn("request", {kind: True, **body}, span)
            if span is not None
            else self._log(f"{kind}_request", body)
        )
        text, completed = provider.complete(request)
        answer = {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage}
        if span is not None:
            self._log_turn("response", {kind: True, **answer}, span)
        else:
            self._log(f"{kind}_response", answer)
        self.unreported_usage.append(self.priced(completed.usage, model))
        return text

    def write_video_prompt(
        self,
        direction: str,
        *,
        anchor_id: str | None = None,
        duration: object = None,
        audio: bool | None = None,
        start: RefUse | None = None,
        end: RefUse | None = None,
        model: str | None = None,
    ) -> str:
        """Ask for a video prompt: one continuous shot of `duration` seconds,
        with sound or not (`audio`, None when the model has no say), from a
        start frame and to an end frame when there are. Raises
        ProviderError or ValueError. Written by the same prompt writer as a
        picture's (`model`, else the setting, else the story model); a
        private scene's own model writes regardless."""
        self._tee_guard()
        held = self._character(self.story.held_character_id)
        question = build_video_question(
            direction=direction,
            held_name=held.name if held is not None else None,
            style=self.story.image_style,
            seconds=seconds_words(duration),
            audio=audio,
            start=start,
            end=end,
            anchor_is_leaf=anchor_id is None or anchor_id == self.story.active_leaf_id,
            chat=self.story.chat,
            texts=self.texts,
        )
        text = self._ask_prompt_writer(
            question, direction=direction, anchor_id=anchor_id, model=model, kind="video_prompt"
        )
        return parse_video_prompt(text)

    def video_frame_choices(self, anchor_id: str | None = None) -> list[RefUse]:
        """Pictures a video may start or end on: the story's reference
        pictures a model may see (as for a picture), every picture it made,
        and the last frame of every video it made (to carry a scene on)."""
        uses = [choice.use for choice in self.image_ref_choices(anchor_id)]
        if not self.story.chat:
            for image in self.generated_images():
                shown = ImageRef(id=image.id, file=image.file, caption=image.prompt[:80])
                uses.append(_use("picture", image.id, "A picture from this story", shown))
        for video in self.generated_videos():
            if video.status == "done" and video.last_frame:
                shown = ImageRef(
                    id=f"{video.id}-last", file=video.last_frame, caption="its last frame"
                )
                uses.append(_use("picture", video.id, "An earlier video", shown))
        return uses

    def video_client(self) -> VideoClient | None:
        """A client for one video job, on the video endpoint and key. None
        while video has no key of its own (Settings → Video)."""
        media = self.config.videos()
        if media is None:
            return None
        return VideoClient(media.base_url, media.api_key, api=media.api)

    def generated_videos(self) -> list[GeneratedVideo]:
        """Read afresh each time: a job may have changed one since."""
        return self.pictures.videos()

    def image_client(self) -> ImageClient | None:
        """A client for one job, on the picture endpoint (Settings → Images;
        by default the chat's). The caller closes it."""
        media = self.config.images()
        if media is None:
            return None
        return ImageClient(media.base_url, media.api_key, api=media.api)

    def clean_stored_images(self) -> None:
        """Once per story: remove the hidden data from pictures saved before
        it was removed on the way in. A picture that can't be cleaned is left
        as it is and named in a notice; sending it is refused."""
        if self.story.images_cleaned or self.memory_only:
            return
        failed = clean_story_images(self.story.id, self.root)
        self.story.images_cleaned = True
        self.save()
        if failed:
            count = f"{len(failed)} picture{'s' if len(failed) != 1 else ''}"
            self.bundle.notices.append(
                f"{count} in this story couldn't have hidden data removed and won't be "
                f"sent anywhere: {', '.join(failed)}"
            )

    # --- pictures made ---------------------------------------------------------

    def generated_images(self) -> list[GeneratedImage]:
        """Read afresh each time: a job may have added one since."""
        return self.pictures.images()


def _use(kind, owner_id: str, name: str, ref) -> RefUse:
    return RefUse(
        owner_kind=kind,
        owner_id=owner_id,
        owner_name=name,
        ref_id=ref.id,
        file=ref.file,
        caption=ref.caption,
    )
