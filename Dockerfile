FROM python:3.12-slim-bookworm

# Pinned, so a build next month is the same build (the version in use when this was written).
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /uvx /bin/

WORKDIR /app

# Dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Don't run as root.
RUN useradd --create-home appuser
USER appuser

# The host may set $PORT; default 8000 for local runs and ECS.
# `exec` makes fastapi PID 1, so the SIGTERM from `docker stop` / ECS reaches it and it shuts down cleanly.
# (The worker overrides this command: `python -m app.jobs.worker`.)
CMD ["sh", "-c", "exec fastapi run app/main.py --host 0.0.0.0 --port ${PORT:-8000}"]
