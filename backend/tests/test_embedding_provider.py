# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The embedding contract and NebiusEmbeddingProvider against scripted HTTP (no network)."""

import json
import logging
import math
import os
import subprocess
import sys
import traceback
from pathlib import Path

import httpx2
import openai
import pytest
from pydantic import SecretStr

from app.ai.embeddings import (
    DOCUMENT_INPUT_FORMAT,
    QUERY_INPUT_FORMAT,
    QUERY_TASK_V1,
    QUERY_V1_MAX_CHARS,
    EmbeddingInput,
    EmbeddingSpace,
    check_batch,
    document_input,
    query_input,
)
from app.ai.errors import AIConfigurationError, AIProviderError
from app.ai.nebius_embeddings import NebiusEmbeddingProvider
from app.ai.nebius_transport import PROVIDER_NAME
from app.ai.provider import TokenUsage
from app.core.config import Settings
from app.domain.enums import ModelCallErrorCategory as Category
from app.ingestion.hashing import text_hash
from tests.ai_fakes import (
    API_KEY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    RecordedSleeps,
    ScriptedServer,
    basis_vector,
    connect_error,
    embeddings,
    make_embedding_provider,
    read_timeout,
    status,
)
from tests.embedding_fakes import FakeEmbeddingProvider, hashed_bag_of_words

pytestmark = pytest.mark.anyio

SENSITIVE = "SENSITIVE-QUERY-TEXT"
DOCS = [document_input("Week 3 covers recursion."), document_input("Lab 2: linked lists.")]


async def _embed_error(provider: NebiusEmbeddingProvider, inputs=DOCS) -> AIProviderError:
    with pytest.raises(AIProviderError) as caught:
        await provider.embed(inputs)
    return caught.value


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@h/db_test",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def _raw_vector_body(token: str):
    """Two vectors as raw JSON, the second ending in ``token`` (e.g. NaN):
    httpx2 refuses to encode non-finite floats itself."""
    second = ",".join(["1.0"] * 7 + [token])
    body = (
        '{"object":"list","model":"m","usage":{"prompt_tokens":12,"total_tokens":12},"data":['
        '{"object":"embedding","index":0,"embedding":[1,0,0,0,0,0,0,0]},'
        f'{{"object":"embedding","index":1,"embedding":[{second}]}}]}}'
    )
    return lambda request: httpx2.Response(
        200, content=body.encode(), headers={"content-type": "application/json"}
    )


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# --- input formats and hashing -----------------------------------------------


def test_document_input_is_the_text_unchanged() -> None:
    text = "  Lecture 4 — Étude of graphs\n\n"

    item = document_input(text)

    assert item.text == text
    assert item.input_format == DOCUMENT_INPUT_FORMAT == "document/v1"


def test_input_hash_is_sha256_of_exact_utf8_and_equals_ingestion_text_hash() -> None:
    item = document_input("Café — résumé deadline")

    assert item.input_hash == text_hash(item.text)
    assert len(item.input_hash) == 64


def test_query_input_wraps_the_stripped_question_in_the_versioned_instruction() -> None:
    item = query_input("  When is coursework 1 due?\n")

    assert item.input_format == QUERY_INPUT_FORMAT == "query/v1"
    assert item.text == f"Instruct: {QUERY_TASK_V1}\nQuery:When is coursework 1 due?"


def test_query_and_document_inputs_of_the_same_text_hash_differently() -> None:
    assert query_input("recursion").input_hash != document_input("recursion").input_hash


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_blank_inputs_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        document_input(text)
    with pytest.raises(ValueError, match="empty"):
        query_input(text)


def test_overlong_query_is_rejected() -> None:
    query_input("x" * QUERY_V1_MAX_CHARS)
    with pytest.raises(ValueError, match="longer than"):
        query_input("x" * (QUERY_V1_MAX_CHARS + 1))


