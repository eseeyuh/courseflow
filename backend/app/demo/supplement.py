# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The supplementary Northbridge courses (``supplement.yaml``) in the demo.

They go through the same workflow as the two main courses: import through
``DemoConnector``, indexing, assessment, requirement and obligation
extraction under the production gates, material mapping over production
hybrid retrieval, dependencies, conflicts, validation and the plan. Two
things differ, both deliberately:

- their extraction and mapping answers come from ``AuthoredProvider``:
  what each brief states (``supplement_content.BRIEFS``), written by hand,
  not by a model. It quotes the material it is given exactly, so the gates
  verify every claim as they would a model's, and its calls are recorded
  under the model name ``fixture:authored-supplement/v1``, never as a
  model's output;
- their vectors are a separate committed snapshot pair, pinned to the
  supplement's own hash (``supplement_subject``), so the frozen golden
  dataset and its snapshots stay exactly as they are.

Recording those vectors is the only paid step, made once, by evaluation
tooling (``app.evaluation.northbridge.supplement_record``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import cache
from typing import Any

from pydantic import BaseModel

from app.ai.embeddings import EmbeddingInput, EmbeddingResult, EmbeddingSpace
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.connectors.demo import SUPPLEMENT_FILE, DemoSupplement, load_supplement
from app.demo import recordings
from app.demo.supplement_content import BRIEFS, Brief, Requirement
from app.domain.enums import ModelRole
from app.evaluation.northbridge.labels import CORPUS_DIR, REPO_ROOT
from app.evaluation.retrieval.replay import ReplayEmbeddingProvider, load_replay_provider
from app.evaluation.retrieval.rules import DATASET_HASH_SCHEME
from app.evaluation.retrieval.snapshot import ExpectedInputs, SnapshotSubject
from app.extraction.gate import canonical_key
from app.extraction.obligations import ObligationExtraction
from app.extraction.requirements import RequirementExtraction
from app.extraction.schema import AssessmentExtraction
from app.mapping.selection import MappingSelection
from app.retrieval.chunking import SPAN_PACK_V1

DOCUMENT_EMBEDDINGS = recordings.SUPPLEMENT_DOCUMENT_EMBEDDINGS
QUERY_EMBEDDINGS = recordings.SUPPLEMENT_QUERY_EMBEDDINGS
AUTHORED_MODEL = "fixture:authored-supplement/v1"


@cache
def supplement() -> DemoSupplement:
    return load_supplement(CORPUS_DIR)


def supplement_refs() -> frozenset[str]:
    return frozenset(c.ref for c in supplement().courses)


def supplement_subject() -> SnapshotSubject:
    """The supplement's identity: its manifest and every file it lists."""
    from app.evaluation.retrieval.dataset import dataset_sha256

    base = CORPUS_DIR.relative_to(REPO_ROOT).as_posix()
    paths = [f"{base}/{SUPPLEMENT_FILE}"] + [
        f"{base}/{v.file}" for c in supplement().courses for r in c.resources for v in r.versions
    ]
    return SnapshotSubject(
        name="northbridge-supplement",
        version="v1",
        sha256=dataset_sha256(REPO_ROOT, paths),
        hash_scheme=DATASET_HASH_SCHEME,
        chunker=SPAN_PACK_V1.version,
    )


def document_inputs() -> ExpectedInputs:
    from app.demo.snapshot import corpus_inputs

    return corpus_inputs(supplement())


def load_document_replay(space: EmbeddingSpace, *, max_batch_items: int) -> ReplayEmbeddingProvider:
    return load_replay_provider(
        DOCUMENT_EMBEDDINGS,
        dataset=supplement_subject(),
        active_space=space,
        max_batch_items=max_batch_items,
        inputs=document_inputs(),
    )


def load_query_replay(
    space: EmbeddingSpace, queries: Sequence[EmbeddingInput], *, max_batch_items: int
) -> ReplayEmbeddingProvider:
    return load_replay_provider(
        QUERY_EMBEDDINGS,
        dataset=supplement_subject(),
        active_space=space,
        max_batch_items=max_batch_items,
        inputs=ExpectedInputs((), tuple(queries)),
    )


class CombinedReplay:
    """Several replay snapshots of one space as one provider: each input is
    answered by the snapshot that holds it. Still no network, no spend."""

    def __init__(self, *providers: ReplayEmbeddingProvider) -> None:
        if not providers or any(p.space != providers[0].space for p in providers):
            raise ValueError("combined replays must share one embedding space")
        self._providers = providers

    @property
    def space(self) -> EmbeddingSpace:
        return self._providers[0].space

    @property
    def max_batch_items(self) -> int:
        return min(p.max_batch_items for p in self._providers)

    @property
    def snapshot_sha256(self) -> str:
        return "+".join(p.snapshot_sha256 for p in self._providers)

    async def embed(self, inputs: Sequence[EmbeddingInput]) -> EmbeddingResult:
        owners = [next((p for p in self._providers if p.covers(i)), self._providers[0])
                  for i in inputs]  # fmt: skip
        if len(set(map(id, owners))) == 1:
            return await owners[0].embed(inputs)  # also reports what no snapshot holds
        vectors: list[Any] = [None] * len(inputs)
        results = []
        for provider in self._providers:
            picked = [n for n, owner in enumerate(owners) if owner is provider]
            if picked:
                result = await provider.embed([inputs[n] for n in picked])
                results.append(result)
                for n, vector in zip(picked, result.vectors, strict=True):
                    vectors[n] = vector
        return EmbeddingResult(
            vectors=tuple(vectors),
            space=self.space,
            served_model=results[0].served_model,
            usage=TokenUsage(),
            attempts=1,
            request_id="+".join(r.request_id or "" for r in results),
        )

    async def aclose(self) -> None:
        return None


# --- the authored answers -----------------------------------------------------------------

_SPAN = re.compile(r"(?m)^\[(S[1-9][0-9]*)\] [^\n]*\n")
_ASSESSMENT = re.compile(r"(?m)^\[(A[1-9][0-9]*)\] ([^\n]*)$")
_CANDIDATE = re.compile(r"(?m)^\[(C[1-9][0-9]*)\] [^\n]* — ([^\n]*)\n")
_MARKER = re.compile(r"^\s*\d+\.\s+")


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _blocks(material: str, header: re.Pattern[str]) -> list[tuple[re.Match[str], str]]:
    """Each rendered block: its header match and its text up to the next one."""
    found = list(header.finditer(material))
    ends = [m.start() for m in found[1:]] + [len(material)]
    return [(m, material[m.end() : end].rstrip("\n")) for m, end in zip(found, ends, strict=True)]


def _between(user: str, begin: str, end: str) -> str:
    start = user.index(">>>\n", user.index(f"<<<{begin} ")) + 4
    return user[start : user.index(f"\n<<<{end} ")]


class AuthoredProvider:
    """Answers the structured calls about the supplementary courses from
    ``BRIEFS``, quoting only the material each call shows: a fact whose
    quote is not in that material is not claimed. Obligations: none stated.
    No network, no tokens."""

    name = "fixture"

    def __init__(self, briefs: Sequence[Brief] = BRIEFS) -> None:
        self._briefs = tuple(briefs)

    def model_for(self, role: ModelRole) -> str:
        return AUTHORED_MODEL

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
        thinking: bool | None = None,
    ) -> StructuredResult[T]:
        user = messages[-1].content
        output: dict[str, Any]
        if schema is AssessmentExtraction:
            output = {"assessments": self._assessments(_between(user, "COURSE_MATERIAL",
                                                                  "END_COURSE_MATERIAL"))}  # fmt: skip
        elif schema is RequirementExtraction:
            output = {"requirements": self._requirements(_between(user, "COURSE_MATERIAL",
                                                                  "END_COURSE_MATERIAL"))}  # fmt: skip
        elif schema is ObligationExtraction:
            output = {"obligations": []}
        elif schema is MappingSelection:
            output = self._selection(_between(user, "MAPPING_MATERIAL", "END_MAPPING_MATERIAL"))
        else:
            raise TypeError(f"no authored answers for {schema.__name__}")
        return StructuredResult(
            output=schema.model_validate(output),
            model=AUTHORED_MODEL,
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=0),
            attempts=1,
        )

    @staticmethod
    def _cite(spans: list[tuple[str, str]], quote: str) -> dict[str, str] | None:
        """The span that holds ``quote`` exactly, as evidence; else None."""
        span = next((sid for sid, text in spans if quote in text), None)
        return None if span is None else {"span": span, "quote": quote}

    @staticmethod
    def _spans(material: str) -> list[tuple[str, str]]:
        return [(m.group(1), text) for m, text in _blocks(material, _SPAN)]

    def _assessments(self, material: str) -> list[dict[str, Any]]:
        spans = self._spans(material)
        out = []
        for brief in self._briefs:
            title = self._cite(spans, brief.title_quote)
            if title is None:
                continue
            weighting = self._cite(spans, brief.weighting_quote)
            due = self._cite(spans, brief.due_quote)
            out.append({
                "title": brief.title,
                "title_evidence": title,
                "weighting": weighting and {"percent": brief.weighting, "evidence": weighting},
                "deadline": (
                    {"status": "stated", "due_date": brief.due_date, "due_time": brief.due_time,
                     "evidence": due}
                    if due else
                    {"status": "not_stated", "due_date": None, "due_time": None, "evidence": None}
                ),
            })  # fmt: skip
        return out

    def _requirements(self, material: str) -> list[dict[str, Any]]:
        listed = {
            canonical_key(m.group(2)): m.group(1)
            for m in _ASSESSMENT.finditer(material.split("\n\n[S", 1)[0])
        }
        spans = [(sid, _norm(text)) for sid, text in self._spans(material)]
        out = []
        for brief in self._briefs:
            local = listed.get(canonical_key(brief.title))
            if local is None:
                continue
            for requirement in brief.requirements:
                quote = " ".join(requirement.statement.split())
                span = next((sid for sid, text in spans if _norm(quote) in text), None)
                if span is not None:
                    out.append({"assessment": local, "statement": {"span": span, "quote": quote},
                                "lead_in": None})  # fmt: skip
        return out

    def _requirement(self, text: str) -> Requirement | None:
        wanted = _norm(text)
        return next(
            (
                r
                for b in self._briefs
                for r in b.requirements
                if _norm(_MARKER.sub("", r.statement)) == wanted
            ),
            None,
        )

    def _selection(self, material: str) -> dict[str, Any]:
        head, _, candidates = material.partition("\n\nCandidates:\n")
        text = head.partition("Requirement: ")[2]
        requirement = self._requirement(text)
        chosen = []
        if requirement is not None:
            for match, body in _blocks(candidates, _CANDIDATE):
                if any(match.group(2) == title and _norm(phrase) in _norm(body)
                       for title, phrase in requirement.taught_in):  # fmt: skip
                    chosen.append(match.group(1))
        return {"decision": "selected" if chosen else "none", "candidate_ids": chosen}
