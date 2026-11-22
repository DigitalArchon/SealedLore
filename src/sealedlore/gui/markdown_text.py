"""Markdown in the transcript, shown as it reads (the author, Oct 18 2026: a
simple chat showed **bold**, lists and headings as written, the marks and
all; story passages, Questions and chapters had the same problem).

A message whose text has nothing Markdown would change is shown exactly as
before, as plain text. Otherwise it is read as GitHub's Markdown by Qt's own
importer with HTML switched off, so a model's "<b>" or "<CLOSED>" stays text
and never becomes markup (the rule the plain labels kept: model prose is
never interpreted as HTML). What the importer makes is then held to the
transcript: the document's own font left out (the label's, the theme's and
Text size's, stand), pictures shown by their words, or by their address
when they have none (an image's address could be any file on the computer,
so it is never loaded), links as their words with the address
after them (nothing opens from a model's text), and a line break inside a
paragraph kept a line break (Markdown joins the lines, and Qt's importer
makes a hard break a new paragraph, spaced as one).

`shown_text` is the text as the reader sees it, character for character
with what the label holds, for Find (its offsets select in the label).

A message past what draws in good time is shown plain too, as written
(`_too_much`): Qt lays a table out again at every layout, at a third of a
millisecond a cell, and lists nested deep or thousands of items long cost
by the square of their number, so one message could hold the window for
minutes at each resize.

What is drawn is kept for the messages shown again and again (a rebuild
draws every message in the window), but never what is streaming
(`keep=False`: every prefix of a reply would be kept, pushing the real
messages out), and `forget()` lets go of all of it as a story or chat closes
and as a private scene ends: a memory-only chat's or scene's words must not
stay in the process after them.
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
# Anything Markdown would show differently from the text as written; a
# link, `[words](address)`, is looked for by hand (`_has_link`).
_MARKS = re.compile(
    r"\*\*|__|~~|`"
    r"|(?<![\w*])\*(?=[^\s*])|(?<![\w_])_(?=[^\s_])"
    r"|^ {0,3}(?:#{1,6}\s|[-+*]\s|\d{1,9}[.)]\s|>|(?:-{3,}|\*{3,}|_{3,})\s*$|\|)"
    r"|\bhttps?://",
    re.MULTILINE,
)
# The most of a message that is drawn as Markdown, each from what Qt took to
# lay it out (offscreen, Nov 2026); past any, it is shown as written. Far
# past what a model writes: a reply at the default limit is 16,000
# characters, a wide table a few hundred cells, a list six deep.
TEXT_MOST = 100_000  # characters: 0.25 s of prose with the usual marks
INLINE_MOST = 10_000  # of * _ ` ~ [ together: 0.2 s; a text of nothing else took 1 s
LINES_MOST = 4_000  # 0.1 s as headings or one-line paragraphs
TABLE_CELLS_MOST = 800  # 0.25 s at every layout; 10,000 took 4 s, at 190 MB
LIST_ITEMS_MOST = 2_000  # 0.1 s; 12,000 took 6 s
LIST_DEPTH_MOST = 30  # "- - - a": 500 deep 0.2 s, 2,000 deep 3 s, 10,000 deep 83 s
# The marks a line opens with: quotes and list items, one inside the other.
_OPENS = re.compile(r"(?:\s*(?:>|[-+*]\s|\d{1,9}[.)]\s))+")
_ITEM = re.compile(r"[-+*]\s|[.)]\s")
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
_SRC = re.compile(r"\bsrc=\"([^\"]*)\"")
# A picture with no words of its own: Qt's importer leaves nothing of it.
# Its start; the address and any title after it are read by hand (`_named`).
_BARE_IMAGE = re.compile(r"!\[\s*\]\(")
_ADDRESS_END = re.compile(r"[)\s]")
_SPACES = re.compile(r"\s+")
_LINK = re.compile(r"<a href=\"([^\"]*)\">(.*?)</a>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_LINK_COLOUR = re.compile(r" color:#0000ff;")


def has_markdown(text: str) -> bool:
    """Whether Markdown would show the text any differently."""
    return _MARKS.search(text) is not None or _has_link(text)


def _has_link(text: str) -> bool:
    """Whether the text holds `[words](address)` on one line: what
    `\\[[^\\]\\n]+\\]\\([^)\\n]+\\)` found, in one pass. The pattern read on
    from every `[` to the end of its line, so sixty thousand of them in a
    row took seven seconds before anything was drawn."""
    closes = newline = -2  # the next ")" and the next newline, once looked for
    before = 0  # the "](" before this one: its "]" ends any words before it
    at = text.find("](")
    while at >= 0:
        # Words: a "[" since the last "]" or newline, with something after it.
        edge = max(text.rfind("]", before, at), text.rfind("\n", before, at))
        words = text.find("[", edge + 1, max(at - 1, 0)) >= 0
        # An address: something, then ")" before the line ends.
        start = at + 2
        if closes != -1 and closes < start:
            closes = text.find(")", start)
        if closes == -1:
            return False  # none here, and none after
        if newline != -1 and newline < start:
            newline = text.find("\n", start)
        if words and closes > start and (newline == -1 or newline > closes):
            return True
        before = at
        at = text.find("](", at + 1)
    return False


def _too_much(text: str) -> bool:
    """Whether the text is past what is drawn as Markdown (the limits above),
    counted from the text as written, generously: a line with a `|` in it
    counts as a table's row, in a table or not."""
    if len(text) > TEXT_MOST or text.count("\n") >= LINES_MOST:
        return True
    if sum(text.count(mark) for mark in "*_`~[") > INLINE_MOST:
        return True
    cells = items = 0
    for line in text.split("\n"):
        if "|" in line:
            cells += line.count("|") + 1
        opens = _OPENS.match(line)
        if opens is not None:
            deep = len(_ITEM.findall(opens.group()))
            if deep > LIST_DEPTH_MOST:
                return True
            items += deep
    return cells > TABLE_CELLS_MOST or items > LIST_ITEMS_MOST


