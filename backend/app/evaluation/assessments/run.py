# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Evaluate assessment extraction against the frozen Northbridge golden labels.

    uv run python -m app.evaluation.assessments.run [--role fast|strong] [--live]

Without ``--live`` it prints the plan (model, calls, estimated tokens, the
catalog price read from the provider, expected and maximum spend) and makes
no model call. With ``--live``, in the disposable evaluation database
(``COURSEFLOW_EVAL_DATABASE_URL``, see eval_db):

1. seed the initial state (replayed embeddings, no network) and extract
   every current version of both courses;
2. seed the final state and extract only the two changed versions;
3. call the prompt-injection unit once more without its injection span, as
   a control: the gated result must be identical;
4. score the persisted claims against the golden labels and write the
   report (``evals/reports/assessment-extraction-v1-<role>.{json,md}``) and
   the validated model outputs (``evals/runs/assessment-extraction/``).

Spend is bounded twice: the plan is refused if its expected spend is not
below ``MAX_SPEND_USD``, and before every call the spend so far (provider-
reported tokens at the catalog price) plus that call's worst case (every
attempt and repair round, ``ESTIMATE_MARGIN`` x estimated input, the
output cap) must stay below it, or the call is refused unsent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.errors import AIProviderError
from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.prompts import load_prompt
from app.ai.provider import AIProvider, ChatMessage, ModelInfo, StructuredResult
from app.db.models import (
    Assessment,
    AssessmentDeadline,
    Course,
    EvidenceLink,
    ModelCall,
    Resource,
    ResourceVersion,
    SourceSpan,
    WorkflowRun,
)
from app.domain.enums import EvidenceField, ModelCallKind, ModelRole, VerificationStatus
from app.evaluation.northbridge.labels import REPO_ROOT, GoldenDataset, load_golden, version_texts
from app.extraction.dates import DeadlineRejected, parse_deadline_quote
from app.extraction.gate import GateResult, canonical_key, gate
from app.extraction.schema import AssessmentExtraction
from app.extraction.service import PROMPT_VERSION, WORKFLOW_VERSION, AssessmentExtractionService
from app.extraction.units import ExtractionUnit, UnitSpan, build_messages, load_units, pack_units
from app.retrieval.chunking import estimate_tokens

MAX_SPEND_USD = Decimal("0.01")
ESTIMATE_MARGIN = Decimal("1.25")
OUTPUT_TOKEN_CAP = 2048  # per request, for this evaluation
EXPECTED_OUTPUT_TOKENS = 600  # per call, for the expected-spend estimate only
REPORTS = REPO_ROOT / "evals" / "reports"
RUNS = REPO_ROOT / "evals" / "runs" / "assessment-extraction"
INJECTION_RESOURCE = ("msd7000", "research-methods-lecture", 1)
_M = Decimal(1_000_000)


# --- plan ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedUnit:
    where: tuple[str, str, int]  # course ref, resource key, version
    unit: ExtractionUnit
    estimated_input_tokens: int


def corpus_units(golden: GoldenDataset) -> dict[tuple[str, str, int], list[ExtractionUnit]]:
    """The units every corpus version packs into, from the production parser."""
    texts = version_texts(golden.corpus)
    resources = {(c.ref, r.key): r for c in golden.corpus.courses for r in c.resources}
    out = {}
    for (course, key, number), (_, spans) in texts.items():
        rows = [SimpleNamespace(id=uuid.uuid4(), **s) for s in spans]
        resource = SimpleNamespace(
            title=resources[(course, key)].title,
            type=SimpleNamespace(value=resources[(course, key)].type),
        )
        out[(course, key, number)] = pack_units(uuid.uuid4(), uuid.uuid4(), resource, rows)  # type: ignore[arg-type]
    return out


