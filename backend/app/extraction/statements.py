# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Deterministic statement structure shared by the requirement and
obligation gates: anchor sentences, canonical keys, list structure and
modal wording.

The anchor sentence of a quote gives a claim its identity and, for a
requirement, its verbatim text. It is never semantic support: what a claim
means is decided from the quotes the model cited, nothing else.

Sentences in extracted PDF text cross line breaks: a line continues the
previous one when it starts with a lowercase letter (a wrapped sentence).
A line that starts with a capital or a list marker starts a new sentence,
so a heading line is never glued to the sentence below it.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

_WORD = re.compile(r"[^\W_]+")
# "1." "2)" "(3)" "a." "b)" "-" "•" "*" at the start of a line.
LIST_MARKER = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|\(?[a-z][.)]|[-•*])\s+")
_TERMINAL = re.compile(r"[.!?](?=\s|$)")


def words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True, slots=True)
class Line:
    start: int
    end: int  # exclusive, before the newline
    text: str

    @property
    def is_list_item(self) -> bool:
        return LIST_MARKER.match(self.text) is not None

    @property
    def continues(self) -> bool:
        """Whether this line continues the sentence of the line above."""
        stripped = self.text.lstrip()
        return bool(stripped) and stripped[0].islower()


def lines(text: str) -> list[Line]:
    out, start = [], 0
    for part in text.split("\n"):
        out.append(Line(start, start + len(part), part))
        start += len(part) + 1
    return out


def line_at(text: str, offset: int) -> int:
    """The index of the line containing ``offset``."""
    return text.count("\n", 0, offset)


def _body_start(line: Line) -> int:
    """Where a line's text starts after any list marker: the period of "1." is
    numbering, not the end of a sentence."""
    marker = LIST_MARKER.match(line.text)
    return line.start + (marker.end() if marker else 0)


def _sentence_start(text: str, offset: int) -> int:
    all_lines = lines(text)
    index = line_at(text, offset)
    while True:
        line = all_lines[index]
        begin = _body_start(line)
        ends = [m.end() for m in _TERMINAL.finditer(text, begin, min(offset, line.end))]
        if ends:
            return ends[-1]
        if index == 0 or not line.continues or line.is_list_item:
            return line.start
        if all_lines[index - 1].text.rstrip().endswith((".", "!", "?")):
            return line.start
        index -= 1


def _sentence_end(text: str, offset: int) -> int:
    all_lines = lines(text)
    index = line_at(text, offset)
    while True:
        line = all_lines[index]
        found = _TERMINAL.search(text, max(offset, _body_start(line)), line.end)
        if found:
            return found.end()
        following = all_lines[index + 1] if index + 1 < len(all_lines) else None
        if following is None or not following.continues or following.is_list_item:
            return line.end
        index += 1


def anchor_bounds(text: str, quote_start: int, quote_end: int) -> tuple[int, int]:
    """The sentence(s) of ``text`` covering the quote: exact code points."""
    return _sentence_start(text, quote_start), _sentence_end(text, max(quote_start, quote_end - 1))


def anchor_text(text: str, quote_start: int, quote_end: int) -> str:
    """The anchor sentence verbatim, whitespace collapsed, without a leading
    list marker: numbering is not part of a statement's identity."""
    start, end = anchor_bounds(text, quote_start, quote_end)
    sentence = " ".join(text[start:end].split())
    return LIST_MARKER.sub("", sentence, count=1)


def statement_key(sentence: str) -> str:
    """The canonical key of a statement: SHA-256 of its normal form (NFKC,
    case-folded words, list marker removed). Repeated wording, in another
    version or another source, has the same key."""
    normal = " ".join(words(LIST_MARKER.sub("", sentence, count=1)))
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()


def is_full_line(text: str, start: int, end: int) -> bool:
    """Whether [start, end) is one whole line of ``text``, up to whitespace."""
    line = lines(text)[line_at(text, start)]
    return (
        end <= line.end
        and not text[line.start : start].strip()
        and not text[end : line.end].strip()
    )


def governs_list_item(text: str, lead_in: tuple[int, int], item: tuple[int, int]) -> bool:
    """Whether a lead-in line introduces the list item the item quote starts in,
    within one span: the lead-in is a whole line above the item's line, the
    item's line is a list item, and every line between them is a list item
    or a wrapped continuation of one."""
    if not is_full_line(text, *lead_in):
        return False
    all_lines = lines(text)
    first, target = line_at(text, lead_in[0]), line_at(text, item[0])
    if target <= first or not all_lines[target].is_list_item:
        return False
    between = all_lines[first + 1 : target]
    if between and not between[0].is_list_item:
        return False
    return all(line.is_list_item or line.continues for line in between)


# --- modal wording ----------------------------------------------------------------------
# Deliberately small, closed lists: wording outside them proves nothing.

