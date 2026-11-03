"""A story's picture style: one of a few ready-made styles, or the author's
own words (the author, Nov 3 2026: "Users should be able to choose a style
… gritty and realistic, or sci-fi clean and polished, or high
aesthetic/attractive characters, or anime style, etc.").

Pure. A ready-made style is `Story.picture_style` (its id) and its words
are a prompt text (`pictures.styles.<id>`), so they can be edited like any
other; the author's own words are `Story.image_style`, as before. Neither
set is the default, realistic throughout (the author: "Default style should
be the same throughout the story"): portraits and lore pictures in the
style they always had (`portraits.style`, `portraits.style.lore`), pictures
and videos of the story in `pictures.style.default`. A simple chat has no
style, and its writer chooses (`pictures.style.free`).

A picture or video of the story may be drawn in another style than the
story's (the author: "if they for example want a glamorous or anime style
picture of a scene"): `picture_words` takes the dialog's choice.

The style reaches whoever writes a picture's prompt (a card's picture, a
picture or video of the story); never the storyteller.
"""

from __future__ import annotations

from dataclasses import dataclass

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS, PromptTexts


@dataclass(frozen=True)
class PictureStyle:
    id: str
    label: str

    @property
    def key(self) -> str:
        return f"pictures.styles.{self.id}"

    @property
    def note_key(self) -> str | None:
        """Its note to whoever writes a prompt, for a style that has one."""
        key = f"{self.key}.note"
        return key if key in TEXTS else None


# In the order offered. Their words are in prompt_texts.py.
STYLES: tuple[PictureStyle, ...] = (
    PictureStyle("gritty", "Gritty and realistic"),
    PictureStyle("polished", "Clean and polished sci-fi"),
    PictureStyle("glamorous", "Glamorous"),
    PictureStyle("anime", "Anime"),
    PictureStyle("painted", "Painted fantasy"),
    PictureStyle("comic", "Comic book"),
    PictureStyle("watercolour", "Storybook watercolour"),
    PictureStyle("animated", "3D animated"),
)
BY_ID = {style.id: style for style in STYLES}

# What the choices are called where a style is chosen.
DEFAULT_LABEL = "Realistic (the default)"
OWN_LABEL = "Your own words…"


def style_words(picture_style: str, image_style: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The words a story's pictures are drawn in: a ready-made style's, else
    the author's own, else "" (the defaults). A ready-made style this
    version doesn't know (a newer one's) falls back to the author's words."""
    style = BY_ID.get(picture_style)
    words = texts[style.key] if style is not None else image_style
    return " ".join(words.split()).rstrip(".")


def story_style(story, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """`style_words` for a story."""
    return style_words(story.picture_style, story.image_style, texts)


def picture_words(
    story, texts: PromptTexts = DEFAULT_TEXTS, style: tuple[str, str] | None = None
) -> tuple[str, str]:
    """(the words, the note to the writer) a picture or video of the story is
    written in: `style` (a ready-made style's id, the author's own words:
    chosen for this picture alone), else the story's, else the default for a
    story ("" for a simple chat: its writer chooses)."""
    chosen, own = style if style is not None else (story.picture_style, story.image_style)
    words = style_words(chosen, own, texts)
    if not words and not story.chat:
        words = " ".join(texts["pictures.style.default"].split()).rstrip(".")
    return words, style_note(chosen, texts)


def style_note(picture_style: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """What whoever writes a prompt is told besides, for a ready-made style
    that has a note (its words alone lost to the description after them);
    else ""."""
    style = BY_ID.get(picture_style)
    key = style.note_key if style is not None else None
    return " ".join(texts[key].split()) if key is not None else ""


# The default's key: realistic throughout.
REALISTIC = "realistic"
# The style picture as a reference (`Story.style_picture`): its id among a
# request's references, and whose it is said to be.
STYLE_REF_ID = "style-picture"
STYLE_OWNER = "The story's style"


def style_picture_applies(story, style: tuple[str, str] | None = None) -> bool:
    """Whether the picture the story's own style was taken from goes with a
    picture in `style` (None: the story's): only for that style, its own
    words as they stand, never a ready-made one."""
    if not story.style_picture or story.picture_style or not story.image_style.strip():
        return False
    if style is None:
        return True
    chosen, own = style
    return not chosen and " ".join(own.split()) == " ".join(story.image_style.split())


def style_key(picture_style: str, image_style: str) -> str:
    """What a picture drawn in a style is tagged with (`ImageRef.style`): a
    ready-made style's id, the author's own words, or REALISTIC."""
    if picture_style in BY_ID:
        return picture_style
    return " ".join(image_style.split()) or REALISTIC


def style_label(picture_style: str, image_style: str) -> str:
    """How the choice reads: a ready-made style's name, the author's own
    words, or the default."""
    style = BY_ID.get(picture_style)
    if style is not None:
        return style.label
    return " ".join(image_style.split()) or DEFAULT_LABEL


def read_style(value: str) -> tuple[str, str]:
    """A style as written in a plot file's front matter: a ready-made one's
    name or id, whatever the case ("gritty and realistic", "Anime"), is
    (its id, ""); anything else is the author's own words ("", the words)."""
    words = " ".join(value.split())
    wanted = words.lower()
    for style in STYLES:
        if wanted in (style.id, style.label.lower()):
            return style.id, ""
    return "", words


def write_style(picture_style: str, image_style: str) -> str:
    """The front matter's value for a story's style: the inverse of
    `read_style`; "" for the default."""
    style = BY_ID.get(picture_style)
    if style is not None:
        return style.label.lower()
    return " ".join(image_style.split())
