"""What the picture actions are called, everywhere they are offered (a card,
its small pictures, Media, a story picture's menu), named for what is sent
to the image model (the author, Oct 18 2026: "the wording for the different
edit/new options are confusing and you can't clearly tell if a reference
picture is being used or not"). No imports: every picture widget uses it."""

# A new picture from the card's words: no picture is sent.
DRAW_LABELS = {
    "portrait": ("Draw a portrait…", "Draw a new portrait…"),
    "picture": ("Draw a picture…", "Draw a new picture…"),
}
# Another take from a picture's own prompt: the picture itself isn't sent
# (only any pictures it was itself drawn from, as before).
AGAIN_LABEL = "Same prompt again…"
# The picture sent with what to change.
EDIT_LABEL = "Change this picture…"
# Their portrait sent with them as they are now in the story.
SCENE_LABEL = "In a scene…"
# A picture, or a part of it, kept as a card's own (gui/picture_use.py).
USE_CHARACTER_LABEL = "Use as a character's picture…"
USE_PLACE_LABEL = "Use as a place's picture…"
# The captions over the two kinds on a card.
FROM_WORDS = "From words:"
SENDS_PICTURE = "Sends this picture:"
# What a dialog says goes with its prompt.
WORDS_ONLY = "Sent with the prompt: no picture, only the words."
