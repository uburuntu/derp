# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime
# Colour emoji for quote stickers; FriBiDi lets Pillow's bundled Raqm shape joined emoji and scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-color-emoji libfribidi0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/derp /app/derp
COPY --from=builder --chown=app:app /app/migrations /app/migrations
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
USER app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYDANTIC_AI_NO_BANNER=1 HEARTBEAT_PATH=/tmp/derp.poll \
    BACKGROUND_MODEL=/home/app/.cache/derp/u2netp.onnx
# Background-removal weights: fetched and checksum-verified once at build, never during a request.
RUN python -m derp.tools.background fetch "$BACKGROUND_MODEL"
# Last, so a new revision only adds a metadata layer: Derp links its own source at this commit.
ARG SOURCE_REVISION=main
ENV SOURCE_REVISION=$SOURCE_REVISION
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["python", "-m", "derp.telegram.heartbeat"]
CMD ["python", "-m", "derp"]
