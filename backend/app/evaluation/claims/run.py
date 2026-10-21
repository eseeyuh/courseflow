# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Evaluate requirement and obligation extraction against the frozen
Northbridge golden labels.

    uv run python -m app.evaluation.claims.run [--live] [--spend-cap 0.03]

Without ``--live`` it prints the plan (calls per task, estimated tokens, the
catalog price read from the provider now, expected and worst-case spend)
and makes no model call. With ``--live``, in the disposable evaluation
database (``COURSEFLOW_EVAL_DATABASE_URL``):

1. seed the initial state; assessments come from replaying the recorded
   FAST assessment outputs (``RECORDED_ASSESSMENTS``, no model call), so a
   requirement or obligation error is never an assessment-extraction error;
2. extract requirements (briefs) and obligations (every resource) live;
3. seed the final state and extract obligations from the changed versions;
4. call the obligation prompt once more on the prompt-injection unit
   without its injection span, as a control;
5. score and write the report (``evals/reports/claim-extraction-v1-...``)
   and the validated outputs (``evals/runs/claim-extraction/``).

Matching is by evidence, never by wording: a stored claim matches a label
when one of its evidence links overlaps one of the label's exact quotes in
the same span of the same source version. The golden requirement and
obligation labels do not claim to be exhaustive, so only labelled recall is
a metric: every other extracted claim is listed as ``unlabelled_for_review``
and no precision or F1 is computed. The labels carry no category or
modality; the expected values for the labelled obligations
(``EXPECTED_OBLIGATIONS``) were fixed in this file before any live call.

Spend is bounded by the assessment evaluation's guard (``BudgetedProvider``:
refused unsent when spend so far plus the call's worst case would reach the
cap) and by a call limit: a call beyond the planned count is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.prompts import load_prompt
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult
from app.db.models import (
    Assessment,
    AssessmentRequirement,
    Course,
    EvidenceLink,
    ModelCall,
    Obligation,
    Resource,
    ResourceVersion,
    SourceSpan,
)
from app.domain.enums import ModelCallKind, ModelRole, ResourceType, VerificationStatus
from app.evaluation.assessments import run as assessments_run
from app.evaluation.assessments.run import (
    BudgetedProvider,
    BudgetExceeded,
    EvalConfig,
    ReplayProvider,
    assign_outputs,
    call_worst_case_usd,
    corpus_units,
    spans_of,
    without_injection,
)
from app.evaluation.northbridge.labels import REPO_ROOT, GoldenDataset, load_golden
from app.extraction.claim_units import (
    AssessmentRef,
    ClaimUnit,
    build_claim_messages,
    load_claim_units,
)
from app.extraction.claims_service import TASKS, ClaimExtractionService, Task
from app.extraction.gate import canonical_key
from app.extraction.obligations import ObligationExtraction, gate_obligations
from app.retrieval.chunking import estimate_tokens

MAX_SPEND_USD = Decimal("0.03")
EXPECTED_OUTPUT_TOKENS = 600  # per call, for the expected-spend estimate only
REPORTS = REPO_ROOT / "evals" / "reports"
RUNS = REPO_ROOT / "evals" / "runs" / "claim-extraction"
RECORDED_ASSESSMENTS = (
    REPO_ROOT
    / "evals"
    / "runs"
    / "assessment-extraction"
    / "assessment-extraction-v2-fast-thinking-off-gate-v3-outputs.json"
)
ASSESSMENT_PROMPT = "assessment-extraction/v2"
INJECTION_RESOURCE = ("msd7000", "research-methods-lecture", 1)
INJECTION_MARKER = "IGNORE ALL PREVIOUS INSTRUCTIONS"
_M = Decimal(1_000_000)
V = VerificationStatus

# Fixed before any live call; the frozen labels state neither. From each
# label's summary: (category, modality, the canonical key of applies_to).
EXPECTED_OBLIGATIONS: dict[str, tuple[str, str, str]] = {
    "aml-cw1-resample-training-only": ("other", "mandatory", "coursework 1"),
    "msd-supervisor-review-v1": ("supervisor_review", "mandatory", "project specification"),
    "msd-supervisor-review-v2": ("supervisor_review", "mandatory", "project specification"),
}