@pytest.mark.parametrize(
    "fields",
    [
        {"provider": "", "model": "m", "dimensions": 8, "space_version": "v1"},
        {"provider": "p", "model": "", "dimensions": 8, "space_version": "v1"},
        {"provider": "p", "model": "m", "dimensions": 8, "space_version": ""},
        {"provider": "p", "model": "m", "dimensions": 0, "space_version": "v1"},
    ],
)
def test_embedding_space_rejects_incomplete_identity(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EmbeddingSpace(**fields)  # type: ignore[arg-type]


def test_spaces_differ_by_space_version_alone() -> None:
    assert EmbeddingSpace("p", "m", 8, "v1") != EmbeddingSpace("p", "m", 8, "v2")


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ([], "empty"),
        ([document_input("a")] * 3, "limit is 2"),
        ([document_input("a"), EmbeddingInput(" ", DOCUMENT_INPUT_FORMAT)], "input 1 is empty"),
    ],
)
def test_check_batch_rejects_malformed_batches(inputs: list[EmbeddingInput], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_batch(inputs, max_items=2)


# --- success and request shape -----------------------------------------------


async def test_request_sends_exact_inputs_explicit_dimensions_and_float_encoding() -> None:
    server = ScriptedServer([embeddings()])
    inputs = [query_input("When is CW1 due?"), *DOCS]

    await make_embedding_provider(server).embed(inputs)

    (request,) = server.requests
    assert request.url.path.endswith("/embeddings")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert server.bodies()[0] == {
        "model": EMBEDDING_MODEL,
        "input": [item.text for item in inputs],
        "dimensions": EMBEDDING_DIMENSIONS,
        "encoding_format": "float",
    }


async def test_success_returns_vectors_in_input_order_with_usage_and_identity() -> None:
    server = ScriptedServer([embeddings(prompt_tokens=17, request_id="req-e1")])
    provider = make_embedding_provider(server)

    result = await provider.embed(DOCS)

    assert result.vectors == (
        tuple(basis_vector(0, EMBEDDING_DIMENSIONS)),
        tuple(basis_vector(1, EMBEDDING_DIMENSIONS)),
    )
    assert result.space == EmbeddingSpace(
        PROVIDER_NAME, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, "v1"
    )
    assert result.space is provider.space
    assert result.usage == TokenUsage(prompt_tokens=17, completion_tokens=0)
    assert result.attempts == 1
    assert result.request_id == "req-e1"
    assert result.served_model == EMBEDDING_MODEL


async def test_served_model_is_telemetry_only_and_never_changes_the_space() -> None:
    server = ScriptedServer([embeddings(served_model="vendor/embedding-model-2026-10")])
    provider = make_embedding_provider(server)

    result = await provider.embed(DOCS)

    assert result.served_model == "vendor/embedding-model-2026-10"
    assert result.space.model == EMBEDDING_MODEL


async def test_vectors_are_placed_by_index_not_list_order() -> None:
    server = ScriptedServer([embeddings(order=[1, 0])])

    result = await make_embedding_provider(server).embed(DOCS)

    assert result.vectors[0] == tuple(basis_vector(0, EMBEDDING_DIMENSIONS))
    assert result.vectors[1] == tuple(basis_vector(1, EMBEDDING_DIMENSIONS))


async def test_missing_usage_is_unknown_not_zero() -> None:
    server = ScriptedServer([embeddings(usage=False)])

    result = await make_embedding_provider(server).embed(DOCS)

    assert result.usage == TokenUsage()


async def test_integer_components_are_accepted_as_floats() -> None:
    server = ScriptedServer([embeddings([[1, 0, 0, 0, 0, 0, 0, 2], basis_vector(1, 8)])])

    result = await make_embedding_provider(server).embed(DOCS)

    assert result.vectors[0] == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0)
    assert all(isinstance(value, float) for value in result.vectors[0])


# --- invalid output: classified, never retried --------------------------------


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (embeddings([basis_vector(0, 8)]), "expected 2 vectors, got 1"),
        (embeddings(indices=[0, 0]), "indices are not exactly 0..1"),
        (embeddings(indices=[1, 2]), "indices are not exactly 0..1"),
        (embeddings([basis_vector(0, 8), [1.0] * 7]), "vector 1 has 7 dimensions; requested 8"),
        (embeddings([basis_vector(0, 8), [0.0] * 8]), "vector 1 is all zeros"),
        (embeddings([basis_vector(0, 8), [1.0] * 7 + [None]]), "non-finite or non-numeric"),
        (embeddings([basis_vector(0, 8), [1.0] * 7 + ["1"]]), "non-finite or non-numeric"),
        # Python's JSON parser accepts these tokens, so a provider can send them.
        (_raw_vector_body("NaN"), "non-finite"),
        (_raw_vector_body("Infinity"), "non-finite"),
        (_raw_vector_body("-Infinity"), "non-finite"),
        (embeddings([basis_vector(0, 8), "AAAAAA=="]), "vector 1 is not a list of numbers"),
    ],
)
async def test_invalid_vectors_fail_as_invalid_output_without_retry(step, message: str) -> None:
    server = ScriptedServer([step])

    error = await _embed_error(make_embedding_provider(server))

    assert error.category is Category.INVALID_OUTPUT
    assert message in error.detail
    assert not error.retriable
    assert error.attempts == 1
    assert len(server.requests) == 1
    assert error.usage.prompt_tokens == 12  # the spend is kept for the record


