# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The assessment extraction evaluation without a model: the plan, the golden
expectations, the spend guard and the thinking switch."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.ai.errors import AIProviderError
from app.ai.provider import ChatMessage, ModelInfo, StructuredResult, TokenUsage
from app.domain.enums import ModelCallErrorCategory, ModelRole
from app.evaluation.assessments.run import (
    MAX_SPEND_USD,
    BudgetedProvider,
    BudgetExceeded,
    call_worst_case_usd,
    expected_deadlines,
    plan_units,
)
from app.evaluation.northbridge.labels import load_golden
from app.extraction.gate import injection_fragments
from app.extraction.schema import AssessmentExtraction
from app.retrieval.chunking import estimate_tokens
from tests.ai_fakes import ScriptedServer, completion, make_provider

pytestmark = pytest.mark.anyio

INFO = ModelInfo("m", None, Decimal("0.06"), Decimal("0.24"))
EMPTY = AssessmentExtraction(assessments=[])
MESSAGES = [ChatMessage("system", "rules"), ChatMessage("user", "material")]
ESTIMATED = sum(estimate_tokens(m.content) for m in MESSAGES)


def test_the_plan_is_every_version_once_plus_the_injection_control() -> None:
    planned = plan_units(load_golden())
    assert len(planned) == 13
    assert [p.where[2] for p in planned].count(2) == 2  # the two later versions
    control = planned[-1]
    assert control.where == ("msd7000", "research-methods-lecture", 1)
    assert not any(injection_fragments(s.excerpt) for s in control.unit.spans)
    assert any(injection_fragments(s.excerpt) for p in planned[:-1] for s in p.unit.spans)


def test_the_golden_deadline_facts() -> None:
    expected = expected_deadlines(load_golden())
    assert len(expected) == 6
    assert ("aml7001", "module-handbook", 1, "coursework 1", "2026-11-13T23:59",
            "Europe/London") in expected  # fmt: skip


class _Inner:
    name = "inner"

    def __init__(self, usage: TokenUsage, error: bool = False) -> None:
        self.usage, self.error, self.calls = usage, error, 0
        self.thinking: Any = "unset"

    def model_for(self, role: ModelRole) -> str:
        return "m"

    async def list_models(self) -> list[ModelInfo]:
        return [INFO]

    async def chat_structured(
        self, messages: Any, schema: Any, *, role: Any, **options: Any
    ) -> Any:
        self.calls += 1
        self.thinking = options.get("thinking", "unset")
        if self.error:
            raise AIProviderError(
                ModelCallErrorCategory.TRUNCATED, "length", model="m", attempts=1, usage=self.usage
            )
        return StructuredResult(output=EMPTY, model="m", usage=self.usage, attempts=1)


async def test_the_spend_guard_refuses_a_call_unsent() -> None:
    inner = _Inner(TokenUsage(prompt_tokens=1000, completion_tokens=2000))
    guarded = BudgetedProvider(inner, INFO, attempts=3, repair_rounds=1)  # type: ignore[arg-type]
    worst = call_worst_case_usd(ESTIMATED, INFO, 3, 1)
    guarded.spent_usd = MAX_SPEND_USD - worst  # exactly at the edge
    with pytest.raises(BudgetExceeded):
        await guarded.chat_structured(MESSAGES, AssessmentExtraction, role=ModelRole.FAST)
    assert inner.calls == 0


async def test_spend_is_charged_from_usage_or_the_worst_case() -> None:
    reported = BudgetedProvider(_Inner(TokenUsage(1000, 2000)), INFO, 3, 1)  # type: ignore[arg-type]
    await reported.chat_structured(
        MESSAGES, AssessmentExtraction, role=ModelRole.FAST, thinking=False
    )
    assert reported.spent_usd == Decimal("0.00054")  # 1000 x 0.06 + 2000 x 0.24, per million
    assert reported.inner.thinking is False  # type: ignore[attr-defined]

    failed = BudgetedProvider(_Inner(TokenUsage(), error=True), INFO, 3, 1)  # type: ignore[arg-type]
    with pytest.raises(AIProviderError):
        await failed.chat_structured(MESSAGES, AssessmentExtraction, role=ModelRole.FAST)
    assert failed.spent_usd == call_worst_case_usd(
        ESTIMATED, INFO, 3, 1
    )  # unreported: the worst case
    assert failed.inner.thinking == "unset"  # type: ignore[attr-defined]


@pytest.mark.parametrize(("thinking", "sent"), [(None, None), (False, {"enable_thinking": False})])
async def test_the_thinking_switch_reaches_the_request(thinking: bool | None, sent: Any) -> None:
    server = ScriptedServer([completion(json.dumps({"assessments": []}))])
    provider = make_provider(server)
    await provider.chat_structured(
        MESSAGES, AssessmentExtraction, role=ModelRole.FAST, thinking=thinking
    )
    (body,) = server.bodies()
    assert body.get("chat_template_kwargs") == sent


def test_replay_finds_each_recorded_output_its_unit() -> None:
    from app.ai.prompts import load_prompt
    from app.evaluation.assessments.run import (
        ReplayAmbiguous,
        assign_outputs,
        material_of,
    )
    from app.extraction.units import build_messages, render_material

    planned = plan_units(load_golden())
    (_, user) = build_messages(load_prompt("assessment-extraction/v1"), planned[0].unit)
    assert material_of(user.content) == render_material(planned[0].unit)  # nonce-free

    empty: dict[str, Any] = {"output": {"assessments": []}}
    assigned = assign_outputs([empty] * len(planned), planned)
    assert len(assigned) == len({render_material(p.unit) for p in planned})
    with pytest.raises(ReplayAmbiguous, match="outputs for"):
        assign_outputs([empty], planned)


def test_replay_keys_v1_and_v2_messages_alike() -> None:
    from app.ai.prompts import load_prompt
    from app.evaluation.assessments.run import spans_of
    from app.extraction.units import build_messages, render_material

    unit = plan_units(load_golden())[0].unit
    for version in ("assessment-extraction/v1", "assessment-extraction/v2"):
        (_, user) = build_messages(load_prompt(version), unit)
        assert spans_of(user.content) == render_material(unit), version
