# syntax=docker/dockerfile:1

# Use the official uv image based on Python bookworm slim
FROM ghcr.io/astral-sh/uv:python3.13-bookworm AS builder

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
    uv sync --locked --no-editable

# Production stage - smaller final image
FROM python:3.13-slim AS runtime

# Copy the virtual environment from builder stage
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# Copy application code
COPY --from=builder --chown=app:app /app/derp /app/derp

# Create non-root user for security
RUN groupadd --gid=1000 app && \
    useradd --uid=1000 --gid=app --shell=/bin/bash --create-home app

# Set working directory and switch to non-root user
WORKDIR /app
USER app

# Ensure the virtual environment is in PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set the default command
CMD ["python", "-m", "derp"] 
