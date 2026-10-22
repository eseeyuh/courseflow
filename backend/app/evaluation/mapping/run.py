# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The material-mapping demo smoke run on Northbridge.

    uv run python -m app.evaluation.mapping.run                 # replay, no spend
    uv run python -m app.evaluation.mapping.run --live \\
        --embedding-price-usd-per-million <price> [--spend-cap 0.01]

Builds the initial Northbridge state in the evaluation database
(``EVAL_DATABASE_URL``): seed and index (embedding snapshot replay), then
assessments and requirements from their recorded FAST outputs, so no
extraction call is made. Then maps every requirement with the production
service (hybrid retrieval, k=5).

- ``--live``: embeds the mapping queries once (recorded as a query snapshot,
  ``QUERY_SNAPSHOT``) and makes one FAST verifier call per requirement
  with candidates, under a spend cap and a call limit; the validated
  outputs are written to ``RECORDED``.
- default: replays both recordings. No network, no spend.

It reports the demo acceptance checks and the small dev / held-out
breakdown (span-level retrieval hit, mapping hit, error category). It is a
smoke test of the demo cases, not a benchmark.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.embeddings import EmbeddingInput, query_input
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.db.models import Course, MaterialMapping, Resource, ResourceVersion, SourceSpan
from app.domain.enums import ModelRole
from app.evaluation.mapping.split import Expect, MappingCase, Split, load_split
from app.evaluation.northbridge.labels import REPO_ROOT, GoldenDataset, load_golden
from app.evaluation.retrieval.snapshot import ExpectedInputs
from app.mapping.service import (
    MappingRunResult,
    MaterialMappingService,
    mapping_query,
    mapping_targets,
)
from app.retrieval.search import SearchRequest

# Committed: the demo and the default run replay them.
RECORDINGS = REPO_ROOT / "evals" / "recordings" / "material-mapping"
REPORTS = REPO_ROOT / "evals" / "reports"
RECORDED = RECORDINGS / "material-mapping-v1-fast-thinking-off-outputs.json"
QUERY_SNAPSHOT = (
    REPO_ROOT
    / "evals"
    / "snapshots"
    / "northbridge"
    / "v1"
    / "mapping-queries-nebius-qwen3-embedding-8b-d4096-sv1.jsonl"
)
CLAIM_OUTPUTS = (
    REPO_ROOT
    / "evals"
    / "runs"
    / "claim-extraction"
    / "claim-extraction-v1-fast-thinking-off-gate-v3-outputs.json"
)
REPORT_NAME = "material-mapping-v1-fast-thinking-off"
MAX_SPEND_USD = Decimal("0.01")
IRRELEVANT_KIND = "irrelevant_resource"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mapping_material(user: str) -> str:
    """The material of a verifier message, without the per-call nonce."""
    begin = user.index(">>>\n", user.index("<<<MAPPING_MATERIAL ")) + 4
    return user[begin : user.index("\n<<<END_MAPPING_MATERIAL ")]


class ReplayMissing(RuntimeError):
    pass


@dataclass(slots=True)
class MappingReplayProvider:
    """Answers each verifier call with the output recorded for exactly that
    material. No network, no tokens."""

    by_material: dict[str, dict[str, Any]]
    name: str = "replay"
    outputs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def model_for(self, role: ModelRole) -> str:
        return "replay:recorded-mapping-outputs"

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
        key = _sha(mapping_material(messages[-1].content))
        if key not in self.by_material:
            raise ReplayMissing(f"no recorded mapping output for material {key[:12]}")
        self.outputs.append((messages[-1].content, self.by_material[key]))
        return StructuredResult(
            output=schema.model_validate(self.by_material[key]),
            model=self.model_for(role),
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=0),
            attempts=1,
        )


