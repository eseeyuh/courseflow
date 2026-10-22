# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Dependency edges from the wording of verified obligations
(``dependency-rules/v1``). No model call: the ordering is read from the
source text by fixed rules, so no end can be invented.

Rules, each on one verified ``obligation`` evidence quote, in a current
version, of a verified obligation:

- ``before-submitting``: the obligation is linked to an assessment and its
  quote says "before you submit your <that assessment>". Edge: obligation
  -> assessment.
- ``data-before-ethics-approval``: the quote forbids collecting data before
  or until ethics approval. Edge: milestone ``ethics_approval`` ->
  milestone ``data_collection``.

The relation is ``required_for`` when the obligation is mandatory, else
``before``. The edge's evidence is the quote itself (span + exact range).
A run replaces the course's edges with the derived set; every end is
checked to be of the course before anything is written.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Assessment,
    DependencyEdge,
    EvidenceLink,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
)
from app.domain.enums import (
    DependencyNodeKind,
    DependencyRelation,
    EvidenceField,
    EvidenceSubject,
    Milestone,
    ObligationModality,
    VerificationStatus,
    WorkflowRunStatus,
)
from app.reasoning.runs import finish_run, start_run

RULES_VERSION = "dependency-rules/v1"
BEFORE_SUBMITTING = f"{RULES_VERSION}:before-submitting"
DATA_BEFORE_ETHICS = f"{RULES_VERSION}:data-before-ethics-approval"

_DATA_BEFORE_ETHICS = re.compile(
    r"\b(?:no data (?:may|can|shall) be collected"
    r"|(?:must not|may not|cannot|do not)\s+(?:recruit participants or\s+)?collect\s+(?:any\s+)?data)"
    r"\b.*?\b(?:before|until)\b.*?\bethics\b.*?\b(?:approv\w*|granted)\b"
)


def _normal(text: str) -> str:
    return " ".join(text.lower().split())


@dataclass(frozen=True, slots=True)
class End:
    kind: DependencyNodeKind
    assessment_id: uuid.UUID | None = None
    obligation_id: uuid.UUID | None = None
    milestone: Milestone | None = None

    @classmethod
    def of_milestone(cls, milestone: Milestone) -> End:
        return cls(DependencyNodeKind.MILESTONE, milestone=milestone)


@dataclass(frozen=True, slots=True)
class Quote:
    span_id: uuid.UUID
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class ObligationFacts:
    """A verified obligation and its verified wording."""

    id: uuid.UUID
    assessment_id: uuid.UUID | None
    assessment_key: str | None
    modality: ObligationModality | None
    quotes: tuple[Quote, ...]


@dataclass(frozen=True, slots=True)
class DerivedEdge:
    relation: DependencyRelation
    prerequisite: End
    dependent: End
    rule: str
    obligation_id: uuid.UUID
    evidence: tuple[Quote, ...]

    @property
    def key(self) -> tuple[object, ...]:
        return (self.relation, self.prerequisite, self.dependent)


def derive_edges(obligations: Sequence[ObligationFacts]) -> list[DerivedEdge]:
    """Every edge the rules find, merged per (relation, ends): pure."""
    found: dict[tuple[object, ...], DerivedEdge] = {}

    def add(edge: DerivedEdge) -> None:
        if edge.key in found:  # another statement of the same ordering: more evidence
            prior = found[edge.key]
            evidence = tuple(dict.fromkeys(prior.evidence + edge.evidence))
            edge = DerivedEdge(prior.relation, prior.prerequisite, prior.dependent, prior.rule,
                               prior.obligation_id, evidence)  # fmt: skip
        found[edge.key] = edge

    for o in sorted(obligations, key=lambda o: str(o.id)):
        relation = (
            DependencyRelation.REQUIRED_FOR
            if o.modality is ObligationModality.MANDATORY
            else DependencyRelation.BEFORE
        )
        for quote in o.quotes:
            text = _normal(quote.text)
            key = o.assessment_key
            if (
                o.assessment_id is not None
                and key
                and any(
                    f"before {lead} {key}" in text
                    for lead in ("you submit your", "you submit the", "submitting your")
                )
            ):
                add(DerivedEdge(
                    relation, End(DependencyNodeKind.OBLIGATION, obligation_id=o.id),
                    End(DependencyNodeKind.ASSESSMENT, assessment_id=o.assessment_id),
                    BEFORE_SUBMITTING, o.id, (quote,),
                ))  # fmt: skip
            if _DATA_BEFORE_ETHICS.search(text):
                add(DerivedEdge(
                    relation, End.of_milestone(Milestone.ETHICS_APPROVAL),
                    End.of_milestone(Milestone.DATA_COLLECTION), DATA_BEFORE_ETHICS, o.id,
                    (quote,),
                ))  # fmt: skip
    return list(found.values())