# --- plan ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedCall:
    task: Task | str  # "requirements", "obligations" or "control"
    where: tuple[str, str, int]  # course ref, resource key, version
    estimated_input_tokens: int


def _refs(golden: GoldenDataset, course: str) -> tuple[AssessmentRef, ...]:
    """The golden assessments of a course, standing in for extracted ones when
    estimating tokens (the live run lists what extraction stored)."""
    titles = sorted(a.title for a in golden.by_kind("assessment") if a.course == course)
    return tuple(
        AssessmentRef(f"A{i}", uuid.uuid4(), t, canonical_key(t), False)
        for i, t in enumerate(titles, 1)
    )


def plan_calls(golden: GoldenDataset) -> list[PlannedCall]:
    """In live order: requirements (initial briefs), obligations (initial
    versions, then later ones), then the injection control."""
    packed = corpus_units(golden)
    types = {(c.ref, r.key): r.type for c in golden.corpus.courses for r in c.resources}
    order = sorted(packed, key=lambda w: (w[2], w[0], w[1]))
    planned: list[PlannedCall] = []

    def add(task: str, where: tuple[str, str, int], unit: Any) -> None:
        prompt = load_prompt(TASKS["obligations" if task == "control" else task].prompt_version)  # type: ignore[index]
        messages = build_claim_messages(
            prompt, ClaimUnit(unit, _refs(golden, where[0])), nonce="0" * 16
        )
        planned.append(PlannedCall(task, where, sum(estimate_tokens(m.content) for m in messages)))

    for task in ("requirements", "obligations"):
        allowed = TASKS[task].resource_types  # type: ignore[index]
        for where in order:
            if task == "requirements" and where[2] > 1:
                continue  # requirements are read from the initial briefs only
            if allowed is not None and ResourceType(types[where[:2]]) not in allowed:
                continue
            for unit in packed[where]:
                add(task, where, unit)
    for unit in packed[INJECTION_RESOURCE]:
        add("control", INJECTION_RESOURCE, without_injection(unit))
    return planned


def describe_plan(
    planned: Sequence[PlannedCall],
    info: ModelInfo,
    attempts: int,
    repair_rounds: int,
    spend_cap: Decimal,
) -> dict[str, Any]:
    price_in = assessments_run._price(info.input_usd_per_million)
    price_out = assessments_run._price(info.output_usd_per_million)
    tokens_in = sum(p.estimated_input_tokens for p in planned)
    tokens_out = EXPECTED_OUTPUT_TOKENS * len(planned)
    expected = (tokens_in * price_in + tokens_out * price_out) / _M
    worst = sum(
        call_worst_case_usd(p.estimated_input_tokens, info, attempts, repair_rounds)
        for p in planned
    )
    return {
        "model": info.id,
        "calls": len(planned),
        "calls_by_task": dict(Counter(str(p.task) for p in planned)),
        "estimated_input_tokens": tokens_in,
        "estimated_output_tokens": tokens_out,
        "price_usd_per_million": {
            "input": str(info.input_usd_per_million),
            "output": str(info.output_usd_per_million),
            "source": "provider model catalog (list_models), read now",
        },
        "expected_spend_usd": f"{expected:.6f}",
        "unguarded_worst_case_usd": f"{worst:.6f}",
        "spend_cap_usd": str(spend_cap),
        "assessments": f"replayed from {RECORDED_ASSESSMENTS.name} (no model call)",
        "problems": (
            [f"expected spend ${expected:.6f} is not below the cap"]
            if expected >= spend_cap
            else []
        ),
    }


# --- providers ------------------------------------------------------------------------------


class CallLimitExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class LimitedProvider:
    """A budgeted provider that also refuses, unsent, any call beyond the plan."""

    inner: BudgetedProvider
    max_calls: int
    calls: int = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def outputs(self) -> list[tuple[str, dict[str, Any]]]:
        return self.inner.outputs

    def model_for(self, role: ModelRole) -> str:
        return self.inner.model_for(role)

    async def list_models(self) -> list[ModelInfo]:
        return await self.inner.list_models()

    async def chat_structured[T: BaseModel](
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        temperature: float | None = None,
        thinking: bool | None = None,
    ) -> StructuredResult[T]:
        if self.calls >= self.max_calls:
            raise CallLimitExceeded(f"call {self.calls + 1} is beyond the {self.max_calls} planned")
        self.calls += 1
        return await self.inner.chat_structured(
            messages, schema, role=role, temperature=temperature, thinking=thinking
        )


