# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Live requirement preview: one real model call over an assessment's brief.

Runtime proof, not pipeline. The answer goes back to the caller and is never
persisted as a requirement, span, mapping or plan input; the offline demo
stays deterministic. Only the ``ModelCall`` row is written (observability:
standalone calls are kept, like search calls).

The model never writes evidence text. It reads the brief as numbered
sentences and answers with a requirement and the ID of the one sentence that
states it; the quote is that sentence, taken here from the stored brief, and
is checked against the brief text once more. Nothing here is presented as a
verified claim.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, create_model, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.model_calls import RecordedResult, call_structured_recorded
from app.ai.prompts import load_prompt
from app.ai.provider import AIProvider, ChatMessage
from app.db.models import EvidenceLink, Resource, ResourceVersion, SourceSpan
from app.domain.enums import EvidenceField, ModelRole, ResourceType
from app.extraction.claim_gate import quote_pattern
from app.extraction.schema import StrictModel

# v3: the model cites a numbered sentence and the quote is filled here (v1
# and v2 asked it to copy quotes, and it merged neighbouring requirements
# into one quote that was cut at the schema's length limit).
PROMPT_VERSION = "live-requirement-preview/v3"
# Pinned rather than read from MODEL_FAST: the UI names this model, so the
# label must match what runs. Verified in docs/experiments/model-catalog.md.
LIVE_MODEL = "nvidia/Nemotron-3_5-Lightning"
# A brief is a few pages; a longer text is refused, not truncated.
MAX_BRIEF_CHARS = 50_000
MAX_REQUIREMENTS = 6
# A line this short with no closing punctuation, between sentences, is a
# heading, label or page footer: a sentence of its own (as in BriefPreview).
HEADING_MAX_CHARS = 60

_TERMINAL = re.compile(r"[.!?][\"'”’)\]]*$")
# Sentence end: closing punctuation, then whitespace and a capital or digit.
_BOUNDARY = re.compile(r"[.!?][\"'”’)\]]*(?=\s+[\"“‘(]?[A-Z0-9])")
_ABBREVIATIONS = ("e.g.", "i.e.", "Dr.", "Prof.", "Mr.", "Mrs.", "Ms.", "cf.", "vs.")
_WHITESPACE = re.compile(r"\s+")


class LiveRequirement(StrictModel):
    """A requirement in the model's words and the ID of the one numbered
    sentence of the brief that states it. Never quote text."""

    requirement: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    source_sentence_id: str