async def test_non_embedding_response_is_unexpected() -> None:
    server = ScriptedServer(
        [
            lambda r: httpx2.Response(
                200, text="<html>proxy</html>", headers={"content-type": "text/html"}
            )
        ]
    )

    error = await _embed_error(make_embedding_provider(server))

    assert error.category is Category.UNEXPECTED
    assert "not an embedding list" in error.detail


# --- transport: shared classification and bounded retries ---------------------


async def test_transient_failure_is_retried_then_succeeds() -> None:
    sleeps = RecordedSleeps()
    server = ScriptedServer([status(503), embeddings()])

    result = await make_embedding_provider(server, sleeps).embed(DOCS)

    assert result.attempts == 2
    assert sleeps.delays == [1.0]
    assert server.bodies()[0] == server.bodies()[1]  # the identical request is resent


async def test_rate_limit_honours_retry_after() -> None:
    sleeps = RecordedSleeps()
    server = ScriptedServer([status(429, headers={"retry-after": "2"}), embeddings()])

    await make_embedding_provider(server, sleeps).embed(DOCS)

    assert sleeps.delays == [2.0]


@pytest.mark.parametrize(
    ("step", "category"),
    [(read_timeout(), Category.TIMEOUT), (connect_error(), Category.CONNECTION)],
)
async def test_transient_failures_stop_at_max_attempts(step, category: Category) -> None:
    server = ScriptedServer([step, step, step])

    error = await _embed_error(make_embedding_provider(server, max_attempts=3))

    assert error.category is category
    assert error.attempts == 3
    assert error.model == EMBEDDING_MODEL


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (401, Category.AUTHENTICATION),
        (404, Category.INVALID_REQUEST),
        (422, Category.INVALID_REQUEST),
    ],
)
async def test_deterministic_failures_are_not_retried(code: int, category: Category) -> None:
    server = ScriptedServer([status(code)])

    error = await _embed_error(make_embedding_provider(server))

    assert error.category is category
    assert error.attempts == 1
    assert f"HTTP {code}" in error.detail


# --- no input text or key in errors and logs ----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"detail": f"Input '{SENSITIVE}' exceeds the context window"},
        {"message": f"bad input {SENSITIVE}"},
        {"detail": [{"loc": ["body", "input", 0], "msg": f"bad {SENSITIVE}", "input": SENSITIVE}]},
    ],
)
async def test_provider_messages_that_could_echo_input_are_dropped(
    caplog: pytest.LogCaptureFixture, body: dict[str, object]
) -> None:
    caplog.set_level(logging.DEBUG)
    server = ScriptedServer([status(400, body=body)])

    error = await _embed_error(make_embedding_provider(server), [query_input(SENSITIVE)])

    assert error.detail.startswith("BadRequestError: HTTP 400")
    assert SENSITIVE not in error.detail
    assert SENSITIVE not in caplog.text
    assert SENSITIVE not in "".join(traceback.format_exception(error))


async def test_validation_locations_are_kept_without_messages() -> None:
    body = {"detail": [{"loc": ["body", "dimensions"], "msg": "too large", "input": 99999}]}
    server = ScriptedServer([status(422, body=body)])

    error = await _embed_error(make_embedding_provider(server))

    assert error.detail == "UnprocessableEntityError: HTTP 422 - body.dimensions"


async def test_input_text_never_reaches_logs_on_success_or_invalid_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    inputs = [query_input(SENSITIVE)]
    server = ScriptedServer([embeddings(), embeddings([[0.0] * 8])])
    provider = make_embedding_provider(server)

    await provider.embed(inputs)
    await _embed_error(provider, inputs)

    assert SENSITIVE not in caplog.text
    assert [r.getMessage() for r in caplog.records if r.name == "app.ai.nebius_embeddings"] == [
        "ai.attempt_succeeded",
        "ai.attempt_succeeded",
        "ai.output_invalid",
    ]


async def test_api_key_never_appears_in_error_detail() -> None:
    server = ScriptedServer([status(401, body={"detail": f"invalid key {API_KEY}"})])

    error = await _embed_error(make_embedding_provider(server))

    assert API_KEY not in error.detail
    assert API_KEY not in str(error)


# --- malformed batches never reach the network --------------------------------


@pytest.mark.parametrize(
    "inputs",
    [[], [document_input("a")] * 5, [EmbeddingInput("", DOCUMENT_INPUT_FORMAT)]],
)
async def test_malformed_batch_raises_before_any_request(inputs: list[EmbeddingInput]) -> None:
    server = ScriptedServer([])

    with pytest.raises(ValueError):
        await make_embedding_provider(server, max_batch_items=4).embed(inputs)

    assert server.requests == []


# --- configuration -------------------------------------------------------------