# --- scoring ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Location:
    span_id: uuid.UUID
    start: int
    end: int

    def overlaps(self, other: Location) -> bool:
        return self.span_id == other.span_id and self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class LinkRow:
    location: Location
    field: str
    status: str
    resource: str
    version: int
    locator: str
    quote: str


@dataclass(frozen=True, slots=True)
class ClaimRow:
    id: uuid.UUID
    status: str
    review_reason: str | None
    assessment: str | None  # canonical key
    text: str  # a requirement's text or an obligation's canonical action
    category: str | None
    modality: str | None
    links: tuple[LinkRow, ...]

    def matches(self, locations: Sequence[Location]) -> bool:
        return any(link.location.overlaps(loc) for link in self.links for loc in locations)

    def brief(self) -> dict[str, Any]:
        first = self.links[0] if self.links else None
        return {
            "text": self.text,
            "status": self.status,
            "review_reason": self.review_reason,
            "assessment": self.assessment,
            "category": self.category,
            "modality": self.modality,
            "where": f"{first.resource} v{first.version} {first.locator}" if first else None,
            "quotes": sorted({link.quote for link in self.links}),
        }


def _locator(span: SourceSpan) -> str:
    if span.slide_number is not None:
        return f"slide {span.slide_number}"
    if span.page_number is not None:
        return f"page {span.page_number}"
    return span.section_path or "section"


async def label_locations(
    session: AsyncSession, golden: GoldenDataset, kinds: Sequence[str]
) -> dict[str, list[Location]]:
    """Each label's exact evidence quotes, located in the stored spans."""
    out: dict[str, list[Location]] = {}
    for kind in kinds:
        for label in golden.by_kind(kind):
            found = []
            for evidence in label.evidence:
                uri = f"demo://{golden.corpus.institution.key}/{label.course}/{evidence.resource}"
                spans = (
                    await session.scalars(
                        select(SourceSpan)
                        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                        .join(Resource, Resource.id == ResourceVersion.resource_id)
                        .where(
                            Resource.source_uri == uri,
                            ResourceVersion.version_number == evidence.version,
                            SourceSpan.excerpt.contains(evidence.quote),
                        )
                    )
                ).all()
                for span in spans:
                    start = span.excerpt.index(evidence.quote)
                    found.append(Location(span.id, start, start + len(evidence.quote)))
            out[label.label_id] = found
    return out


async def _links(
    session: AsyncSession, column: Any, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[LinkRow]]:
    rows = await session.execute(
        select(EvidenceLink, SourceSpan, ResourceVersion, Resource)
        .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(column.in_(ids))
        .order_by(Resource.source_uri, ResourceVersion.version_number, SourceSpan.ordinal,
                  EvidenceLink.quote_start)
    )  # fmt: skip
    out: dict[uuid.UUID, list[LinkRow]] = {}
    for link, span, version, resource in rows:
        out.setdefault(getattr(link, column.key), []).append(
            LinkRow(
                Location(span.id, link.quote_start, link.quote_end),
                link.field.value,
                link.verification_status.value,
                resource.source_uri.rsplit("/", 1)[1],
                version.version_number,
                _locator(span),
                span.excerpt[link.quote_start : link.quote_end],
            )
        )
    return out


