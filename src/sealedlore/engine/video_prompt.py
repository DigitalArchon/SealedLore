"""Writing a video prompt from the story, for the author to approve. Pure.

Asked as a picture prompt is (`engine/image_prompt.py`): of the prompt
writer, on the story's cached prefix, so it has read what everyone looks
like. But a video is a few seconds of one continuous shot, not a frozen
moment, so the question says how long it lasts, whether it has sound, and
what picture it starts or ends on, and asks for movement and the camera.

Nothing here sends anything: the prompt goes to the author, who may change
every word before approving it, and what they approve is what is sent.
"""

from __future__ import annotations

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.image import RefUse

VIDEO_PROMPT_MAX_TOKENS = 3000
# The writer is asked for about this many words; the author can go longer.
VIDEO_PROMPT_WORDS = "60 to 150"


def frame_words(use: RefUse) -> str:
    """What a frame's picture is of, for the writer."""
    caption = f" ({use.caption})" if use.caption else ""
    return f"{use.owner_name}{caption}"


def seconds_words(duration: object) -> str:
    try:
        value = float(str(duration))
    except (TypeError, ValueError):
        return "a few seconds"
    return f"{value:g} second{'s' if value != 1 else ''}"


def build_video_question(
    *,
    direction: str,
    held_name: str | None,
    style: str,
    seconds: str,
    audio: bool | None,
    start: RefUse | None = None,
    end: RefUse | None = None,
    anchor_is_leaf: bool = True,
    chat: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The request for a video prompt, in place of an author's question.
    `audio`: None when the model has no say about sound."""
    asked = "asked" if direction.strip() else "unasked"
    if chat:
        subject = texts[f"video.subject.chat_{asked}"]
    else:
        moment = texts["video.moment." + ("last" if anchor_is_leaf else "earlier")]
        subject = texts.fill(f"video.subject.story_{asked}", moment=moment)
    kind = "chat" if chat else "story"
    parts = [
        "# VIDEO PROMPT",
        texts.fill(f"video.opening.{kind}", seconds=seconds, subject=subject),
        "",
        texts.fill("video.rules", seconds=seconds, words=VIDEO_PROMPT_WORDS),
    ]
    if audio is not None:
        parts.append(texts["video.audio.on" if audio else "video.audio.off"])
    if start is not None:
        parts.append(texts.fill("video.start", start=frame_words(start)))
    if end is not None:
        parts.append(texts.fill("video.end", end=frame_words(end)))
    if held_name:
        parts.append(texts.fill("pictures.held", held=held_name))
    parts.append(
        texts.fill("pictures.style", style=style.strip())
        if style.strip()
        else texts["video.style.free"]
    )
    if direction.strip():
        parts += ["", "# THE AUTHOR ASKS FOR", direction.strip()]
    parts += ["", texts["video.reply"]]
    return "\n".join(parts)


def parse_video_prompt(text: str) -> str:
    """The writer's prompt. Raises ValueError when there is none."""
    data = extract_json(text, dict, "video prompt")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("the reply held no prompt")
    return prompt.strip()