class LiveRequirementPreview(StrictModel):
    requirements: list[LiveRequirement] = Field(max_length=MAX_REQUIREMENTS)

    @model_validator(mode="after")
    def _one_requirement_per_sentence(self) -> Self:
        ids = [r.source_sentence_id for r in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("each source_sentence_id may support only one requirement")
        return self


class BriefTooLongError(ValueError):
    """The brief's text is over ``MAX_BRIEF_CHARS``."""


class EmptyBriefError(ValueError):
    """The brief has no text to number."""


@dataclass(frozen=True, slots=True)
class Sentence:
    id: str  # S001, S002, …: valid for one request only
    start: int  # [start, end) in the brief's extracted text
    end: int
    text: str  # the source text with each line break or whitespace run as one space


def number_sentences(text: str) -> list[Sentence]:
    """The brief's sentences in order, deterministically: wrapped lines are
    joined, a heading or footer line is its own sentence, and a blank line
    (a page break) always ends one."""
    lines: list[tuple[int, int] | None] = []  # None: a blank line
    offset = 0
    for raw in text.split("\n"):
        body = raw.strip()
        start = offset + len(raw) - len(raw.lstrip())
        lines.append((start, start + len(body)) if body else None)
        offset += len(raw) + 1

    segments: list[tuple[int, int]] = []
    run: tuple[int, int] | None = None  # running text not yet split into sentences
    for i, line in enumerate(lines):
        if line is None:
            if run:
                segments += _split_run(text, *run)
            run = None
            continue
        start, end = line
        following = lines[i + 1] if i + 1 < len(lines) else None
        between_sentences = run is None or _TERMINAL.search(text[run[0] : run[1]]) is not None
        heading = (
            between_sentences
            and _TERMINAL.search(text[start:end]) is None
            and end - start <= HEADING_MAX_CHARS
            and (following is None or text[following[0]].isupper() or text[following[0]].isdigit())
        )
        if heading:
            if run:
                segments += _split_run(text, *run)
            segments.append((start, end))
            run = None
        else:
            run = (run[0] if run else start, end)
    if run:
        segments += _split_run(text, *run)
    return [
        Sentence(f"S{n:03d}", s, e, _WHITESPACE.sub(" ", text[s:e]))
        for n, (s, e) in enumerate(segments, start=1)
    ]


def _split_run(text: str, start: int, end: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for match in _BOUNDARY.finditer(text, start, end):
        if text[start : match.end()].endswith(_ABBREVIATIONS):
            continue
        out.append((start, match.end()))
        start = match.end() + len(text[match.end() : end]) - len(text[match.end() : end].lstrip())
    if start < end:
        out.append((start, end))
    return out


def preview_schema(sentences: Sequence[Sentence]) -> type[LiveRequirementPreview]:
    """The output schema for one brief: ``source_sentence_id`` is an enum of
    its sentence IDs, so constrained decoding cannot produce another."""
    ids = Literal[tuple(s.id for s in sentences)]  # type: ignore[valid-type]
    item = create_model("LiveRequirement", __base__=LiveRequirement, source_sentence_id=(ids, ...))
    return create_model(
        "LiveRequirementPreview",
        __base__=LiveRequirementPreview,
        requirements=(list[item], Field(max_length=MAX_REQUIREMENTS)),  # type: ignore[valid-type]
    )


@dataclass(frozen=True, slots=True)
class Brief:
    resource: Resource
    version: ResourceVersion


@dataclass(frozen=True, slots=True)
class PreviewLine:
    requirement: str  # the model's words
    sentence_id: str  # the model's citation
    quote: str  # that sentence, from the stored brief
    quote_found: bool  # the quote occurs in the brief (whitespace-insensitive)


@dataclass(frozen=True, slots=True)
class LivePreview:
    brief: Brief
    lines: list[PreviewLine]
    recorded: RecordedResult[LiveRequirementPreview]
    latency_ms: int


async def current_brief(session: AsyncSession, assessment_id: uuid.UUID) -> Brief | None:
    """The current version of the brief that states this assessment's title."""
    resource = await session.scalar(
        select(Resource)
        .join(ResourceVersion, ResourceVersion.resource_id == Resource.id)
        .join(SourceSpan, SourceSpan.resource_version_id == ResourceVersion.id)
        .join(EvidenceLink, EvidenceLink.source_span_id == SourceSpan.id)
        .where(
            EvidenceLink.assessment_id == assessment_id,
            EvidenceLink.field == EvidenceField.TITLE,
            Resource.type == ResourceType.BRIEF,
        )
        .order_by(Resource.source_uri)
        .limit(1)
    )
    if resource is None or resource.current_version_id is None:
        return None
    version = await session.get(ResourceVersion, resource.current_version_id)
    return None if version is None else Brief(resource, version)


def build_messages(
    assessment_title: str, brief: Brief, sentences: Sequence[Sentence]
) -> list[ChatMessage]:
    prompt = load_prompt(PROMPT_VERSION)
    user = prompt.user.substitute(
        nonce=secrets.token_hex(8),
        assessment_title=assessment_title,
        resource_title=brief.resource.title,
        material="\n".join(f"[{s.id}] {s.text}" for s in sentences),
    )
    return [ChatMessage("system", prompt.system), ChatMessage("user", user)]


def _found(quote: str, text: str) -> bool:
    pattern = quote_pattern(quote)
    return pattern is not None and pattern.search(text) is not None


async def preview_requirements(
    provider: AIProvider,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assessment_title: str,
    brief: Brief,
) -> LivePreview:
    """One live call; raises ``RecordedCallError`` when the model call fails."""
    text = brief.version.extracted_text
    if len(text) > MAX_BRIEF_CHARS:
        raise BriefTooLongError(f"brief has {len(text)} characters (limit {MAX_BRIEF_CHARS})")
    sentences = number_sentences(text)
    if not sentences:
        raise EmptyBriefError("brief has no text")
    started = time.perf_counter()
    recorded = await call_structured_recorded(
        provider,
        session_factory,
        messages=build_messages(assessment_title, brief, sentences),
        schema=preview_schema(sentences),
        role=ModelRole.FAST,
        prompt_version=PROMPT_VERSION,
        temperature=0,
        # Lightning's default reasoning can use up the whole output budget
        # (model-catalog.md, finding 7).
        thinking=False,
    )
    latency_ms = round((time.perf_counter() - started) * 1000)
    return LivePreview(brief, quoted(recorded.result.output, sentences, text), recorded, latency_ms)


def quoted(
    output: LiveRequirementPreview, sentences: Sequence[Sentence], text: str
) -> list[PreviewLine]:
    """Each requirement with the sentence it cites as its quote, verbatim
    from the brief; the quote check is an assertion that this held."""
    by_id = {s.id: s for s in sentences}
    lines = []
    for r in output.requirements:
        sentence = by_id.get(r.source_sentence_id)
        if sentence is None:  # the schema admits only these IDs
            raise ValueError(f"unknown source sentence {r.source_sentence_id!r}")
        found = _found(sentence.text, text)
        lines.append(PreviewLine(r.requirement, sentence.id, sentence.text, found))
    return lines
