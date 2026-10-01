"""Turns a model reply into what the card shows: prose as Pango markup, code
fenced off so the UI can tuck it behind a fold.

GTK-free, like runner.py, so it can be checked from a terminal:
`python3 render.py < reply.txt`.

The persona asks for plain talk, but the model still reaches for markdown --
and web search pulls it into a citation list no matter what. Rendering the
small useful subset (bold, bullets) and dropping the rest looks deliberate;
showing raw asterisks and URLs in a cream card looks like a bug.
"""

import re
from xml.sax.saxutils import escape

_FENCE = re.compile(r"^[ \t]*```[^\n]*\n?(.*?)(?:^[ \t]*```[ \t]*$\n?|\Z)", re.S | re.M)
_SOURCES_TAIL = re.compile(r"\n+\s*(?:\*\*)?(sources?|references?|citations?)(?:\*\*)?\s*:.*\Z", re.I | re.S)
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\s]+\)")
_BARE_URL = re.compile(r"\s*<?https?://\S+>?")
_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[-*•]\s+", re.M)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_INLINE_CODE = re.compile(r"`+([^`\n]+)`+")
_HRULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M)


def _prose_markup(text):
    text = _SOURCES_TAIL.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _HRULE.sub("", text)
    text = _TABLE_RULE.sub("", text)
    # Inline code is nearly always a name ("the `bluetoothctl` tool"); reading
    # it as plain words is what a person saying it aloud would do.
    text = _INLINE_CODE.sub(r"\1", text)
    text = escape(text)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BULLET.sub(lambda m: m.group(1) + "•  ", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    # Whatever survives is an unpaired marker, usually half a bold that is
    # still streaming in. Dropping it beats flashing asterisks.
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pieces(text):
    """[("prose", markup) | ("code", raw_text), ...] in reading order.

    An unterminated fence (still streaming) counts as code to the end, so a
    script being typed out never flashes up as prose first.
    """
    out = []
    pos = 0
    for match in _FENCE.finditer(text or ""):
        before = _prose_markup(text[pos:match.start()])
        if before:
            out.append(("prose", before))
        code = match.group(1).rstrip("\n")
        if code.strip():
            out.append(("code", code))
        pos = match.end()
    tail = _prose_markup((text or "")[pos:])
    if tail:
        out.append(("prose", tail))
    return out


if __name__ == "__main__":
    import sys

    for kind, body in pieces(sys.stdin.read()):
        print(f"--- {kind}\n{body}")
