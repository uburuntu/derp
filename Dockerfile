# syntax=docker/dockerfile:1.25.0@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12

# Use the lock-matched uv image with the production Python runtime.
FROM ghcr.io/astral-sh/uv:0.11.29-python3.14-trixie@sha256:cd22b8ef1b9a27e285a0e8ee3416db1c955d7d14c33bb39ec2a41306c68a5500 AS builder

# Set environment variables for uv
ENV UV_CACHE_DIR=/opt/uv-cache/
ENV UV_SYSTEM_PYTHON=1

# Set the working directory
WORKDIR /app

# Install dependencies first (separate layer for better caching)
# This layer will only rebuild if pyproject.toml or uv.lock changes
RUN --mount=type=cache,target=/opt/uv-cache/ \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

# Copy the application code
COPY . /app

# Install the project itself in non-editable mode
RUN --mount=type=cache,target=/opt/uv-cache/ \
    uv sync --locked --no-editable && \
    uv run pybabel compile -d derp/locales -D messages

# Production stage - smaller final image
FROM python:3.14.6-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime

# Create the runtime identity before named --chown directives.
RUN groupadd --gid=1000 app && \
    useradd --uid=1000 --gid=app --shell=/bin/bash --create-home app

# Copy the virtual environment from builder stage
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Copy application code
COPY --from=builder --chown=app:app /app/derp /app/derp

# Copy alembic config and migrations for database upgrades
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=app:app /app/migrations /app/migrations

# Install ffmpeg for audio conversion (TTS voice messages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory and switch to non-root user
WORKDIR /app
USER app

# Ensure the virtual environment is in PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set the default command
CMD ["python", "-m", "derp"] 
