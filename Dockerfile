ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home app

COPY pyproject.toml README.md ./
COPY src/agent_config.py src/config.py cli.py run.py ./
COPY src/main.py ./src/main.py
COPY src/app ./src/app
COPY migrations ./migrations
COPY crud_migrations ./crud_migrations
COPY alembic.ini alembic.crud.ini ./
COPY mock_data ./mock_data

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install . && \
    mkdir -p /workspace/pv /app/var/workflows && \
    chown -R app:app /app /workspace/pv

USER 10001:10001

EXPOSE 8000 8011

CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "/app/src", "--host", "0.0.0.0", "--port", "8000"]