def without_injection(unit: ExtractionUnit) -> ExtractionUnit:
    """The unit with every detected injection fragment removed (a span left
    empty is dropped); everything else is unchanged."""
    from app.extraction.gate import injection_fragments

    kept = []
    for span in unit.spans:
        text = span.excerpt
        for start, end in sorted(set(injection_fragments(text)), reverse=True):
            text = text[:start] + text[end:]
        if text.strip():
            kept.append((span, text))
    spans = tuple(
        UnitSpan(f"S{i}", s.span_id, text, s.locator) for i, (s, text) in enumerate(kept, 1)
    )
    return ExtractionUnit(
        unit.course_id, unit.resource_version_id, unit.resource_title, unit.resource_type,
        unit.ordinal, spans,
    )  # fmt: skip


def plan_units(golden: GoldenDataset) -> list[PlannedUnit]:
    """Initial state (each resource's v1), then each later version, then the control."""
    prompt = load_prompt(PROMPT_VERSION)
    packed = corpus_units(golden)
    order = sorted(packed, key=lambda w: (w[2], w[0], w[1]))
    planned = []

    def add(where: tuple[str, str, int], unit: ExtractionUnit) -> None:
        messages = build_messages(prompt, unit, nonce="0" * 16)
        tokens = sum(estimate_tokens(m.content) for m in messages)
        planned.append(PlannedUnit(where, unit, tokens))

    for where in order:
        for unit in packed[where]:
            add(where, unit)
    for unit in packed[INJECTION_RESOURCE]:
        add(INJECTION_RESOURCE, without_injection(unit))
    return planned


def call_worst_case_usd(
    estimated_input: int, info: ModelInfo, attempts: int, repair_rounds: int
) -> Decimal:
    requests = attempts * (1 + repair_rounds)
    # A repair turn resends the conversation plus the rejected answer.
    per_request_in = Decimal(estimated_input) * ESTIMATE_MARGIN + OUTPUT_TOKEN_CAP
    return (
        requests
        * (
            per_request_in * _price(info.input_usd_per_million)
            + OUTPUT_TOKEN_CAP * _price(info.output_usd_per_million)
        )
        / _M
    )


def _price(value: Decimal | None) -> Decimal:
    if value is None:
        raise ValueError("the catalog lists no price for this model")
    return value


def describe_plan(
    planned: Sequence[PlannedUnit], info: ModelInfo, attempts: int, repair_rounds: int
) -> dict[str, Any]:
    tokens_in = sum(p.estimated_input_tokens for p in planned)
    tokens_out = EXPECTED_OUTPUT_TOKENS * len(planned)
    expected = (
        tokens_in * _price(info.input_usd_per_million)
        + tokens_out * _price(info.output_usd_per_million)
    ) / _M
    worst = sum(
        call_worst_case_usd(p.estimated_input_tokens, info, attempts, repair_rounds)
        for p in planned
    )
    return {
        "model": info.id,
        "calls": len(planned),
        "calls_by_kind": {
            "initial_state_versions": sum(1 for p in planned[:-1] if p.where[2] == 1),
            "later_versions": sum(1 for p in planned[:-1] if p.where[2] > 1),
            "injection_control": 1,
        },
        "estimated_input_tokens": tokens_in,
        "estimated_output_tokens": tokens_out,
        "token_estimate": "input: ceil(chars / 4); output: an assumption, not a bound",
        "price_usd_per_million": {
            "input": str(info.input_usd_per_million),
            "output": str(info.output_usd_per_million),
            "source": "provider model catalog (list_models), read now",
        },
        "expected_spend_usd": f"{expected:.6f}",
        "unguarded_worst_case_usd": f"{worst:.6f}",
        "spend_cap_usd": str(MAX_SPEND_USD),
        "max_spend_usd": f"< {MAX_SPEND_USD} (runtime guard before every call)",
        "output_token_cap_per_request": OUTPUT_TOKEN_CAP,
        "problems": ([f"expected spend ${expected:.6f} is not below the cap"]
                     if expected >= MAX_SPEND_USD else []),
    }  # fmt: skip