def _with_breaks(text: str) -> str:
    """The text with each line break inside a paragraph marked by `_BREAK`
    at the end of its line: two lines of text, the second carrying on what
    the first says (not a list's next item, a heading, a table's row, a
    quote after plain text), a quote's lines counted by what they quote;
    and a picture with no words given its address as them."""
    lines = text.split("\n")
    out: list[str] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            line = _pictures_named(line)
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


def _pictures_named(line: str) -> str:
    """A picture with no alt text given its address as its words, before
    the importer, which otherwise drops it whole; never inside a code span
    (the odd segments of a split on backticks), where it is text."""
    if "![" not in line:
        return line
    segments = line.split("`")
    for i in range(0, len(segments), 2):
        segments[i] = _named(segments[i])
    return "`".join(segments)


def _named(text: str) -> str:
    """Each `![](address)` or `![](address "title")` in `text` as
    `![(address)](address)`: what one pattern did,
    `!\\[\\s*\\]\\(([^)\\s]+)(?:\\s+"[^"]*")?\\)`, read here so that no stretch
    of the text is read twice. From every `![](` the pattern read on to the
    next space or `)`, and twelve thousand that never closed took ten
    seconds."""
    out: list[str] = []
    done = at = 0
    quotes = True  # whether a title could still close
    while (start := _BARE_IMAGE.search(text, at)) is not None:
        end = _ADDRESS_END.search(text, start.end())
        if end is None:
            break  # nothing closes this one, nor any after it
        at = end.start()
        if at == start.end():
            continue  # no address
        close = -1
        if text[at] == ")":
            close = at + 1
        elif quotes:
            title = _SPACES.match(text, at).end()  # type: ignore[union-attr]
            if text[title : title + 1] == '"':
                ends = text.find('"', title + 1)
                quotes = ends >= 0
                if text[ends + 1 : ends + 2] == ")" and quotes:
                    close = ends + 2
        if close < 0:
            # Another `![](` before this space has the same address's end,
            # and fails as this one did: on from the space.
            continue
        address = text[start.end() : at]
        out += [text[done : start.start()], f"![({address})]({address})"]
        done = at = close
    out.append(text[done:])
    return "".join(out)


def _link(match: re.Match[str]) -> str:
    address, words = match.group(1), _LINK_COLOUR.sub("", match.group(2))
    plain = html.unescape(_TAG.sub("", words)).strip()
    if plain == html.unescape(address) or not address:
        return words
    return f"{words} ({address})"


def _image(match: re.Match[str]) -> str:
    """A picture by its words: its alt text, else its address (as a link's
    is shown), so a message that is only a picture never shows as blank."""
    alt = _ALT.search(match.group(0))
    if alt and alt.group(1).strip():
        return alt.group(1)
    src = _SRC.search(match.group(0))
    return f"({src.group(1)})" if src and src.group(1) else ""


def markdown_html(text: str, *, keep: bool = True) -> str | None:
    """The text as the transcript shows Markdown, as rich text for a label;
    None when Markdown would change nothing (show it plain). `keep`: kept
    for the next time it is shown (not while it streams). Plain text is
    never kept: there is nothing to draw, and finding so is quick."""
    if len(text) > TEXT_MOST or not has_markdown(text):
        return None
    return _kept(text) if keep else _drawn(text)


@functools.lru_cache(maxsize=2048)
def _kept(text: str) -> str | None:
    return _drawn(text)


def _drawn(text: str) -> str | None:
    if _too_much(text) or not has_markdown(text):
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


def forget() -> None:
    """Let go of every message's text kept here (a story or chat closed, a
    private scene ended)."""
    _kept.cache_clear()
    shown_text.cache_clear()