async def stored_claims(session: AsyncSession) -> tuple[list[ClaimRow], list[ClaimRow]]:
    requirements = (
        await session.execute(
            select(AssessmentRequirement, Assessment.canonical_key).join(
                Assessment, Assessment.id == AssessmentRequirement.assessment_id
            )
        )
    ).all()
    obligations = (
        await session.execute(
            select(Obligation, Assessment.canonical_key).outerjoin(
                Assessment, Assessment.id == Obligation.assessment_id
            )
        )
    ).all()
    r_links = await _links(
        session, EvidenceLink.assessment_requirement_id, [r.id for r, _ in requirements]
    )
    o_links = await _links(session, EvidenceLink.obligation_id, [o.id for o, _ in obligations])
    return (
        [
            ClaimRow(r.id, r.status.value, r.review_reason and r.review_reason.value, key,
                     r.text, None, None, tuple(r_links.get(r.id, [])))
            for r, key in requirements
        ],
        [
            ClaimRow(o.id, o.status.value, o.review_reason and o.review_reason.value, key,
                     o.canonical_action, o.category.value, o.modality and o.modality.value,
                     tuple(o_links.get(o.id, [])))
            for o, key in obligations
        ],
    )  # fmt: skip


def _labels_hit(
    claim: ClaimRow, locations: dict[str, list[Location]], kind_of: dict[str, str]
) -> dict[str, list[str]]:
    hit: dict[str, list[str]] = {}
    for label_id, locs in locations.items():
        if claim.matches(locs):
            hit.setdefault(kind_of[label_id], []).append(label_id)
    return hit


def score_requirements(
    golden: GoldenDataset, claims: Sequence[ClaimRow], locations: dict[str, list[Location]]
) -> dict[str, Any]:
    titles = {(a.course, a.assessment): a.title for a in golden.by_kind("assessment")}
    labels = golden.by_kind("requirement")
    per_label, matched_ids = [], set()
    for label in labels:
        key = canonical_key(titles[(label.course, label.assessment)])
        hits = [c for c in claims if c.matches(locations[label.label_id])]
        matched_ids |= {c.id for c in hits}
        right = [c for c in hits if c.assessment == key]
        per_label.append(
            {
                "label": label.label_id,
                "matched": bool(right),
                "verified": any(c.status == V.VERIFIED for c in right),
                "statuses": sorted(f"{c.status}:{c.review_reason}" for c in right),
                "rows": len(right),  # more than one: the statement was split
                "wrong_assessment": [c.assessment for c in hits if c.assessment != key],
            }
        )
    n = len(labels)
    verified = sum(p["verified"] for p in per_label)
    extras = [c for c in claims if c.id not in matched_ids]
    return {
        "labelled": n,
        "labels_exhaustive": False,
        "recall_verified": verified,
        "recall_verified_rate": verified / n if n else None,
        "recall_any_status": sum(p["matched"] for p in per_label),
        "per_label": per_label,
        "missed": [p["label"] for p in per_label if not p["matched"]],
        "stored": dict(Counter(c.status for c in claims)),
        "needs_review": [c.brief() for c in claims if c.status == V.NEEDS_REVIEW],
        "unlabelled_for_review": [c.brief() for c in extras],
    }