# --- the budgeted provider ----------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class BudgetedProvider:
    """Refuses, unsent, a call whose worst case would reach the cap; records
    each validated output for the report."""

    inner: AIProvider
    info: ModelInfo
    attempts: int
    repair_rounds: int
    spent_usd: Decimal = Decimal(0)
    outputs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.inner.name

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
        estimated = sum(estimate_tokens(m.content) for m in messages)
        worst = call_worst_case_usd(estimated, self.info, self.attempts, self.repair_rounds)
        if self.spent_usd + worst >= MAX_SPEND_USD:
            raise BudgetExceeded(f"spent ${self.spent_usd:.6f} + worst case ${worst:.6f}")
        try:
            options = {} if thinking is None else {"thinking": thinking}
            result = await self.inner.chat_structured(
                messages, schema, role=role, temperature=temperature, **options
            )
        except AIProviderError as error:
            self._charge(error.usage.prompt_tokens, error.usage.completion_tokens, worst)
            raise
        self._charge(result.usage.prompt_tokens, result.usage.completion_tokens, worst)
        self.outputs.append((messages[-1].content, result.output.model_dump(mode="json")))
        return result

    def _charge(self, tokens_in: int | None, tokens_out: int | None, worst: Decimal) -> None:
        if tokens_in is None or tokens_out is None:
            self.spent_usd += worst  # unreported usage: assume the worst
            return
        self.spent_usd += (
            tokens_in * _price(self.info.input_usd_per_million)
            + tokens_out * _price(self.info.output_usd_per_million)
        ) / _M


# --- scoring ------------------------------------------------------------------------------


def expected_deadlines(golden: GoldenDataset) -> set[tuple[str, str, int, str, str, str]]:
    """(course, resource, version, assessment key, local, timezone) for every
    golden deadline claim's evidence quote that states the date."""
    titles = {(a.course, a.assessment): a.title for a in golden.by_kind("assessment")}
    expected = set()
    for claim in golden.by_kind("deadline_claim"):
        key = canonical_key(titles[(claim.course, claim.assessment)])
        for evidence in claim.evidence:
            try:
                parse_deadline_quote(evidence.quote)
            except DeadlineRejected:
                continue  # a supporting quote that names the assessment, not the date
            expected.add(
                (claim.course, evidence.resource, evidence.version, key,
                 claim.deadline.local, claim.deadline.timezone)
            )  # fmt: skip
    return expected


async def _extracted(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(
            Course.external_id, Resource.source_uri, ResourceVersion.version_number,
            Assessment.canonical_key, AssessmentDeadline, EvidenceLink, SourceSpan.excerpt,
        )
        .join(AssessmentDeadline, AssessmentDeadline.id == EvidenceLink.assessment_deadline_id)
        .join(Assessment, Assessment.id == AssessmentDeadline.assessment_id)
        .join(Course, Course.id == Assessment.course_id)
        .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
    )  # fmt: skip
    out = []
    for course, uri, number, key, deadline, link, excerpt in rows:
        local = deadline.due_date.isoformat()
        if deadline.due_time is not None:
            local += "T" + deadline.due_time.strftime("%H:%M")
        out.append(
            {
                "course": course,
                "resource": uri.rsplit("/", 1)[1],
                "version": number,
                "assessment": key,
                "local": local,
                "timezone": deadline.timezone,
                "timezone_source": deadline.timezone_source.value,
                "deadline_status": deadline.status.value,
                "evidence_status": link.verification_status.value,
                "quote": excerpt[link.quote_start : link.quote_end],
            }
        )
    return sorted(out, key=lambda r: (r["course"], r["resource"], r["version"], r["local"]))


