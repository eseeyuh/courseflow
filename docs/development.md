# CourseFlow development guide

The full local development stack. For the judge demo only, see the [README](../README.md#run-the-demo).

The local stack is PostgreSQL + pgvector, a one-shot migration job and the API, all in Docker Compose. The web frontend runs on the host for fast reloads.

```text
browser ──► Next.js (host, :3000) ──► FastAPI (Docker, :8000) ──► PostgreSQL + pgvector (Docker, :5432)
```

| Path | Contents |
|---|---|
| `backend/` | FastAPI app (`app/`), Alembic migrations (`alembic/`), tests (`tests/`) |
| `frontend/` | Next.js App Router web app |
| `docker-compose.yml` | `db` → `migrate` → `api`; volumes `pgdata` (database) and `rawdata` (original source files) |

## Prerequisites

- **Docker** with Compose v2 (Docker Desktop or Docker Engine)
- **[uv](https://docs.astral.sh/uv/)**. It installs the pinned Python 3.12 (`backend/.python-version`) automatically.
- **Node.js ≥ 20.9** with npm

All ports are published on `127.0.0.1` only.

## 1. Configure

```bash
cp .env.example .env                              # API, database, CORS
cp frontend/.env.example frontend/.env.local      # browser-visible API URL
```

On Windows PowerShell, use `Copy-Item` instead of `cp`. The defaults work for local development as-is. The database password in `.env.example` is a **development-only** value; use real secrets anywhere reachable from a network.

`NEXT_PUBLIC_*` values are compiled into the JavaScript sent to browsers, so they are public. Never put a secret in one.

AI features need a Nebius Token Factory API key: set `NEBIUS_API_KEY` in your local `.env` only. Everything else runs without it. Embeddings use `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` and `EMBEDDING_SPACE_VERSION` (see `.env.example`); Docker Compose passes these and the Nebius settings to the API container only. The model IDs in `.env.example` were verified on the date recorded in [docs/experiments/model-catalog.md](experiments/model-catalog.md).

## 2. Install dependencies

From the repository root:

```bash
(cd backend && uv sync --locked)   # Python 3.12 + exact versions from uv.lock into backend/.venv
(cd frontend && npm ci)            # exact versions from package-lock.json
```

## 3. Start the stack

```bash
docker compose up -d --build --wait
docker compose ps -a               # db healthy, migrate "Exited (0)", api healthy
```

Startup order: `db` becomes healthy → `migrate` runs `alembic upgrade head` and exits → `api` starts. If the migration fails, the API does not start (`docker compose logs migrate` shows why).

## 4. Start the frontend

```bash
cd frontend
npm run dev
```

## Service URLs

| Service | URL |
|---|---|
| Web frontend | http://localhost:3000 |
| API | http://127.0.0.1:8000 |
| Interactive API docs | http://127.0.0.1:8000/docs |
| Liveness (process up, no dependency checks) | http://127.0.0.1:8000/health/live |
| Readiness (database reachable, else 503) | http://127.0.0.1:8000/health/ready |
| Versioned status for clients | http://127.0.0.1:8000/api/v1/health |
| Courses (temporary listing) | http://127.0.0.1:8000/api/v1/courses |
| PostgreSQL | `127.0.0.1:5432`, database `courseflow` |

## Importing a document

Ingestion runs **inside the API container**, which owns the one raw-source store (the `rawdata` volume). Host-side imports are refused on purpose (`RAW_STORAGE_DIR` is empty in `.env`). Details: [docs/architecture/ingestion.md](architecture/ingestion.md).

Until the demo dataset importer exists, create a course to import into:

```bash
docker compose exec db psql -U courseflow -d courseflow -tAc   "WITH i AS (INSERT INTO institutions (name, lms_type) VALUES ('Northbridge University', 'upload') RETURNING id)
   INSERT INTO courses (institution_id, title) SELECT id, 'Data Systems' FROM i RETURNING id"
```

Then import a file (the folder is mounted read-only), and re-check what was stored:

```bash
docker compose run --rm --no-deps -v "$PWD/backend/tests/fixtures/ingestion:/input:ro" api   python -m app.ingestion.cli import /input/handbook.pdf --course-id <course-uuid> --type handbook
docker compose run --rm --no-deps api python -m app.ingestion.cli verify <version-uuid>
```

`import` prints one JSON result: `created` or `unchanged`, the version, hashes, and span counts. Importing the same bytes again creates nothing. On Windows PowerShell, use `${PWD}` in the volume path; in Git Bash, prefix the command with `MSYS_NO_PATHCONV=1`.

## Indexing and searching a course

Retrieval also runs in the API container. `index` chunks the course's current versions and embeds what is not cached yet (it needs `NEBIUS_API_KEY`); re-running it on unchanged content makes no provider calls. `status` and `stats` are offline and read-only: they need neither the API key nor the network.

```bash
docker compose run --rm --no-deps api python -m app.retrieval.cli index  --course-id <course-uuid>
docker compose run --rm --no-deps api python -m app.retrieval.cli status --course-id <course-uuid>
docker compose run --rm --no-deps api python -m app.retrieval.cli stats  --course-id <course-uuid>
docker compose run --rm --no-deps api python -m app.retrieval.cli search --course-id <course-uuid> --query "When is the coursework report deadline?" -k 5
docker compose run --rm --no-deps api python -m app.retrieval.cli search --course-id <course-uuid> --query "..." --type brief --json
docker compose run --rm --no-deps api python -m app.retrieval.cli search --course-id <course-uuid> --query "EC7 form" --mode lexical
```

`search` ranks only fully indexed, current versions of the course (an inconsistent index makes it refuse, exit code 6) and prints each hit's score, resource, version, chunk ID and the exact source spans it came from. The score ranks results; it is not a confidence. Use `--query -` to read the question from stdin (with `docker compose run -T`), which keeps it out of shell history. The default mode is `hybrid` (vector and full-text rankings fused by reciprocal rank fusion); `--mode vector` and `--mode lexical` select one ranker. Hybrid and vector searches make one recorded embedding call each (query vectors are not cached); lexical search makes none and needs no API key. Details: [docs/architecture/retrieval.md](architecture/retrieval.md).

A live end-to-end check with invented courses, and the development measurements:

```bash
docker compose run --rm --no-deps api python -m app.retrieval.smoke_test            # reruns reuse its records
docker compose run --rm --no-deps api python -m app.retrieval.measure throughput --plan-only
docker compose run --rm --no-deps api python -m app.retrieval.measure chunker
```

## Database migrations

Compose applies migrations automatically. From `backend/` you can also run them against the database in `.env`:

```bash
uv run alembic upgrade head        # apply all migrations
uv run alembic current             # show the applied revision
uv run alembic check               # fail if the ORM models and the database schema differ
uv run alembic revision --autogenerate -m "describe change"   # draft only: always review by hand
```

Schema changes go through migrations only. Autogenerate does not handle extensions, CHECK constraints or cyclic foreign keys correctly, so every generated revision must be reviewed.

## Tests and checks

Backend (from `backend/`, with the database running: `docker compose up -d db`):

```bash
uv run pytest                      # unit, API, schema and migration tests
uv run ruff check .                # lint
uv run ruff format --check .       # formatting (use `ruff format .` to apply)
```

The tests never call a real model: provider tests run the real `openai` SDK against a scripted transport. To make one real, recorded model call (needs `NEBIUS_API_KEY`; writes a row to `model_calls`):

```bash
uv run python -m app.ai.smoke_test --role fast    # or --role strong; --fail records a failure
```

Tests use a separate database, `courseflow_test`, on the same server. It is created and migrated automatically. Tests refuse any database whose name does not end in `_test`. Set `TEST_DATABASE_URL` to use a different one.

Frontend (from `frontend/`):

```bash
npm run lint
npm run typecheck
npm run build
```

## Running the API on the host (optional)

For auto-reload while working on the backend:

```bash
docker compose stop api            # frees port 8000; db stays up
cd backend
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

## Troubleshooting

- **Database password changes have no effect / "password authentication failed".** PostgreSQL applies `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` **only when its data volume is first created**. Changing them later leaves the old credentials in place, so the API and `migrate` can no longer log in. Either restore the original values in `.env`, or recreate the volume with `docker compose down -v`. **This deletes all local database data.**
- **Frontend shows "API unreachable".** Check that the API is running (`docker compose ps`) and that `NEXT_PUBLIC_API_BASE_URL` in `frontend/.env.local` points to it. Also check that `CORS_ALLOWED_ORIGINS` in `.env` matches the address in your browser exactly: `http://localhost:3000` and `http://127.0.0.1:3000` are different origins. Restart `npm run dev` after editing `.env.local`, and `docker compose up -d` after editing `.env`.
- **Frontend shows "API reachable but not ready (HTTP 503)".** The API is up but cannot reach PostgreSQL: `docker compose ps db`, `docker compose logs db`.
- **`RAW_STORAGE_DIR is not set` when importing.** You ran the CLI on the host. Run it in the container with `docker compose run --rm --no-deps api python -m app.ingestion.cli ...` (see [Importing a document](#importing-a-document)).
- **Import fails with `"error": "raw_storage"`** and a detail such as `raw object could not be written (EACCES)`. Check the store with `docker compose run --rm --no-deps api python -m app.ingestion.cli check-store write`. A `rawdata` volume created by an older image can have the wrong owner. Recreating it (`docker compose down`, then `docker volume rm courseflow_rawdata`) **deletes the stored original files** while their database rows remain, so only do that together with a database reset.
- **Port already in use.** Change `POSTGRES_HOST_PORT` / `API_HOST_PORT` in `.env` and update `DATABASE_URL`, `NEXT_PUBLIC_API_BASE_URL` and `CORS_ALLOWED_ORIGINS` to match.

## Demo data

All demo content is **fully synthetic**: a fictional "Northbridge University" with two courses ([demo_data/README.md](../demo_data/README.md)). The repository contains no real university material and no student data. Seed it, imported and indexed, with no API key:

```bash
docker compose run --rm api python -m app.demo.seed --reset [--state initial|final]
```