def test_from_settings_names_every_missing_value() -> None:
    with pytest.raises(AIConfigurationError) as caught:
        NebiusEmbeddingProvider.from_settings(_settings())

    for name in ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "EMBEDDING_MODEL"):
        assert name in str(caught.value)
    assert "MODEL_FAST" not in str(caught.value)  # chat models are not needed


async def test_from_settings_wires_space_batch_limit_and_reliability_bounds() -> None:
    settings = _settings(
        nebius_api_key=SecretStr("k"),
        nebius_base_url="https://example.test/v1/",
        embedding_model="q/embed",
        embedding_dimensions=8,
        embedding_space_version="v7",
        embedding_batch_max_items=3,
        ai_max_attempts=2,
    )
    server = ScriptedServer([status(503), status(503)])
    provider = NebiusEmbeddingProvider.from_settings(
        settings,
        http_client=openai.DefaultAsyncHttpx2Client(transport=httpx2.MockTransport(server)),
        sleep=RecordedSleeps(),
        rand=lambda: 1.0,
    )

    assert provider.space == EmbeddingSpace(PROVIDER_NAME, "q/embed", 8, "v7")
    assert provider.max_batch_items == 3
    error = await _embed_error(provider)
    assert error.attempts == 2
    assert server.bodies()[0]["dimensions"] == 8


# --- the offline fake used by indexing and search tests -------------------------


async def test_fake_provider_is_deterministic_and_word_overlap_raises_similarity() -> None:
    fake = FakeEmbeddingProvider(dimensions=64)
    texts = ["recursion and base cases", "recursion base case examples", "lab safety rules"]

    first = await fake.embed([document_input(t) for t in texts])
    second = await fake.embed([document_input(t) for t in texts])

    assert first.vectors == second.vectors
    assert all(len(v) == 64 for v in first.vectors)
    assert _cosine(first.vectors[0], first.vectors[1]) > _cosine(first.vectors[0], first.vectors[2])
    assert fake.embedded_texts == texts * 2


async def test_fake_provider_enforces_batches_and_raises_scripted_failures() -> None:
    failure = AIProviderError(
        Category.RATE_LIMITED, "scripted", model="fake", attempts=3, usage=TokenUsage()
    )
    fake = FakeEmbeddingProvider(max_batch_items=2, failures=[failure])

    with pytest.raises(ValueError):
        await fake.embed([document_input("a")] * 3)
    with pytest.raises(AIProviderError) as caught:
        await fake.embed([document_input("a")])
    result = await fake.embed([document_input("a")])

    assert caught.value is failure
    assert len(fake.calls) == 2  # the rejected batch never counts as a call
    assert result.attempts == 1


# --- safe request correlation ---------------------------------------------------


async def test_request_id_is_kept_when_a_status_error_body_is_dropped() -> None:
    body = {"detail": f"bad input {SENSITIVE}"}
    server = ScriptedServer([status(400, body=body, headers={"x-request-id": "req-400"})])

    error = await _embed_error(make_embedding_provider(server), [query_input(SENSITIVE)])

    assert error.request_id == "req-400"
    assert SENSITIVE not in error.detail


async def test_request_id_is_kept_on_invalid_output_and_after_retries() -> None:
    invalid = ScriptedServer([embeddings([[0.0] * 8, [0.0] * 8], request_id="req-bad")])
    exhausted = ScriptedServer(
        [status(503, headers={"x-request-id": f"req-503-{n}"}) for n in (1, 2)]
    )

    invalid_error = await _embed_error(make_embedding_provider(invalid))
    exhausted_error = await _embed_error(make_embedding_provider(exhausted, max_attempts=2))

    assert invalid_error.request_id == "req-bad"
    assert exhausted_error.request_id == "req-503-2"  # the last attempt's


# --- the fake is stable across processes ----------------------------------------

_FAKE_VECTOR_SCRIPT = """
import json, sys
from tests.embedding_fakes import hashed_bag_of_words
print(json.dumps([hashed_bag_of_words(t, 64) for t in json.loads(sys.argv[1])]))
"""


@pytest.mark.parametrize("seed", ["0", "1", "4242"])
def test_fake_vectors_are_identical_across_processes_and_hash_seeds(seed: str) -> None:
    """Bucket placement uses SHA-256, not hash(): str hashing is salted per
    process, which would make 'deterministic' fixtures differ between runs."""
    texts = ["Recursion and base cases", "Lab 2: linked lists", "étude — graphs"]
    backend = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-c", _FAKE_VECTOR_SCRIPT, json.dumps(texts)],
        cwd=backend,
        env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(backend)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    in_process = [list(hashed_bag_of_words(t, 64)) for t in texts]
    assert json.loads(completed.stdout) == in_process