async def obligation_facts(session: AsyncSession, course_id: uuid.UUID) -> list[ObligationFacts]:
    rows = (
        await session.execute(
            select(Obligation, Assessment.canonical_key)
            .outerjoin(Assessment, Assessment.id == Obligation.assessment_id)
            .where(
                Obligation.course_id == course_id,
                Obligation.status == VerificationStatus.VERIFIED,
            )
        )
    ).all()
    # Current wording only: evidence in a superseded version is history.
    links = (
        await session.execute(
            select(EvidenceLink, SourceSpan.excerpt)
            .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
            .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(
                EvidenceLink.obligation_id.in_([o.id for o, _ in rows]),
                EvidenceLink.field == EvidenceField.OBLIGATION,
                EvidenceLink.verification_status == VerificationStatus.VERIFIED,
                Resource.current_version_id == ResourceVersion.id,
            )
            .order_by(EvidenceLink.source_span_id, EvidenceLink.quote_start)
        )
    ).all()
    quotes: dict[uuid.UUID, list[Quote]] = {}
    for link, excerpt in links:
        quotes.setdefault(link.obligation_id, []).append(  # type: ignore[arg-type]
            Quote(link.source_span_id, link.quote_start, link.quote_end,
                  excerpt[link.quote_start : link.quote_end])
        )  # fmt: skip
    return [
        ObligationFacts(o.id, o.assessment_id, key, o.modality, tuple(quotes.get(o.id, ())))
        for o, key in rows
    ]


class DependencyIntegrityError(RuntimeError):
    """An edge end is not a claim of the course. Names IDs only."""


async def _check_ends(
    session: AsyncSession, course_id: uuid.UUID, edges: Sequence[DerivedEdge]
) -> None:
    obligations = {
        i for e in edges for i in (e.prerequisite.obligation_id, e.dependent.obligation_id,
                                   e.obligation_id) if i is not None
    }  # fmt: skip
    assessments = {
        i for e in edges for i in (e.prerequisite.assessment_id, e.dependent.assessment_id)
        if i is not None
    }  # fmt: skip
    found_o = set(
        (
            await session.scalars(
                select(Obligation.id).where(
                    Obligation.id.in_(obligations), Obligation.course_id == course_id
                )
            )
        ).all()
    )
    found_a = set(
        (
            await session.scalars(
                select(Assessment.id).where(
                    Assessment.id.in_(assessments), Assessment.course_id == course_id
                )
            )
        ).all()
    )
    missing = sorted(str(i) for i in (obligations - found_o) | (assessments - found_a))
    if missing:
        raise DependencyIntegrityError(f"not claims of course {course_id}: {', '.join(missing)}")


def _columns(edge: DerivedEdge) -> dict[str, object]:
    out: dict[str, object] = {"relation": edge.relation}
    for name, end in (("prerequisite", edge.prerequisite), ("dependent", edge.dependent)):
        out |= {
            f"{name}_kind": end.kind,
            f"{name}_assessment_id": end.assessment_id,
            f"{name}_obligation_id": end.obligation_id,
            f"{name}_milestone": end.milestone,
        }
    return out