def recorded_outputs(outputs: Sequence[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {"material_sha256": _sha(mapping_material(user)), "output": output}
        for user, output in outputs
    ]


# --- the Northbridge initial state, with no model call ------------------------------------


async def build_initial_state(
    sessions: async_sessionmaker[AsyncSession], settings: Any, raw: Path
) -> dict[str, uuid.UUID]:
    """Seed, index and extract assessments and requirements by replay."""
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.demo.seed import seed
    from app.demo.snapshot import load_demo_replay
    from app.evaluation.assessments import run as assessments_run
    from app.evaluation.assessments.run import EvalConfig, ReplayProvider, assign_outputs
    from app.evaluation.claims.run import (
        ASSESSMENT_PROMPT,
        RECORDED_ASSESSMENTS,
        ClaimReplayProvider,
        recorded_by_material,
    )
    from app.evaluation.retrieval.resolve import production_limits
    from app.extraction.claims_service import ClaimExtractionService
    from app.extraction.service import AssessmentExtractionService
    from app.ingestion.storage import LocalRawObjectStore
    from app.retrieval.indexing import IndexingService

    golden = load_golden()
    indexer = IndexingService(
        sessions,
        load_demo_replay(embedding_space_from_settings(settings), max_batch_items=64),
        batch_max_items=64,
        batch_max_estimated_tokens=32_000,
    )
    await seed(sessions, LocalRawObjectStore(raw), limits=production_limits(), indexer=indexer)
    plan = assessments_run.plan_units(golden, EvalConfig(ASSESSMENT_PROMPT))
    recorded = json.loads(RECORDED_ASSESSMENTS.read_text(encoding="utf-8"))
    assess = AssessmentExtractionService(
        sessions,
        ReplayProvider(assign_outputs(recorded, plan), "replay:recorded-fast"),
        thinking=False,
        prompt_version=ASSESSMENT_PROMPT,
    )
    claims = json.loads(CLAIM_OUTPUTS.read_text(encoding="utf-8"))
    requirements = ClaimExtractionService(
        sessions, ClaimReplayProvider(recorded_by_material(claims)), "requirements", thinking=False
    )
    async with sessions() as session:
        courses = dict((await session.execute(select(Course.external_id, Course.id))).all())
    for ref in sorted(courses):
        await assess.extract_course(courses[ref])
        await requirements.extract_course(courses[ref])
    return courses


async def query_inputs(
    sessions: async_sessionmaker[AsyncSession], courses: dict[str, uuid.UUID]
) -> tuple[EmbeddingInput, ...]:
    """Exactly the query/v1 inputs the mapping run will embed."""
    found: dict[str, EmbeddingInput] = {}
    async with sessions() as session:
        for ref in sorted(courses):
            course = await session.get(Course, courses[ref])
            assert course is not None
            for target in await mapping_targets(session, course):
                query = SearchRequest.build(
                    course_id=course.id,
                    query=mapping_query(target.requirement, target.assessment, target.course),
                ).query
                item = query_input(query)
                found.setdefault(item.text, item)
    return tuple(found.values())


# --- scoring ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanInfo:
    resource: str  # the resource key
    version: int
    locator: str


async def _span_info(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, SpanInfo]:
    rows = await session.execute(
        select(SourceSpan, ResourceVersion.version_number, Resource.source_uri)
        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
        .join(Resource, Resource.id == ResourceVersion.resource_id)
        .where(SourceSpan.id.in_(ids))
    )
    out = {}
    for span, version, uri in rows:
        locator = (
            f"slide {span.slide_number}"
            if span.slide_number is not None
            else f"page {span.page_number}"
            if span.page_number is not None
            else span.section_path or "section"
        )
        out[span.id] = SpanInfo(uri.rsplit("/", 1)[1], version, locator)
    return out


async def material_spans(session: AsyncSession, golden: GoldenDataset) -> dict[str, set[uuid.UUID]]:
    """Each requirement label's target spans: where its material quotes are."""
    out: dict[str, set[uuid.UUID]] = {}
    for label in golden.by_kind("requirement"):
        spans: set[uuid.UUID] = set()
        for material in label.materials:
            uri = f"demo://{golden.corpus.institution.key}/{label.course}/{material.resource}"
            spans.update(
                (
                    await session.scalars(
                        select(SourceSpan.id)
                        .join(ResourceVersion, ResourceVersion.id == SourceSpan.resource_version_id)
                        .join(Resource, Resource.id == ResourceVersion.resource_id)
                        .where(
                            Resource.source_uri == uri,
                            ResourceVersion.version_number == material.version,
                            SourceSpan.excerpt.contains(material.quote),
                        )
                    )
                ).all()
            )
        out[label.label_id] = spans
    return out


async def requirement_labels(session: AsyncSession, golden: GoldenDataset) -> dict[uuid.UUID, str]:
    """Stored requirement -> its label, by overlapping evidence."""
    from app.evaluation.claims.run import label_locations, stored_claims

    locations = await label_locations(session, golden, ["requirement"])
    requirements, _ = await stored_claims(session)
    out: dict[uuid.UUID, str] = {}
    for claim in requirements:
        hits = [label for label, locs in locations.items() if claim.matches(locs)]
        if len(hits) == 1:
            out[claim.id] = hits[0]
    return out


def _category(case: MappingCase, retrieved: bool, hit: bool, wrong: bool, extra: bool) -> str:
    if case.expect is Expect.MATERIALS:
        if not retrieved:
            return "retrieval_miss"
        if not hit or wrong:
            return "verifier_mistake"
        return "ground_truth_ambiguity" if extra else "correct"
    if case.expect is Expect.NO_MATERIAL:
        return "verifier_mistake" if (hit or wrong or extra) else "correct"
    return "verifier_mistake" if wrong else "unlabelled_for_review"


async def score(
    sessions: async_sessionmaker[AsyncSession],
    golden: GoldenDataset,
    results: Sequence[MappingRunResult],
) -> dict[str, Any]:
    cases = {c.label_id: c for c in load_split(golden)}
    irrelevant: dict[str, set[str]] = {}
    for label in golden.by_kind(IRRELEVANT_KIND):
        for requirement in label.not_relevant_to:
            irrelevant.setdefault(requirement, set()).add(label.resource)
    outcomes = {o.requirement_id: o for r in results for o in r.outcomes}
    async with sessions() as session:
        labels = await requirement_labels(session, golden)
        targets = await material_spans(session, golden)
        stored = (await session.execute(select(MaterialMapping))).scalars().all()
        ids = {s for o in outcomes.values() for s in o.candidate_span_ids}
        info = await _span_info(session, ids | {m.source_span_id for m in stored})
    rows = []
    for requirement_id, label_id in sorted(labels.items(), key=lambda item: item[1]):
        case, outcome = cases[label_id], outcomes.get(requirement_id)
        if outcome is None:
            continue
        gold = targets.get(label_id, set())
        candidates, selected = set(outcome.candidate_span_ids), set(outcome.selected_span_ids)
        bad = irrelevant.get(label_id, set())
        retrieved_bad = {info[s].resource for s in candidates if info[s].resource in bad}
        wrong = {s for s in selected if info[s].resource in bad}
        gold_resources = {info[s].resource for s in gold if s in info}
        extra = selected - gold
        rows.append(
            {
                "label_id": label_id,
                "split": case.split.value,
                "expect": case.expect.value,
                "candidates": len(candidates),
                "retrieval_hit": bool(gold & candidates) if gold else None,
                "selected": sorted(
                    f"{info[s].resource} v{info[s].version} {info[s].locator}" for s in selected
                ),
                "mapping_hit": bool(gold & selected) if gold else None,
                "irrelevant_retrieved": sorted(retrieved_bad),
                "irrelevant_selected": bool(wrong),
                "outside_target_resources": sorted(
                    {info[s].resource for s in extra} - gold_resources
                ),
                "category": _category(case, bool(gold & candidates), bool(gold & selected),
                                      bool(wrong), bool(extra)),
                "failure": outcome.failure,
            }
        )  # fmt: skip
    subset = all(
        m.source_span_id in set(outcomes[m.requirement_id].candidate_span_ids) for m in stored
    )
    by_label = {row["label_id"]: row for row in rows}
    r2 = by_label.get("aml-cw1-r2-class-imbalance", {})
    aml = [row for row in rows if row["label_id"].startswith("aml-cw1-")]
    acceptance = {
        "class_imbalance_maps_to_lecture_07": bool(r2.get("mapping_hit"))
        and all(s.startswith("lecture-07-class-imbalance ") for s in r2.get("selected", [])),
        "irrelevant_lecture_rejected": bool(aml) and not any(r["irrelevant_selected"] for r in aml),
        "no_material_case_has_no_mapping": all(
            not row["selected"] for row in rows if row["expect"] == Expect.NO_MATERIAL.value
        ),
        "mappings_subset_of_retrieved_candidates": subset,
    }
    return {
        "acceptance": acceptance,
        "passed": all(acceptance.values()),
        "stored_mappings": len(stored),
        "rejected": dict(sum((Counter(r.summary.rejected) for r in results), Counter())),
        "by_split": {
            split.value: [row for row in rows if row["split"] == split.value] for split in Split
        },
    }  # fmt: skip


# --- the run ------------------------------------------------------------------------------


async def run(settings: Any, *, live: bool, embedding_price: Decimal | None,
              spend_cap: Decimal) -> dict[str, Any]:  # fmt: skip
    from app.ai.nebius_embeddings import embedding_space_from_settings
    from app.demo.snapshot import snapshot_subject
    from app.evaluation.assessments.run import BudgetedProvider
    from app.evaluation.claims.run import LimitedProvider
    from app.evaluation.retrieval.eval_db import (
        migrate_eval_database,
        recreate_eval_database,
        require_eval_database,
    )
    from app.evaluation.retrieval.replay import load_replay_provider
    from app.retrieval.search import SearchService

    golden = load_golden()
    space = embedding_space_from_settings(settings)
    database = require_eval_database(
        os.environ, application_database_url=settings.database_url.get_secret_value()
    )
    await asyncio.to_thread(recreate_eval_database, database)
    await asyncio.to_thread(migrate_eval_database, database)
    engine = create_async_engine(database.url)
    closers: list[Any] = []
    started = datetime.now(UTC)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        with tempfile.TemporaryDirectory() as raw:
            courses = await build_initial_state(sessions, settings, Path(raw))
            queries = await query_inputs(sessions, courses)
            inputs = ExpectedInputs((), queries)
            embed_spend = Decimal(0)
            if live:
                embed_spend = await _record_queries(settings, golden, inputs, embedding_price)
            query_replay = load_replay_provider(
                QUERY_SNAPSHOT,
                dataset=snapshot_subject(golden),
                active_space=space,
                max_batch_items=64,
                inputs=inputs,
            )
            search = SearchService(sessions, space, query_replay)
            if live:
                from app.ai.nebius import NebiusTokenFactoryProvider

                inner = NebiusTokenFactoryProvider.from_settings(settings, max_output_tokens=200)
                closers.append(inner)
                model = next(
                    m for m in await inner.list_models() if m.id == inner.model_for(ModelRole.FAST)
                )
                budgeted = BudgetedProvider(
                    inner, model, settings.ai_max_attempts, settings.ai_max_repair_attempts,
                    spend_cap_usd=spend_cap - embed_spend,
                )  # fmt: skip
                provider: Any = LimitedProvider(budgeted, len(queries))
            else:
                recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
                provider = MappingReplayProvider(
                    {item["material_sha256"]: item["output"] for item in recorded}
                )
            service = MaterialMappingService(sessions, search, provider, thinking=False)
            results = [await service.map_course(courses[ref]) for ref in sorted(courses)]
            scored = await score(sessions, golden, results)
        outputs = recorded_outputs(provider.outputs)
        spend = embed_spend + (provider.inner.spent_usd if live else Decimal(0))
        return {
            "name": REPORT_NAME,
            "mode": "live" if live else "replay",
            "started_at": started.isoformat(),
            "dataset_sha256": golden.sha256,
            "queries": len(queries),
            "verifier_calls": len(outputs),
            "spent_usd": str(spend),
            "runs": [r.summary.model_dump(mode="json") | {"status": r.status.value}
                     for r in results],
            **scored,
            "outputs": outputs,
        }  # fmt: skip
    finally:
        for closer in closers:
            await closer.aclose()
        await engine.dispose()


async def _record_queries(
    settings: Any, golden: GoldenDataset, inputs: ExpectedInputs, price: Decimal | None
) -> Decimal:
    from app.ai.nebius_embeddings import NebiusEmbeddingProvider
    from app.demo.snapshot import snapshot_subject
    from app.evaluation.retrieval.record import RecordingRefused, plan_recording, record_snapshot

    if price is None:
        raise RecordingRefused(["--embedding-price-usd-per-million is required with --live"])
    provider = NebiusEmbeddingProvider.from_settings(settings)
    try:
        plan = plan_recording(
            snapshot_subject(golden),
            provider.space,
            batch_max_items=settings.embedding_batch_max_items,
            batch_max_estimated_tokens=settings.embedding_batch_max_estimated_tokens,
            attempts_per_request=settings.ai_max_attempts,
            price_usd_per_million=price,
            output=QUERY_SNAPSHOT,
            inputs=inputs,
        )
        result = await record_snapshot(plan, provider)
    finally:
        await provider.aclose()
    return result.spent_usd


def markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# Material mapping smoke run: {result['name']}",
        "",
        (
            "Northbridge initial state; requirements from recorded FAST outputs; hybrid"
            " retrieval, k=5 chunks, teaching resources only; FAST verifier, thinking off,"
            " candidate IDs only."
        ),
        (
            f"Mode: {result['mode']}. Queries: {result['queries']}. Verifier calls:"
            f" {result['verifier_calls']}. Spend: ${result['spent_usd']}."
        ),
        "",
        "## Demo acceptance",
        "",
        *(f"- {name}: {'PASS' if ok else 'FAIL'}" for name, ok in result["acceptance"].items()),
        "",
        f"Stored mappings: {result['stored_mappings']}. Gate rejections: {result['rejected'] or 'none'}.",
        "",
        "## Cases (smoke, not a benchmark)",
        "",
        "| split | label | expect | retrieval hit | mapping hit | selected | category |",
        "|---|---|---|---|---|---|---|",
    ]
    for split, rows in result["by_split"].items():
        for row in rows:
            lines.append(
                f"| {split} | {row['label_id']} | {row['expect']} | {row['retrieval_hit']} |"
                f" {row['mapping_hit']} | {'; '.join(row['selected']) or '—'} | {row['category']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.evaluation.mapping.run")
    parser.add_argument("--live", action="store_true", help="make the paid calls")
    parser.add_argument("--embedding-price-usd-per-million", type=Decimal)
    parser.add_argument("--spend-cap", type=Decimal, default=MAX_SPEND_USD)
    args = parser.parse_args(argv)
    if args.spend_cap > MAX_SPEND_USD:
        print(json.dumps({"status": "refused", "reason": f"cap above ${MAX_SPEND_USD}"}))
        return 4
    result = asyncio.run(
        run(get_settings(), live=args.live, embedding_price=args.embedding_price_usd_per_million,
            spend_cap=args.spend_cap)
    )  # fmt: skip
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    outputs = result.pop("outputs")
    if args.live:
        RECORDED.write_text(json.dumps(outputs, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8", newline="\n")  # fmt: skip
        report = REPORTS / f"{REPORT_NAME}.json"
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8", newline="\n")  # fmt: skip
        (REPORTS / f"{REPORT_NAME}.md").write_text(markdown(result), encoding="utf-8",
                                                   newline="\n")  # fmt: skip
    print(markdown(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
