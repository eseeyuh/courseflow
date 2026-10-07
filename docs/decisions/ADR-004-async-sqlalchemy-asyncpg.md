# ADR-004 — Async SQLAlchemy 2.x with the asyncpg driver for all database access

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The CourseFlow API is a FastAPI service (see [ADR-001](ADR-001-modular-monolith.md)) on PostgreSQL + pgvector (see [ADR-002](ADR-002-postgres-pgvector.md)). Most of its work is I/O-bound waiting rather than computation:

- LLM calls to an external, OpenAI-compatible inference API take seconds each, and the analysis workflow fans out many of them (extraction, verification, mapping, dependency and conflict reasoning).
- Retrieval issues several database queries per requirement (vector, lexical, metadata filters).
- The stateful workflow engine and the HTTP client are asyncio-native.

SQLAlchemy can be used synchronously (each request handled in a worker thread) or with its asyncio extension (`AsyncEngine`/`AsyncSession`). The database driver must match that choice. Development happens on Windows, and CI and deployment run on Linux, so the driver has to work on both. The choice affects every module that touches the database, plus the test fixtures and migrations, so it is expensive to change later.

## Decision

- **All application database access uses SQLAlchemy 2.x's asyncio extension.** One `AsyncEngine` (with its connection pool) is created per process in the FastAPI lifespan and disposed on shutdown. One `AsyncSession` per request is provided through a FastAPI `yield` dependency and is always closed.
- **Driver: asyncpg** (`postgresql+asyncpg://`). Settings reject any other `DATABASE_URL` scheme at startup.
- **Sessions use `expire_on_commit=False`.** ORM relationships use `lazy="raise"`. Related objects must be loaded explicitly (`selectinload`, `joinedload`) or queried directly.
- **Alembic runs through the same async engine** (async `env.py`, `connection.run_sync(...)`), so migrations, the application and the tests share one driver and one URL format.
- **Sync and async access are not mixed.** A synchronous code path needs a documented reason and an amendment to this ADR.

## Alternatives considered

| Alternative | Why not (for this version) |
|---|---|
| **Synchronous SQLAlchemy** (psycopg 3 or psycopg2, `def` routes in FastAPI's thread pool) | The simplest model: implicit lazy loading, plain tests, plain Alembic. But the I/O-heavy LLM workflow is async, so every database call would hop between the event loop and a thread pool. Concurrency would then be capped by the thread-pool size, and there would be two concurrency models in one codebase. Converting later would mean rewriting every repository function and test. |
| **Async SQLAlchemy + psycopg 3 (async mode)** | One driver for sync and async, and well maintained. But psycopg's async mode is not compatible with Windows' default `ProactorEventLoop`: the event loop policy would have to be changed in the app, the tests and Alembic, which is fragile on the developer platform. |
| **asyncpg directly, without SQLAlchemy** | Fastest and simplest at the driver level, but we would lose the ORM, Alembic autogenerate drafts, typed `Mapped[...]` models and the SQL expression language. We would be hand-writing all SQL and the migrations. |
| **SQLModel** | Merges Pydantic and ORM models into one class. CourseFlow deliberately keeps API schemas separate from ORM models, so the main benefit doesn't apply, and it adds a layer between us and SQLAlchemy 2.x features. |

## Consequences

**Positive**

- One concurrency model end to end: an HTTP request, an LLM call and a database query can all be awaited on the same event loop.
- Pool and lifecycle are explicit and testable: tests assert that connections are returned to the pool after every request.
- `lazy="raise"` makes every database round trip visible in code. That prevents accidental N+1 queries and hidden I/O.
- asyncpg works the same on the Windows host and in Linux containers.

**Negative / risks**

- **No implicit lazy loading.** Touching an unloaded relationship raises an error instead of querying. *Mitigation:* `lazy="raise"` makes this fail loudly in tests, and queries state their loading strategy explicitly.
- **More rules to learn:** `expire_on_commit=False`, `refresh()` instead of expiration, and that an `AsyncSession` must never be shared across concurrent tasks. *Mitigation:* one session per request through the dependency; documented in the data layer.
- **Test complexity:** async engines are bound to the event loop that created them. *Mitigation:* each test creates its own app and engine. Session-level setup (creating and migrating the test database) runs in its own short `asyncio.run`.
- **CPU-bound work blocks the loop.** Heavy parsing or embedding inside a request would stall every other request. *Mitigation:* long-running work moves to background jobs when the async job runner is introduced.
- **asyncpg specifics:** its own exception types (wrapped by SQLAlchemy) and server-side prepared statements. These can conflict with transaction-mode connection poolers such as PgBouncer. *Mitigation:* no pooler is used in v0.1.0; revisit if one is added.

## Revisit when

- Profiling shows the per-request overhead of async ORM materially affects p95 latency compared to a sync baseline under realistic load, **or**
- A transaction-mode connection pooler (e.g. PgBouncer) or a managed Postgres proxy is adopted and asyncpg's prepared-statement behaviour causes errors that configuration can't fix, **or**
- Most database work moves into background workers that are synchronous by design, making a dual (sync worker / async API) data layer simpler than a single async one, **or**
- psycopg's async mode becomes Windows-compatible by default and consolidating on one driver for sync tooling becomes valuable.
