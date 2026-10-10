# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""One real, recorded model call through the provider boundary.

    uv run python -m app.ai.smoke_test [--role fast|strong] [--fail]

Uses the local .env (NEBIUS_API_KEY, MODEL_*, DATABASE_URL) and writes one
``model_calls`` row. ``--fail`` requests a model ID that does not exist, to
prove that failures are recorded too.

Exit codes: 0 = validated output, recorded; 1 = failure, recorded;
2 = not configured; 3 = the call's row was not recorded or not found.
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.course_fact import PROMPT_VERSION, CourseFact, build_messages
from app.ai.errors import AIConfigurationError
from app.ai.model_calls import RecordedCallError, call_structured_recorded
from app.ai.nebius import NebiusTokenFactoryProvider
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import ModelCall
from app.db.session import create_db_engine, create_session_factory
from app.domain.enums import ModelRole

# Synthetic, not real course material.
SOURCE_TEXT = (
    "COMP-701 Coursework 1 brief. The written report must be submitted via Moodle "
    "by 23:59 on 31 October 2026. It is worth 40% of the module mark."
)
QUESTION = "What is the submission deadline?"
_MISSING_MODEL = "nvidia/courseflow-smoke-test-model-that-does-not-exist"

EXIT_OK, EXIT_FAILED, EXIT_NOT_CONFIGURED, EXIT_NOT_RECORDED = 0, 1, 2, 3


async def run(role: ModelRole, fail: bool) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        overrides = {"models": dict.fromkeys(ModelRole, _MISSING_MODEL)} if fail else {}
        provider = NebiusTokenFactoryProvider.from_settings(settings, **overrides)
    except AIConfigurationError as exc:
        print(f"NOT CONFIGURED: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        try:
            recorded = await call_structured_recorded(
                provider,
                session_factory,
                messages=build_messages(SOURCE_TEXT, QUESTION),
                schema=CourseFact,
                role=role,
                prompt_version=PROMPT_VERSION,
                temperature=0,
            )
        except RecordedCallError as exc:
            print(f"FAILED ({exc.error.category.value}): {exc.error.detail}")
            if exc.model_call_id is None:
                print(f"model_calls row NOT RECORDED: {exc.record_error!r}")
                return EXIT_NOT_RECORDED
            found = await _print_row(session_factory, exc.model_call_id)
            return EXIT_FAILED if found else EXIT_NOT_RECORDED

        fact = recorded.result.output
        print(f"OK: {fact.model_dump_json()}")
        # Grounding is a separate check from schema validity.
        grounded = fact.evidence_text.rstrip(".") in SOURCE_TEXT
        print(f"evidence_text found verbatim in source: {grounded}")
        found = await _print_row(session_factory, recorded.model_call_id)
        return EXIT_OK if found else EXIT_NOT_RECORDED
    finally:
        await provider.aclose()
        await engine.dispose()


async def _print_row(
    session_factory: async_sessionmaker[AsyncSession], model_call_id: uuid.UUID
) -> bool:
    async with session_factory() as session:
        row = await session.scalar(select(ModelCall).where(ModelCall.id == model_call_id))
    if row is None:
        print(f"model_calls row {model_call_id} NOT FOUND")
        return False
    print(
        f"model_calls row {row.id}: status={row.status.value} model={row.model} "
        f"role={row.model_role} prompt_version={row.prompt_version} attempts={row.attempts} "
        f"repair_rounds={row.repair_rounds} "
        f"tokens(prompt/completion/reasoning)={row.prompt_tokens}/{row.completion_tokens}/"
        f"{row.reasoning_tokens} latency_ms={row.latency_ms} "
        f"error_category={row.error_category} request_id={row.provider_request_id}"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="One real, recorded model call.")
    parser.add_argument("--role", choices=[r.value for r in ModelRole], default="fast")
    parser.add_argument("--fail", action="store_true", help="use a model ID that does not exist")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(ModelRole(args.role), args.fail)))


if __name__ == "__main__":
    main()