def score_obligations(
    golden: GoldenDataset, claims: Sequence[ClaimRow], locations: dict[str, list[Location]]
) -> dict[str, Any]:
    kind_of = {label.label_id: label.kind for label in golden.labels}
    labels = golden.by_kind("obligation")
    per_label, matched_ids = [], set()
    for label in labels:
        hits = [c for c in claims if c.matches(locations[label.label_id])]
        matched_ids |= {c.id for c in hits}
        category, modality, assessment = EXPECTED_OBLIGATIONS[label.label_id]
        verified = [c for c in hits if c.status == V.VERIFIED]
        best = verified[0] if verified else (hits[0] if hits else None)
        per_label.append(
            {
                "label": label.label_id,
                "hidden": label.hidden,
                "matched": bool(hits),
                "verified": bool(verified),
                "rows": len(hits),
                "status": best and f"{best.status}:{best.review_reason}",
                "expected": {"category": category, "modality": modality, "assessment": assessment},
                "category_correct": best is not None and best.category == category,
                "modality_correct": best is not None and best.modality == modality,
                "assessment_correct": best is not None and best.assessment == assessment,
                "evidence": best.brief()["quotes"] if best else [],
                "where": best.brief()["where"] if best else None,
            }
        )
    supervisor = [p for p in per_label if p["label"].startswith("msd-supervisor-review")]
    n = len(labels)
    verified_n = sum(p["verified"] for p in per_label)
    checked = [p for p in per_label if p["matched"]]
    guards = {}
    for kind in ("needs_review", "not_a_deadline", "prompt_injection"):
        guards[kind] = [
            {
                "label": label.label_id,
                "rows": [
                    f"{c.status}:{c.review_reason}"
                    for c in claims
                    if c.matches(locations[label.label_id])
                ],
            }
            for label in golden.by_kind(kind)
        ]
    extras = [c for c in claims if c.id not in matched_ids]
    return {
        "labelled": n,
        "labels_exhaustive": False,
        "recall_verified": verified_n,
        "recall_verified_rate": verified_n / n if n else None,
        "recall_any_status": sum(p["matched"] for p in per_label),
        "per_label": per_label,
        "supervisor_review_killer_case": all(
            p["verified"]
            and p["category_correct"]
            and p["modality_correct"]
            and p["assessment_correct"]
            and p["where"] is not None
            and p["where"].startswith("dissertation-handbook")
            and p["where"].endswith("page 2")
            for p in supervisor
        ),
        "category_correct": f"{sum(p['category_correct'] for p in checked)}/{len(checked)}",
        "modality_correct": f"{sum(p['modality_correct'] for p in checked)}/{len(checked)}",
        "ambiguous_never_verified": all(
            not any(s.startswith("verified") for s in g["rows"]) for g in guards["needs_review"]
        ),
        "generic_never_verified": all(
            not any(s.startswith("verified") for s in g["rows"]) for g in guards["not_a_deadline"]
        ),
        "guards": guards,
        "stored": dict(Counter(c.status for c in claims)),
        "unlabelled_for_review": [
            c.brief() | {"overlaps_labels": _labels_hit(c, locations, kind_of)} for c in extras
        ],
    }


def _semantics(claim_unit: ClaimUnit, output: dict[str, Any], zone: str) -> list[Any]:
    gated = gate_obligations(claim_unit, ObligationExtraction.model_validate(output), zone)
    return sorted(
        (o.canonical_key, o.category.value, o.modality and o.modality.value, o.review_reason)
        for o in gated.obligations
    )


async def _injection_control(
    provider: Any,
    sessions: async_sessionmaker[AsyncSession],
    course_id: uuid.UUID,
    zone: str,
) -> dict[str, Any]:
    """The injection unit once more without its injection span, through the
    obligation prompt: the gated result must not change."""
    injected = [o for o in provider.outputs if INJECTION_MARKER in o[0]]
    if not injected:
        return {"control": "not_run: the injection unit produced no validated output"}
    async with sessions() as session:
        version_id = await session.scalar(
            select(ResourceVersion.id)
            .join(Resource, Resource.id == ResourceVersion.resource_id)
            .where(Resource.source_uri.endswith("/research-methods-lecture"))
        )
        if version_id is None:
            return {"control": "not_run: the research methods lecture is missing"}
        (claim_unit,) = await load_claim_units(session, course_id, version_id)
    control_unit = ClaimUnit(without_injection(claim_unit.unit), claim_unit.assessments)
    prompt = load_prompt(TASKS["obligations"].prompt_version)
    try:
        control = await call_structured_recorded(
            provider, sessions,
            messages=build_claim_messages(prompt, control_unit),
            schema=ObligationExtraction, role=ModelRole.FAST, prompt_version=prompt.version,
            temperature=0.0, thinking=False,
        )  # fmt: skip
    except RecordedCallError as failed:
        return {"control": f"not_run: {failed.error.category.value}"}
    with_injection = _semantics(claim_unit, injected[0][1], zone)
    without = _semantics(control_unit, control.result.output.model_dump(mode="json"), zone)
    return {
        "control": "run",
        "with_injection": with_injection,
        "without_injection": without,
        "unchanged": with_injection == without,
    }


# --- the live run -------------------------------------------------------------------------