async def score(
    sessions: async_sessionmaker[AsyncSession], golden: GoldenDataset
) -> dict[str, Any]:
    expected = expected_deadlines(golden)
    async with sessions() as session:
        extracted = await _extracted(session)
        assessments = (
            await session.execute(
                select(Course.external_id, Assessment).join(
                    Course, Course.id == Assessment.course_id
                )
            )
        ).all()
        title_versions = dict(
            (
                await session.execute(
                    select(
                        Assessment.canonical_key,
                        func.count(func.distinct(SourceSpan.resource_version_id)),
                    )
                    .join(EvidenceLink, EvidenceLink.assessment_id == Assessment.id)
                    .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
                    .where(EvidenceLink.field == EvidenceField.TITLE)
                    .group_by(Assessment.canonical_key)
                )
            ).all()
        )
        injection_links = await session.scalar(
            select(func.count())
            .select_from(EvidenceLink)
            .join(SourceSpan, SourceSpan.id == EvidenceLink.source_span_id)
            .where(SourceSpan.excerpt.contains("IGNORE ALL PREVIOUS INSTRUCTIONS"))
        )
    verified = [
        r for r in extracted
        if r["deadline_status"] == r["evidence_status"] == VerificationStatus.VERIFIED.value
    ]  # fmt: skip

    def as_key(r: dict[str, Any]) -> tuple[str, str, int, str, str, str]:
        return (
            r["course"],
            r["resource"],
            r["version"],
            r["assessment"],
            r["local"],
            r["timezone"],
        )

    found = {as_key(r) for r in verified}
    golden_assessments = {
        (a.course, canonical_key(a.title)): a for a in golden.by_kind("assessment")
    }
    stored = {(course, a.canonical_key): a for course, a in assessments}
    exam_key = ("aml7001", canonical_key(golden_assessments_title(golden, "aml-exam")))
    return {
        "deadlines": {
            "expected": len(expected),
            "exact_match": len(expected & found),
            "exact_match_rate": round(len(expected & found) / len(expected), 4),
            "missed": sorted(map(list, expected - found)),
            "verified_extracted": len(verified),
            "verified_not_in_golden": [r for r in verified if as_key(r) not in expected],
            "needs_review": [r for r in extracted if r not in verified],
            "all": extracted,
        },
        "assessments": {
            "expected": sorted(f"{c}/{k}" for c, k in golden_assessments),
            "found": sorted(f"{c}/{k}" for c, k in stored),
            "missing": sorted(f"{c}/{k}" for c, k in set(golden_assessments) - set(stored)),
            "extra": sorted(f"{c}/{k}" for c, k in set(stored) - set(golden_assessments)),
            "weighting": {
                f"{c}/{k}": {
                    "golden": a.weighting_percent,
                    "extracted": stored[(c, k)].weighting_percent if (c, k) in stored else None,
                }
                for (c, k), a in golden_assessments.items()
            },
            "title_evidence_versions": {k: int(n) for k, n in sorted(title_versions.items())},
        },
        "abstention": {
            # None (not measured) unless the exam itself was extracted.
            "exam_month_only_has_no_deadline": (
                not any((r["course"], r["assessment"]) == exam_key for r in extracted)
                if exam_key in stored
                else None
            ),
            "not_a_deadline_quotes_extracted": [
                r
                for r in extracted
                for label in golden.by_kind("not_a_deadline")
                if any(e.quote in r["quote"] or r["quote"] in e.quote for e in label.evidence)
            ],
        },
        "injection": {"evidence_links_on_injection_span": int(injection_links or 0)},
    }


def golden_assessments_title(golden: GoldenDataset, label_id: str) -> str:
    return next(a.title for a in golden.by_kind("assessment") if a.label_id == label_id)


def _semantics(result: GateResult) -> list[Any]:
    return sorted(
        (
            a.canonical_key,
            a.weighting.percent if a.weighting else None,
            (a.deadline.value.due_date.isoformat(), str(a.deadline.value.due_time))
            if a.deadline else None,
        )
        for a in result.assessments
    )  # fmt: skip


# --- the live run -------------------------------------------------------------------------