@dataclass(frozen=True, slots=True)
class EdgeCounts:
    created: int = 0
    kept: int = 0
    removed: int = 0
    links_created: int = 0


async def replace_edges(
    session: AsyncSession, course_id: uuid.UUID, edges: Sequence[DerivedEdge], run_id: uuid.UUID
) -> EdgeCounts:
    """The course's edges become exactly ``edges``; existing ones keep their ID."""
    await _check_ends(session, course_id, edges)
    created = kept = links = 0
    ids: list[uuid.UUID] = []
    for edge in edges:
        columns = _columns(edge)
        inserted = await session.scalar(
            insert(DependencyEdge)
            .values(course_id=course_id, rule=edge.rule,
                    derived_from_obligation_id=edge.obligation_id, workflow_run_id=run_id,
                    **columns)
            .on_conflict_do_nothing(constraint="uq_dependency_edges_ends")
            .returning(DependencyEdge.id)
        )  # fmt: skip
        if inserted is None:
            kept += 1
            inserted = await session.scalar(
                select(DependencyEdge.id).where(
                    DependencyEdge.course_id == course_id,
                    *(getattr(DependencyEdge, k).is_(v) if v is None
                      else getattr(DependencyEdge, k) == v for k, v in columns.items()),
                )
            )  # fmt: skip
            if inserted is None:
                raise RuntimeError("dependency edge vanished during persistence")
        else:
            created += 1
        ids.append(inserted)
        for quote in edge.evidence:
            link = await session.scalar(
                insert(EvidenceLink)
                .values(subject_type=EvidenceSubject.DEPENDENCY_EDGE, dependency_edge_id=inserted,
                        field=EvidenceField.DEPENDENCY, source_span_id=quote.span_id,
                        quote_start=quote.start, quote_end=quote.end,
                        verification_status=VerificationStatus.VERIFIED, workflow_run_id=run_id)
                .on_conflict_do_nothing(constraint="uq_evidence_links_fact_quote")
                .returning(EvidenceLink.id)
            )  # fmt: skip
            links += link is not None
    stale = await session.execute(
        delete(DependencyEdge)
        .where(DependencyEdge.course_id == course_id, DependencyEdge.id.not_in(ids))
        .returning(DependencyEdge.id)
    )
    return EdgeCounts(created, kept, len(stale.all()), links)


class DependencySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: str = RULES_VERSION
    obligations_read: int = 0
    edges: int = 0
    created: int = 0
    kept: int = 0
    removed: int = 0
    links_created: int = 0
    by_rule: dict[str, int] = {}


@dataclass(frozen=True, slots=True)
class DependencyRunResult:
    run_id: uuid.UUID
    status: WorkflowRunStatus
    summary: DependencySummary


async def derive_course_dependencies(
    sessions: async_sessionmaker[AsyncSession], course_id: uuid.UUID
) -> DependencyRunResult:
    """One run: read the verified obligations, derive, replace, all in one
    transaction (no model call, nothing to wait for)."""
    summary = DependencySummary()
    run_id = await start_run(sessions, course_id, RULES_VERSION)
    try:
        async with sessions() as session, session.begin():
            facts = await obligation_facts(session, course_id)
            edges = derive_edges(facts)
            counts = await replace_edges(session, course_id, edges, run_id)
        summary = DependencySummary(
            obligations_read=len(facts), edges=len(edges), created=counts.created,
            kept=counts.kept, removed=counts.removed, links_created=counts.links_created,
            by_rule=dict(Counter(e.rule for e in edges)),
        )  # fmt: skip
        status = WorkflowRunStatus.SUCCEEDED
    except BaseException:
        await finish_run(sessions, run_id, WorkflowRunStatus.FAILED, summary)
        raise
    await finish_run(sessions, run_id, status, summary)
    return DependencyRunResult(run_id, status, summary)
