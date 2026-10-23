#!/usr/bin/env sh
# Run the CourseFlow API on the host for the offline demo, against its own
# database (courseflow_demo, in the Compose Postgres) and its own raw store
# (storage/demo-raw, git-ignored). Migrates first. No model key is needed:
# the demo replays committed recordings.
#
#     sh scripts/demo-api.sh            # http://127.0.0.1:8000 (RELOAD=1 to reload on edits)
#
# Database credentials come from the repository-root .env (DATABASE_URL);
# only the database name is replaced.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
url="$(grep '^DATABASE_URL=' "$root/.env" | cut -d= -f2- | sed -E 's#/[^/]+$#/courseflow_demo#; s#@db:#@127.0.0.1:#')"
mkdir -p "$root/storage/demo-raw"
cd "$root/backend"
export DATABASE_URL="$url"
export RAW_STORAGE_DIR="$root/storage/demo-raw"
export APP_ENV="${APP_ENV:-development}"
export CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-http://localhost:3000}"
# The demo's embedding space (the committed snapshots'), not a live provider.
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-Qwen/Qwen3-Embedding-8B}"
uv run alembic upgrade head
exec uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port "${API_PORT:-8000}" ${RELOAD:+--reload}