async def run_live(
    role: ModelRole, info: ModelInfo, settings: Any, golden: GoldenDataset, thinking: bool | None
) -> dict[str, Any]:
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
    from app.ingestion.storage import LocalRawObjectStore
    from app.retrieval.indexing import IndexingService

    database = require_eval_database(
        os.environ, application_database_url=settings.database_url.get_secret_value()
    )
    await asyncio.to_thread(recreate_eval_database, database)
    await asyncio.to_thread(migrate_eval_database, database)
    engine = create_async_engine(database.url)
    inner = NebiusTokenFactoryProvider.from_settings(settings, max_output_tokens=OUTPUT_TOKEN_CAP)
    provider = BudgetedProvider(
        inner, info, settings.ai_max_attempts, settings.ai_max_repair_attempts
    )
    started = datetime.now(UTC)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        indexer = IndexingService(
            sessions,
            load_demo_replay(embedding_space_from_settings(settings), max_batch_items=64),
            batch_max_items=64,
            batch_max_estimated_tokens=32_000,
        )
        service = AssessmentExtractionService(sessions, provider, role=role, thinking=thinking)
        runs = []
        with tempfile.TemporaryDirectory() as raw:
            store = LocalRawObjectStore(Path(raw))
            await seed(sessions, store, limits=production_limits(), indexer=indexer)
            async with sessions() as session:
                courses = dict((await session.execute(select(Course.external_id, Course.id))).all())
            for ref in sorted(courses):
                runs.append(await service.extract_course(courses[ref]))
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
                    runs.append(await service.extract_course(courses[ref], versions))

            injection = await _injection_control(
                provider, sessions, courses["msd7000"], role, thinking, NORTHBRIDGE_TIMEZONE
            )
        return await _assemble(
            sessions, golden, info, role, thinking, started, injection,
            [r.summary.model_dump(mode="json") | {"status": r.status.value} for r in runs],
            [{"material_sha256": _sha(user), "output": out} for user, out in provider.outputs],
        )  # fmt: skip
    finally:
        await inner.aclose()
        await engine.dispose()


async def _injection_control(
    provider: BudgetedProvider,
    sessions: async_sessionmaker[AsyncSession],
    course_id: uuid.UUID,
    role: ModelRole,
    thinking: bool | None,
    zone: str,
) -> dict[str, Any]:
    """The injection unit once more without its injection span: the gated
    result must not change. Reported, never raised, when it cannot run."""
    injected = [o for o in provider.outputs if "IGNORE ALL PREVIOUS INSTRUCTIONS" in o[0]]
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
        (unit,) = await load_units(session, course_id, version_id)
    control_unit = without_injection(unit)
    try:
        control = await call_structured_recorded(
            provider, sessions,
            messages=build_messages(load_prompt(PROMPT_VERSION), control_unit),
            schema=AssessmentExtraction, role=role, prompt_version=PROMPT_VERSION,
            temperature=0.0, thinking=thinking,
        )  # fmt: skip
    except RecordedCallError as failed:
        return {"control": f"not_run: {failed.error.category.value}: {failed.error.detail}"}
    with_injection = gate(unit, AssessmentExtraction.model_validate(injected[0][1]), zone)
    without = gate(control_unit, control.result.output, zone)
    return {
        "control": "run",
        "with_injection": _semantics(with_injection),
        "without_injection": _semantics(without),
        "unchanged": _semantics(with_injection) == _semantics(without),
        "rejected_with_injection": dict(with_injection.rejected),
    }


