"""Make attacker text visible before the model sees it. Pure: no IO.

Invisible characters are an injection channel: the human reviewer sees nothing, the model reads
the hidden instruction. We delete them. Newlines and tabs are untouched, so line numbers hold.
"""

import re

# Raw string: the regex engine, not the source file, expands the \u escapes, so no invisible
# character ever sits in this file. Ranges: soft hyphen, Arabic letter mark, Mongolian vowel
# separator, zero-width and bidi marks (200B-200F), bidi embeddings/overrides (202A-202E), word
# joiner family (2060-2064), bidi isolates (2066-2069), BOM, and the Unicode "tag" block
# (E0000-E007F) that mirrors ASCII invisibly.
_INVISIBLE = re.compile(
    r"[­؜᠎​-‏‪-‮⁠-⁤⁦-⁩﻿"
    r"\U000e0000-\U000e007f]"
)


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text)


def count_invisible(text: str) -> int:
    return len(_INVISIBLE.findall(text))