# A prohibition is mandatory: "must not", "may not", "No data may be ...".
_PROHIBITION = re.compile(
    r"\b(?:may\s+not|must\s+not|cannot|can\s*not|not\s+permitted|not\s+allowed)\b"
    r"|\bno\b[^.;:\n]{0,60}?\bmay\b",
    re.IGNORECASE,
)
MANDATORY = re.compile(
    r"\b(?:must|required|requires|requirement|need\s+to|needs\s+to|have\s+to|has\s+to"
    r"|shall|mandatory|compulsory)\b",
    re.IGNORECASE,
)
RECOMMENDED = re.compile(
    r"\b(?:should|recommended|recommend|encouraged|advised|advisable)\b", re.IGNORECASE
)
PERMISSIVE = re.compile(
    r"\b(?:may|can|could|might|optional|optionally)\b|\bif\s+you\s+wish\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Modal:
    mandatory: bool
    recommended: bool
    permissive: bool

    @property
    def kinds(self) -> int:
        return self.mandatory + self.recommended + self.permissive


def modal_wording(text: str) -> Modal:
    """Which kinds of modal wording ``text`` contains. A prohibition counts as
    mandatory, and its "may"/"can" is not permissive."""
    prohibitions = [m.span() for m in _PROHIBITION.finditer(text)]

    def outside(match: re.Match[str]) -> bool:
        return not any(a <= match.start() < b for a, b in prohibitions)

    return Modal(
        mandatory=bool(prohibitions) or bool(MANDATORY.search(text)),
        recommended=bool(RECOMMENDED.search(text)),
        permissive=any(outside(m) for m in PERMISSIVE.finditer(text)),
    )


def sentence_bounds(text: str, offset: int) -> tuple[int, int]:
    """The one sentence of ``text`` containing ``offset``."""
    return _sentence_start(text, offset), _sentence_end(text, offset)


def governing_lead_in(text: str, offset: int) -> tuple[int, int] | None:
    """The lead-in line that introduces the list item ``offset`` is in, within
    one span: walk up over list items and their wrapped lines to the first
    line that is neither; that whole line, if ``governs_list_item`` confirms
    it. None when ``offset`` is not in a list item or nothing introduces it."""
    all_lines = lines(text)
    index = line_at(text, offset)
    while index > 0 and not all_lines[index].is_list_item and all_lines[index].continues:
        index -= 1  # a wrapped line: its item starts above
    if not all_lines[index].is_list_item:
        return None
    above = index
    while above > 0 and (all_lines[above - 1].is_list_item or all_lines[above - 1].continues):
        above -= 1
    if above == 0:
        return None
    line = all_lines[above - 1]
    stripped = line.text.strip()
    if not stripped:
        return None
    start = line.start + line.text.index(stripped)
    bounds = (start, start + len(stripped))
    item = all_lines[index]
    return bounds if governs_list_item(text, bounds, (item.start, item.end)) else None


def first_modal_offset(text: str) -> int | None:
    """Where the first modal word of any kind is in ``text``, if any."""
    found = [
        m.start()
        for pattern in (_PROHIBITION, MANDATORY, RECOMMENDED, PERMISSIVE)
        for m in [pattern.search(text)]
        if m is not None
    ]
    return min(found) if found else None


def list_item_bounds(text: str, offset: int) -> tuple[int, int] | None:
    """The whole list item ``offset`` is in: its marker line and every wrapped
    line after it, ending before the next item or a line that starts a new
    sentence; trailing whitespace excluded. None outside a list item."""
    all_lines = lines(text)
    index = line_at(text, offset)
    while index > 0 and not all_lines[index].is_list_item and all_lines[index].continues:
        index -= 1
    if not all_lines[index].is_list_item:
        return None
    last = index
    while (
        last + 1 < len(all_lines)
        and all_lines[last + 1].continues
        and not all_lines[last + 1].is_list_item
    ):
        last += 1
    start = all_lines[index].start
    end = all_lines[last].start + len(all_lines[last].text.rstrip())
    return start, end


def list_items_in(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Every list item [start, end) touches, in source order: the item it
    starts in, then each item whose marker line it reaches."""
    found = []
    first = list_item_bounds(text, start)
    if first is not None:
        found.append(first)
    for line in lines(text)[line_at(text, start) + 1 :]:
        if line.start >= end:
            break
        if line.is_list_item:
            bounds = list_item_bounds(text, line.start)
            if bounds is not None and bounds not in found:
                found.append(bounds)
    return found


# What an assessed artefact must contain or do, as opposed to an action the
# student performs: an artefact noun as subject, then a modal and a content
# verb, within one clause. Deliberately narrow ("must be revised and
# resubmitted" is a process, so "be" is not a content verb).
ARTEFACT_CONTENT = re.compile(
    r"\b(?:specifications?|reports?|submissions?|essays?|dissertations?|theses|thesis"
    r"|proposals?|portfolios?|assignments?|coursework|answers?|appendix|abstracts?)\b"
    r"[^.;:\n]{0,40}?\b(?:must|should|shall|needs?\s+to|is\s+required\s+to|are\s+required\s+to)"
    r"\s+(?:not\s+)?(?:include|contain|address|discuss|cover|describe|present|demonstrate"
    r"|justify|explain|reflect|compare|state|cite|do)\b",
    re.IGNORECASE,
)


def describes_artefact_content(text: str, offset: int) -> bool:
    """Whether the statement at ``offset`` says what an assessed artefact must
    contain or do: its sentence has that form, or it is a list item whose
    governing lead-in does ("Your specification must include")."""
    start, end = sentence_bounds(text, offset)
    if ARTEFACT_CONTENT.search(text[start:end]):
        return True
    item = list_item_bounds(text, offset)
    lead = governing_lead_in(text, item[0]) if item is not None else None
    return lead is not None and ARTEFACT_CONTENT.search(text[lead[0] : lead[1]]) is not None