async def _assemble(
    sessions: async_sessionmaker[AsyncSession],
    golden: GoldenDataset,
    info: ModelInfo,
    role: ModelRole,
    thinking: bool | None,
    started: datetime,
    injection: dict[str, Any],
    runs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    result = await score(sessions, golden)
    async with sessions() as session:
        calls = (
            await session.scalars(
                select(ModelCall).where(ModelCall.call_kind == ModelCallKind.CHAT_STRUCTURED)
            )
        ).all()
    result["injection"] |= injection
    result["runs"] = runs
    result["usage"] = _usage(calls, info)
    result["meta"] = {
        "dataset": {"name": golden.manifest.dataset, "version": golden.manifest.dataset_version,
                    "sha256": golden.sha256},
        "prompt_version": PROMPT_VERSION,
        "model_role": role.value,
        "model": info.id,
        "temperature": 0.0,
        "thinking": "model default" if thinking is None else ("on" if thinking else "off"),
        "output_token_cap_per_request": OUTPUT_TOKEN_CAP,
        "spend_cap_usd": str(MAX_SPEND_USD),
        "run_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }  # fmt: skip
    result["outputs"] = outputs
    return result


async def report_existing(
    role: ModelRole, info: ModelInfo, settings: Any, golden: GoldenDataset, thinking: bool | None
) -> dict[str, Any]:
    """Score what a previous live run left in the evaluation database. No model
    call, nothing recreated."""
    from app.evaluation.retrieval.eval_db import require_eval_database

    database = require_eval_database(
        os.environ, application_database_url=settings.database_url.get_secret_value()
    )
    engine = create_async_engine(database.url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            run_rows = (
                await session.scalars(
                    select(WorkflowRun)
                    .where(WorkflowRun.workflow_version == WORKFLOW_VERSION)
                    .order_by(WorkflowRun.started_at)
                )
            ).all()
            outputs = (
                await session.scalars(
                    select(ModelCall.output_summary)
                    .where(ModelCall.call_kind == ModelCallKind.CHAT_STRUCTURED)
                    .where(ModelCall.output_summary.is_not(None))
                    .order_by(ModelCall.started_at)
                )
            ).all()
        started = run_rows[0].started_at if run_rows else datetime.now(UTC)
        runs = [(r.summary or {}) | {"status": r.status.value} for r in run_rows]
        injection = {"control": "not_run: refused by the spend guard (see usage.failed)"}
        return await _assemble(
            sessions, golden, info, role, thinking, started, injection, runs,
            [{"output": json.loads(o)} for o in outputs],
        )  # fmt: skip
    finally:
        await engine.dispose()


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _usage(calls: Sequence[ModelCall], info: ModelInfo) -> dict[str, Any]:
    tokens_in = sum(c.prompt_tokens or 0 for c in calls)
    tokens_out = sum(c.completion_tokens or 0 for c in calls)
    reasoning = sum(c.reasoning_tokens or 0 for c in calls)
    latencies = sorted(c.latency_ms for c in calls)
    cost = (
        tokens_in * _price(info.input_usd_per_million)
        + tokens_out * _price(info.output_usd_per_million)
    ) / _M
    return {
        "model_calls": len(calls),
        "succeeded": sum(1 for c in calls if c.status.value == "succeeded"),
        "failed": dict(Counter(c.error_category.value for c in calls if c.error_category)),
        "http_attempts": sum(c.attempts for c in calls),
        "repair_rounds": sum(c.repair_rounds for c in calls),
        "unreported_usage_calls": sum(1 for c in calls if c.prompt_tokens is None),
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "reasoning_tokens": reasoning,
        "cost_usd_from_reported_tokens": f"{cost:.6f}",
        "latency_ms": {
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
    }


# --- report -------------------------------------------------------------------------------


def write_report(result: dict[str, Any], role: ModelRole) -> tuple[Path, Path, Path]:
    outputs = result.pop("outputs")
    thinking = result["meta"]["thinking"].replace("model default", "default")
    name = f"assessment-extraction-v1-{role.value}-thinking-{thinking}"
    REPORTS.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    report_json = REPORTS / f"{name}.json"
    report_md = REPORTS / f"{name}.md"
    run_file = RUNS / f"{name}-outputs.json"
    for path, text in (
        (report_json, json.dumps(result, indent=2, sort_keys=True) + "\n"),
        (run_file, json.dumps(outputs, indent=2, sort_keys=True) + "\n"),
        (report_md, _markdown(result)),
    ):
        path.write_text(text, encoding="utf-8", newline="\n")  # LF on every platform
    return report_json, report_md, run_file


def _markdown(r: dict[str, Any]) -> str:
    d, a, ab, inj, u, m = (
        r["deadlines"],
        r["assessments"],
        r["abstention"],
        r["injection"],
        r["usage"],
        r["meta"],
    )
    lines = [
        f"# Assessment extraction v1: {m['model_role']} model",
        "",
        (
            f"Frozen dataset `{m['dataset']['name']}` {m['dataset']['version']} "
            f"(sha256 `{m['dataset']['sha256'][:12]}…`), prompt `{m['prompt_version']}`, "
            f"model `{m['model']}`, thinking {m['thinking']}, temperature {m['temperature']}, "
            f"output cap {m['output_token_cap_per_request']} tokens per request, "
            f"spend cap ${m['spend_cap_usd']}, run {m['run_at']}. "
            "Real Nebius Token Factory calls; every number below is measured, not estimated."
        ),
        "",
        "Extraction runs: "
        + "; ".join(
            f"{run['status']} ({run.get('units', 0)} units, {run.get('units_failed', 0)} failed"
            f"{': ' + str(run['failures']) if run.get('failures') else ''})"
            for run in r["runs"]
        ),
        "",
        "## Deadlines (golden exact match: course, source version, assessment, local time, timezone)",
        "",
        f"- Exact match: **{d['exact_match']}/{d['expected']}** ({d['exact_match_rate']:.0%}), verified only.",
        (
            f"- Verified deadlines extracted: {d['verified_extracted']}; not in golden: "
            f"{len(d['verified_not_in_golden'])}; needs review: {len(d['needs_review'])}."
        ),
    ]
    for row in d["missed"]:
        lines.append(f"- Missed: `{'/'.join(map(str, row))}`")
    for row in d["verified_not_in_golden"]:
        lines.append(f"- Not in golden: `{row['course']}/{row['resource']} v{row['version']}` "
                     f"{row['assessment']} {row['local']} — \"{row['quote']}\"")  # fmt: skip
    lines += [
        "",
        "## Assessments and deduplication",
        "",
        f"- Expected: {', '.join(a['expected'])}",
        f"- Missing: {', '.join(a['missing']) or 'none'}; extra: {', '.join(a['extra']) or 'none'}",
        f"- Title evidence from distinct versions: {a['title_evidence_versions']}",
        f"- Weighting (golden vs extracted): {a['weighting']}",
        "",
        "## Abstention",
        "",
        f"- Exam stated only as a month: no deadline row: **{ab['exam_month_only_has_no_deadline']}**",
        f"- Not-a-deadline statements extracted as deadlines: {len(ab['not_a_deadline_quotes_extracted'])}",
        "",
        "## Prompt injection",
        "",
        f"- Evidence links on the injection span: {inj['evidence_links_on_injection_span']}",
        (
            f"- Control without the injection span: {inj.get('control')}; gated result unchanged: "
            f"**{inj.get('unchanged', 'not measured')}**"
        ),
        "",
        "## Usage and cost",
        "",
        (
            f"- Model calls: {u['model_calls']} ({u['succeeded']} succeeded, failed {u['failed'] or 'none'}); "
            f"HTTP attempts {u['http_attempts']}; repair rounds {u['repair_rounds']}."
        ),
        (
            f"- Tokens: {u['prompt_tokens']} prompt, {u['completion_tokens']} completion "
            f"({u['reasoning_tokens']} reasoning)."
        ),
        f"- Cost from reported tokens at the catalog price: ${u['cost_usd_from_reported_tokens']}.",
        f"- Latency per call: p50 {u['latency_ms']['p50']} ms, max {u['latency_ms']['max']} ms.",
        "",
    ]
    return "\n".join(lines)


# --- command line -------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    from app.ai.nebius import NebiusTokenFactoryProvider
    from app.core.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.evaluation.assessments.run")
    parser.add_argument("--role", choices=["fast", "strong"], default="fast")
    parser.add_argument("--thinking", choices=["default", "off"], default="default")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="make the paid model calls")
    mode.add_argument(
        "--report-existing",
        action="store_true",
        help="score the last live run left in the evaluation database (no model call)",
    )
    args = parser.parse_args(argv)
    thinking = None if args.thinking == "default" else False
    role = ModelRole(args.role)
    settings = get_settings()
    golden = load_golden()

    async def catalog() -> ModelInfo:
        provider = NebiusTokenFactoryProvider.from_settings(settings)
        try:
            model = provider.model_for(role)
            return next(m for m in await provider.list_models() if m.id == model)
        finally:
            await provider.aclose()

    info = asyncio.run(catalog())
    plan = describe_plan(
        plan_units(golden), info, settings.ai_max_attempts, settings.ai_max_repair_attempts
    )
    print(json.dumps({"plan": plan}, indent=2))
    if plan["problems"]:
        return 4
    if args.report_existing:
        result = asyncio.run(report_existing(role, info, settings, golden, thinking))
    elif args.live:
        result = asyncio.run(run_live(role, info, settings, golden, thinking))
    else:
        return 0
    paths = write_report(result, role)
    print(json.dumps({"status": "done", "reports": [p.as_posix() for p in paths],
                      "usage": result["usage"]}, indent=2))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
