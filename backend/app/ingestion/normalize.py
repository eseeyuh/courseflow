# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Conservative text normalisation, applied to one block at a time.

Changes only representation, never wording: line endings, invisible control
characters, Unicode composition (NFC), runs of spaces/tabs, spaces at line
edges and runs of blank lines (at most one is kept). NFKC is
deliberately not used: it rewrites visible characters (ligatures, full-width
forms, superscripts), so excerpts would stop matching the source. Because it
runs per block, it can never merge text across a page, slide or block
boundary.
"""

import re
import unicodedata

# Line terminators parsers emit besides "\n": CRLF/CR, vertical tab (a soft
# line break in python-pptx), form feed, NEL, and the Unicode line/paragraph
# separators.
_LINE_BREAK = re.compile(r"\r\n|[\r\v\f\x85  ]")
# Remaining C0 controls and DEL (tab and newline are kept). Invisible, never
# evidence; NUL in particular is rejected by PostgreSQL text columns.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Return ``text`` normalised; ``""`` when nothing visible remains.

    Idempotent: ``normalize_text(normalize_text(t)) == normalize_text(t)``.
    """
    text = _LINE_BREAK.sub("\n", text)
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    lines = (_SPACES.sub(" ", line).strip(" ") for line in text.split("\n"))
    text = "\n".join(lines)
    # At most one blank line inside a block.
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip("\n")