async def run_live(
    info: ModelInfo,
    settings: Any,
    golden: GoldenDataset,
    spend_cap: Decimal,
    max_calls: int,
    model: Any = None,
) -> dict[str, Any]:
    """The live run; model replaces the Nebius provider (an offline dry run)."""
    from app.ai.nebius import NebiusTokenFactoryProvider
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.demo.seed import NORTHBRIDGE_TIMEZONE, seed
    from app.demo.snapshot import load_demo_replay
    from app.evaluation.retrieval.eval_db import (
        migrate_eval_database,
        recreate_eval_database,
        require_eval_database,
    )
    from app.evaluation.retrieval.resolve import production_limits
    from app.extraction.service import AssessmentExtractionService
    from app.ingestion.storage import LocalRawObjectStore
    from app.retrieval.indexing import IndexingService

    recorded = json.loads(RECORDED_ASSESSMENTS.read_text(encoding="utf-8"))
    assessment_plan = assessments_run.plan_units(golden, EvalConfig(ASSESSMENT_PROMPT))
    replay = ReplayProvider(assign_outputs(recorded, assessment_plan), "replay:recorded-fast")
    database = require_eval_database(
        os.environ, application_database_url=settings.database_url.get_secret_value()
    )
    await asyncio.to_thread(recreate_eval_database, database)
    await asyncio.to_thread(migrate_eval_database, database)
    engine = create_async_engine(database.url)
    inner = model or NebiusTokenFactoryProvider.from_settings(
        settings, max_output_tokens=assessments_run.OUTPUT_TOKEN_CAP
    )
    budgeted = BudgetedProvider(
        inner, info, settings.ai_max_attempts, settings.ai_max_repair_attempts,
        spend_cap_usd=spend_cap,
    )  # fmt: skip
    provider = LimitedProvider(budgeted, max_calls)
    started = datetime.now(UTC)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        indexer = IndexingService(
            sessions,
            load_demo_replay(embedding_space_from_settings(settings), max_batch_items=64),
            batch_max_items=64,
            batch_max_estimated_tokens=32_000,
        )
        assess = AssessmentExtractionService(
            sessions, replay, thinking=False, prompt_version=ASSESSMENT_PROMPT
        )
        services = {
            task: ClaimExtractionService(sessions, provider, task, thinking=False)  # type: ignore[arg-type]
            for task in ("requirements", "obligations")
        }
        runs: list[dict[str, Any]] = []

        async def extract(task: str, course_id: uuid.UUID, versions: Any = None) -> None:
            result = await services[task].extract_course(course_id, versions)
            runs.append(result.summary.model_dump(mode="json") | {"status": result.status.value})

        with tempfile.TemporaryDirectory() as raw:
            store = LocalRawObjectStore(Path(raw))
            await seed(sessions, store, limits=production_limits(), indexer=indexer)
            async with sessions() as session:
                courses = dict((await session.execute(select(Course.external_id, Course.id))).all())
            for ref in sorted(courses):
                await assess.extract_course(courses[ref])
            for task in ("requirements", "obligations"):
                for ref in sorted(courses):
                    await extract(task, courses[ref])
            await seed(sessions, store, limits=production_limits(), indexer=indexer, state="final")
            async with sessions() as session:
                later = (
                    await session.execute(
                        select(Resource.course_id, ResourceVersion.id)
                        .join(ResourceVersion, ResourceVersion.resource_id == Resource.id)
                        .where(ResourceVersion.version_number > 1)
                    )
                ).all()
            for ref in sorted(courses):
                versions = [v for c, v in later if c == courses[ref]]
                if versions:
                    await assess.extract_course(courses[ref], versions)
                    await extract("obligations", courses[ref], versions)
            injection = await _injection_control(
                provider, sessions, courses["msd7000"], NORTHBRIDGE_TIMEZONE
            )
        return await assemble(sessions, golden, info, started, injection, runs, provider, spend_cap)
    finally:
        if model is None:
            await inner.aclose()
        await engine.dispose()


