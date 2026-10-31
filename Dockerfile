# Docker Hub's official Python image; the digest pins the exact build.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

# uv comes from PyPI, pinned to the version the project's build backend uses.
ARG UV_VERSION=0.11.32
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_CONFIG_FILE=/app/logger.yml \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Cache third-party dependencies independently from application source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
# One image per environment: build with --build-arg APP_ENV=stg (or prd) and
# only that environment's credentials ship in it. Without APP_ENV the image
# carries config.yml alone (the [l] glob may match nothing).
ARG APP_ENV=
COPY logger.yml config.yml config.${APP_ENV}.ym[l] ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 1000 executor \
    && chown -R executor:executor /app

USER executor

EXPOSE 8000

CMD ["executor-service"]
