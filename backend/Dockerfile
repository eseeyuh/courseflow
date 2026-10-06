# ---- builder: resolve and install locked dependencies with uv ---------------
FROM ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first (cached unless pyproject.toml / uv.lock change).
# --locked fails the build if uv.lock is out of date with pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY . /app

# ---- runtime: plain Python, no uv or build tooling, non-root ---------------
FROM python:3.12.15-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 courseflow \
 && useradd --system --gid 10001 --uid 10001 --no-create-home courseflow

WORKDIR /app
COPY --from=builder --chown=courseflow:courseflow /app /app

USER courseflow
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