async def assemble(
    sessions: async_sessionmaker[AsyncSession],
    golden: GoldenDataset,
    info: ModelInfo,
    started: datetime,
    injection: dict[str, Any],
    runs: list[dict[str, Any]],
    provider: Any,
    spend_cap: Decimal,
) -> dict[str, Any]:
    prompts = [TASKS[t].prompt_version for t in ("requirements", "obligations")]
    async with sessions() as session:
        requirements, obligations = await stored_claims(session)
        locations = await label_locations(
            session,
            golden,
            ["requirement", "obligation", "needs_review", "not_a_deadline", "dependency",
             "prompt_injection"],
        )  # fmt: skip
        calls = (
            await session.scalars(
                select(ModelCall).where(
                    ModelCall.call_kind == ModelCallKind.CHAT_STRUCTURED,
                    ModelCall.prompt_version.in_(prompts),
                )
            )
        ).all()
        injection_span = locations["msd-prompt-injection"]
        on_injection = sum(
            1 for claim in (*requirements, *obligations) if claim.matches(injection_span)
        )
    return {
        "requirements": score_requirements(golden, requirements, locations),
        "obligations": score_obligations(golden, obligations, locations),
        "injection": injection | {"claims_with_evidence_on_injection_text": on_injection},
        "runs": runs,
        "usage": assessments_run._usage(calls, info),
        "meta": {
            "dataset": {
                "name": golden.manifest.dataset,
                "version": golden.manifest.dataset_version,
                "sha256": golden.sha256,
            },
            "prompts": prompts,
            "gates": [TASKS[t].gate_version for t in ("requirements", "obligations")],
            "assessments": f"replayed: {RECORDED_ASSESSMENTS.name}",
            "model_role": "fast",
            "model": info.id,
            "temperature": 0.0,
            "thinking": "off",
            "output_token_cap_per_request": assessments_run.OUTPUT_TOKEN_CAP,
            "spend_cap_usd": str(spend_cap),
            "run_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "outputs": [
            {"nonce_free_material_sha256": _sha(spans_of(user)), "output": out}
            for user, out in provider.outputs
        ],
    }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- report -------------------------------------------------------------------------------


def write_report(result: dict[str, Any]) -> tuple[Path, Path, Path]:
    outputs = result.pop("outputs")
    name = "claim-extraction-v1-fast-thinking-off"
    REPORTS.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    paths = (REPORTS / f"{name}.json", REPORTS / f"{name}.md", RUNS / f"{name}-outputs.json")
    texts = (
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        markdown(result),
        json.dumps(outputs, indent=2, sort_keys=True) + "\n",
    )
    for path, text in zip(paths, texts, strict=True):
        path.write_text(text, encoding="utf-8", newline="\n")
    return paths


def _extras(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"- `{r['status']}`{' (' + r['review_reason'] + ')' if r['review_reason'] else ''} "
        f"{r['where']}: {r['text']}"
        + (f" (overlaps {r['overlaps_labels']})" if r.get("overlaps_labels") else "")
        for r in rows
    ]


