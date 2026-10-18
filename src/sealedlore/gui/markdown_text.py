"""Markdown in the transcript, shown as it reads (the author, Oct 18 2026: a
simple chat showed **bold**, lists and headings as written, the marks and
all; story passages, Questions and chapters had the same problem).

A message whose text has nothing Markdown would change is shown exactly as
before, as plain text. Otherwise it is read as GitHub's Markdown by Qt's own
importer with HTML switched off, so a model's "<b>" or "<CLOSED>" stays text
and never becomes markup (the rule the plain labels kept: model prose is
never interpreted as HTML). What the importer makes is then held to the
transcript: the document's own font left out (the label's, the theme's and
Text size's, stand), pictures shown by their words (an image's address
could be any file on the computer), links as their words with the address
after them (nothing opens from a model's text), and a line break inside a
paragraph kept a line break (Markdown joins the lines, and Qt's importer
makes a hard break a new paragraph, spaced as one).

`shown_text` is the text as the reader sees it, character for character
with what the label holds, for Find (its offsets select in the label).
"""

from __future__ import annotations

import functools
import html
import re

from PySide6.QtGui import QTextDocument

# GitHub's dialect, less md4c's underline (MD_FLAG_UNDERLINE): Qt drew
# "_word_" underlined, where Markdown means it in italics.
_UNDERLINE = 0x4000
_FEATURES = QTextDocument.MarkdownFeature(
    (QTextDocument.MarkdownFeature.MarkdownDialectGitHub.value & ~_UNDERLINE)
    | QTextDocument.MarkdownFeature.MarkdownNoHTML.value
)
# Anything Markdown would show differently from the text as written.
_MARKS = re.compile(
    r"\*\*|__|~~|`"
    r"|(?<![\w*])\*(?=[^\s*])|(?<![\w_])_(?=[^\s_])"
    r"|^ {0,3}(?:#{1,6}\s|[-+*]\s|\d{1,9}[.)]\s|>|(?:-{3,}|\*{3,}|_{3,})\s*$|\|)"
    r"|\[[^\]\n]+\]\([^)\n]+\)"
    r"|\bhttps?://",
    re.MULTILINE,
)
# A line that begins a block of its own, never joined to the line before.
_BLOCK = re.compile(
    r"^ {0,3}(?:#{1,6}(?:\s|$)|[-+*]\s|\d{1,9}[.)]\s|>|```|~~~|\||(?:[-*_=]\s*){3,}$)"
)
# A line nothing may be joined to: a heading, a rule, a table's row.
_WHOLE = re.compile(r"^ {0,3}(?:#{1,6}(?:\s|$)|\||(?:[-*_]\s*){3,}$)|\|\s*$")
_FENCE = re.compile(r"^ {0,3}(?:```|~~~)")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_QUOTE = re.compile(r"^ {0,3}(?:> ?)+")
# A line break inside a paragraph, carried through the importer as this:
# punctuation, as a line's end is to emphasis ("_word_" at either end of a
# line is in italics; a private-use character joined "word__two" into one).
_BREAK = "\u2e18"
_BODY = re.compile(r"<body style=\"[^\"]*\">")
_IMAGE = re.compile(r"<img\b[^>]*>")
_ALT = re.compile(r"\balt=\"([^\"]*)\"")
_LINK = re.compile(r"<a href=\"([^\"]*)\">(.*?)</a>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_LINK_COLOUR = re.compile(r" color:#0000ff;")


def has_markdown(text: str) -> bool:
    """Whether Markdown would show the text any differently."""
    return _MARKS.search(text) is not None


def _with_breaks(text: str) -> str:
    """The text with each line break inside a paragraph marked by `_BREAK`
    at the end of its line: two lines of text, the second carrying on what
    the first says (not a list's next item, a heading, a table's row, a
    quote after plain text), a quote's lines counted by what they quote."""
    lines = text.split("\n")
    out: list[str] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        after = lines[i + 1] if i + 1 < len(lines) else ""
        if _QUOTE.match(line) and _QUOTE.match(after):
            line_says, after_says = _QUOTE.sub("", line), _QUOTE.sub("", after)
        else:
            line_says, after_says = line, after
        marked = (
            not fenced
            and line_says.strip()
            and after_says.strip()
            and not _FENCE.match(line)
            and not _WHOLE.search(line_says)
            and not _BLOCK.match(after_says)
            and not (_INDENTED.match(line_says) and _INDENTED.match(after_says))
        )
        out.append(line + _BREAK if marked else line)
    return "\n".join(out)


def _link(match: re.Match[str]) -> str:
    address, words = match.group(1), _LINK_COLOUR.sub("", match.group(2))
    plain = html.unescape(_TAG.sub("", words)).strip()
    if plain == html.unescape(address) or not address:
        return words
    return f"{words} ({address})"


def _image(match: re.Match[str]) -> str:
    alt = _ALT.search(match.group(0))
    return alt.group(1) if alt else ""


@functools.lru_cache(maxsize=2048)
def markdown_html(text: str) -> str | None:
    """The text as the transcript shows Markdown, as rich text for a label;
    None when Markdown would change nothing (show it plain)."""
    if not has_markdown(text):
        return None
    document = QTextDocument()
    document.setMarkdown(_with_breaks(text), _FEATURES)
    out = _BODY.sub("<body>", document.toHtml(), count=1)
    out = _IMAGE.sub(_image, out)
    out = _LINK.sub(_link, out)
    # The soft break after the mark is read as a space: the mark and it are
    # the line break.
    return re.sub(_BREAK + r"[ \n]?", "<br />", out)


@functools.lru_cache(maxsize=4096)
def shown_text(text: str) -> str:
    """The text as the reader sees it: the plain text of what the label
    shows, its offsets the label's own."""
    rich = markdown_html(text)
    if rich is None:
        return text
    document = QTextDocument()
    document.setHtml(rich)
    return document.toPlainText()
