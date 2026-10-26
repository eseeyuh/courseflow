# CourseFlow API for the hosted offline demo. Build context: the repository
# root (see compose.yml). Same build as backend/Dockerfile, plus the demo's
# read-only inputs baked in, so no bind mount and no model key is needed:
#   /demo_data          synthetic Northbridge sources (the demo LMS)
#   /evals/recordings   committed model-call recordings, replayed offline
#   /evals/snapshots    committed embedding snapshots
#   /evals/datasets     the frozen labels the demo validates against
# The paths follow the application's repository-relative lookups from /app.

# ---- builder: resolve and install locked dependencies with uv ---------------
FROM ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY backend/ /app

# ---- runtime ----------------------------------------------------------------
FROM python:3.12.15-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_ENV=demo \
    RAW_STORAGE_DIR=/var/lib/courseflow/raw \
    EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B

RUN groupadd --system --gid 10001 courseflow \
 && useradd --system --gid 10001 --uid 10001 --no-create-home courseflow \
 && install -d -o courseflow -g courseflow -m 0750 /var/lib/courseflow/raw

WORKDIR /app
COPY --from=builder --chown=courseflow:courseflow /app /app
COPY demo_data/ /demo_data/
COPY evals/recordings/ /evals/recordings/
COPY evals/snapshots/ /evals/snapshots/
COPY evals/datasets/ /evals/datasets/

USER courseflow
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