def markdown(r: dict[str, Any]) -> str:
    req, obl, inj, u, m = (
        r[k] for k in ("requirements", "obligations", "injection", "usage", "meta")
    )
    lines = [
        "# Requirement and obligation extraction v1: fast model",
        "",
        (
            f"Frozen dataset `{m['dataset']['name']}` {m['dataset']['version']} "
            f"(sha256 `{m['dataset']['sha256'][:12]}…`), prompts {', '.join(f'`{p}`' for p in m['prompts'])}, "
            f"gates {', '.join(f'`{g}`' for g in m['gates'])}, model `{m['model']}`, thinking off, "
            f"temperature 0, output cap {m['output_token_cap_per_request']} tokens, spend cap "
            f"${m['spend_cap_usd']}, run {m['run_at']}. Assessments {m['assessments']}. "
            "Real Nebius Token Factory calls; every number below is measured."
        ),
        "",
        (
            "The golden requirement and obligation labels are not exhaustive: only labelled recall is "
            "a metric. Every other extracted claim is listed for review; no precision or F1 is computed."
        ),
        "",
        "## Requirements",
        "",
        (
            f"- Labelled recall (verified): **{req['recall_verified']}/{req['labelled']}**; "
            f"any status: {req['recall_any_status']}/{req['labelled']}. Missed: {req['missed'] or 'none'}."
        ),
        f"- Stored: {req['stored']}.",
        f"- Needs review: {len(req['needs_review'])}; unlabelled for review: {len(req['unlabelled_for_review'])}.",
        *_extras(req["needs_review"]),
        "",
        "Unlabelled for review:",
        *(_extras(req["unlabelled_for_review"]) or ["- none"]),
        "",
        "## Obligations",
        "",
        (
            f"- Labelled recall (verified): **{obl['recall_verified']}/{obl['labelled']}**; "
            f"any status: {obl['recall_any_status']}/{obl['labelled']}."
        ),
        (
            f"- Supervisor-review killer case (verified, supervisor_review, mandatory, linked to the "
            f"Project Specification, handbook page 2, both versions): **{obl['supervisor_review_killer_case']}**."
        ),
        (
            f"- Category correct {obl['category_correct']}, modality correct {obl['modality_correct']} "
            "(expected values fixed before the run)."
        ),
        (
            f"- Ambiguous statements never verified: **{obl['ambiguous_never_verified']}**; "
            f"generic informational statements never verified: **{obl['generic_never_verified']}**."
        ),
        f"- Stored: {obl['stored']}.",
    ]
    for p in obl["per_label"]:
        lines.append(f"- `{p['label']}`: {p['status'] or 'not extracted'}, category "
                     f"{'ok' if p['category_correct'] else 'wrong'}, modality "
                     f"{'ok' if p['modality_correct'] else 'wrong'}, assessment "
                     f"{'ok' if p['assessment_correct'] else 'wrong'}; {p['where']}")  # fmt: skip
    for kind, rows in obl["guards"].items():
        for g in rows:
            lines.append(f"- {kind} `{g['label']}`: {g['rows'] or 'not extracted'}")
    lines += [
        "",
        "Unlabelled for review:",
        *(_extras(obl["unlabelled_for_review"]) or ["- none"]),
        "",
        "## Prompt injection",
        "",
        f"- Claims with evidence on the injection text: {inj['claims_with_evidence_on_injection_text']}.",
        (f"- Control without the injection span: {inj.get('control')}; gated result unchanged: "
        f"**{inj.get('unchanged', 'not measured')}**."),
        "",
        "## Usage and cost",
        "",
        (f"- Model calls: {u['model_calls']} ({u['succeeded']} succeeded, failed {u['failed'] or 'none'}); "
        f"HTTP attempts {u['http_attempts']}; repair rounds {u['repair_rounds']}."),
        (f"- Tokens: {u['prompt_tokens']} prompt, {u['completion_tokens']} completion "
        f"({u['reasoning_tokens']} reasoning)."),
        f"- Cost from reported tokens at the catalog price: ${u['cost_usd_from_reported_tokens']}.",
        f"- Latency per call: p50 {u['latency_ms']['p50']} ms, max {u['latency_ms']['max']} ms.",
        "",
    ]  # fmt: skip
    return "\n".join(lines)


# --- command line -------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius import NebiusTokenFactoryProvider
    from app.core.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.evaluation.claims.run")
    parser.add_argument("--live", action="store_true", help="make the paid model calls")
    parser.add_argument("--spend-cap", type=Decimal, default=MAX_SPEND_USD)
    parser.add_argument("--expect-calls", type=int, help="refuse unless the plan has this many")
    args = parser.parse_args(argv)
    settings = get_settings()
    golden = load_golden()

    async def catalog() -> ModelInfo:
        provider = NebiusTokenFactoryProvider.from_settings(settings)
        try:
            model = provider.model_for(ModelRole.FAST)
            return next(m for m in await provider.list_models() if m.id == model)
        finally:
            await provider.aclose()

    info = asyncio.run(catalog())
    planned = plan_calls(golden)
    plan = describe_plan(
        planned, info, settings.ai_max_attempts, settings.ai_max_repair_attempts, args.spend_cap
    )
    if args.expect_calls is not None and args.expect_calls != len(planned):
        plan["problems"].append(f"{len(planned)} calls planned, {args.expect_calls} expected")
    print(json.dumps({"plan": plan}, indent=2))
    if plan["problems"] or not args.live:
        return 4 if plan["problems"] else 0
    try:
        result = asyncio.run(run_live(info, settings, golden, args.spend_cap, len(planned)))
    except (BudgetExceeded, CallLimitExceeded) as refused:
        print(json.dumps({"status": "refused", "reason": str(refused)}), file=sys.stderr)
        return 5
    paths = write_report(result)
    print(json.dumps({"status": "written", "files": [p.as_posix() for p in paths]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
