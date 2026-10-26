"""Shared API/agent service for the versioned Agent DB catalog.

No HTTP dependencies. Inject an embedding provider; MockEmbeddingProvider is
explicitly for local tests and its model ID cannot mix with real bge-m3 vectors.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg import sql as pg_sql
from httpx import TimeoutException
from psycopg.types.json import Jsonb

from app.services.async_io_resources import workflow_connection_pool
from app.schemas.workflows.workflow_standard import StandardWorkflow


class WorkflowNotFoundError(LookupError):
    """Router should map to 404."""
    code = "WORKFLOW_NOT_FOUND"


class WorkflowVersionNotFoundError(WorkflowNotFoundError):
    code = "WORKFLOW_VERSION_NOT_FOUND"


class WorkflowValidationError(ValueError):
    code = "WORKFLOW_VALIDATION_ERROR"


class WorkflowEmbeddingError(Exception):
    """Provider/invalid vector failure; router maps to 502, never exposes cause."""
    code = "EMBEDDING_SERVICE_ERROR"


class WorkflowEmbeddingTimeoutError(WorkflowEmbeddingError):
    """Router maps to 504."""
    code = "EMBEDDING_TIMEOUT"


class WorkflowRevisionConflictError(Exception):
    """Router should map to 409."""
    code = "WORKFLOW_REVISION_CONFLICT"
    def __init__(self, expected_revision: int, current_revision: int):
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(f"Expected revision {expected_revision}, current revision {current_revision}")


class WorkflowIdempotencyConflictError(Exception):
    """The same requester/key was reused for a different write request (409)."""
    code = "IDEMPOTENCY_KEY_CONFLICT"


class EmbeddingProvider(Protocol):
    model: str
    async def embed(self, text: str) -> list[float]: ...


class MockEmbeddingProvider:
    """Deterministic normalized mock, not semantic embeddings."""
    model = "mock/bge-m3/1024/v1"

    def __init__(self, *, model_name="bge-m3", dimension=1024):
        self.dimension = dimension
        self.model = f"mock/{model_name}/{dimension}/v1"

    async def embed(self, text: str) -> list[float]:
        rng = random.Random(hashlib.sha256(text.encode()).digest())
        values = [rng.uniform(-1, 1) for _ in range(self.dimension)]
        norm = math.sqrt(sum(x*x for x in values))
        return [x / norm for x in values]


def _uuid(value) -> UUID:
    return UUID(str(value))


def _optional_uuid(value):
    return None if value is None else _uuid(value)


def _vector(values) -> str:
    if not values:
        raise ValueError("Embedding must have positive dimensions")
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Embedding values must be numbers")
        number = float(value)
        if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
            raise ValueError("Embedding values must be finite float32 numbers")
        numbers.append(number)
    if not any(abs(x) >= 1.401298464324817e-45 for x in numbers):
        raise ValueError("Cosine search requires a nonzero embedding")
    return json.dumps(numbers, allow_nan=False)


def build_search_text(document: dict) -> str:
    from app.agents.workflow_generator.workflow_generator_tools import (
        _load_skill_index_document, _load_tool_registry_document,
    )
    skills = _load_skill_index_document()["skills"]
    tools = _load_tool_registry_document()["tools"]
    definition = document["workflow"]
    parts = [definition["name"], definition["user_request"], definition["execution_mode"]]
    for step in definition["steps"]:
        skill = step["skill"]
        parts.extend([skill, skills[skill].get("description", "")])
        for call in step["tools"]:
            tool = call["tool"]
            parts.extend([tool, tools[tool].get("docstring", "")])
    return "\n".join(parts)


async def _rows(conn, sql, params=()):
    async with conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(sql, params)
        return await cursor.fetchall()


CURRENT = """
    SELECT c.*, v.version_id, v.revision, v.workflow, v.registration_type,
           v.created_by AS version_created_by, v.project_id, v.session_id, v.task_id,
           v.created_at AS version_created_at
    FROM workflow_catalog c JOIN workflow_version v
      ON v.workflow_id=c.workflow_id AND v.revision=c.current_revision
"""


class WorkflowCatalogService:
    def __init__(self, database_url: str, embedding_provider: EmbeddingProvider, *, embedding_dimension=1024):
        if not database_url:
            raise ValueError("Agent Workflow database URL is required")
        if not embedding_provider.model.strip():
            raise ValueError("Embedding model identifier is required")
        self.database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.embedding_provider = embedding_provider
        if type(embedding_dimension) is not int or embedding_dimension < 1:
            raise ValueError("embedding_dimension must be positive")
        self.embedding_dimension = embedding_dimension

    @classmethod
    def from_environment(cls, embedding_provider: EmbeddingProvider | None = None):
        from agent_config import load_agent_settings
        settings = load_agent_settings()
        database_url = os.environ.get("WORKFLOW_DATABASE_URL") or os.environ.get("EW_DATABASE_URL")
        if embedding_provider is None:
            from app.services.workflow_embedding_provider import create_embedding_provider
            embedding_provider = create_embedding_provider(settings)
        return cls(database_url or "", embedding_provider,
                   embedding_dimension=settings.embedding_dimension)

    async def _prepare(self, workflow, workflow_id, registration_type):
        if registration_type not in ("USER", "AGENT"):
            raise ValueError("registration_type must be USER or AGENT")
        from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
        # Validate Skill/Tool, arguments, outputs and conditions; never persist 1.3.
        try:
            document = StandardWorkflow.model_validate(workflow).model_dump(mode="json", exclude_unset=True)
            compile_workflow(document, workflow_id=workflow_id)
        except ValueError as exc:
            raise WorkflowValidationError(str(exc)) from exc
        search_text = build_search_text(document)
        vector, dimensions = await self._embed(search_text)
        return document, search_text, vector, dimensions

    async def _embed(self, text):
        try:
            values = await self.embedding_provider.embed(text)
        except (TimeoutError, TimeoutException) as exc:
            raise WorkflowEmbeddingTimeoutError("Embedding request timed out") from exc
        except Exception as exc:
            raise WorkflowEmbeddingError("Embedding provider failed") from exc
        try:
            vector = _vector(values)
            if len(values) != self.embedding_dimension:
                raise ValueError("Unexpected embedding dimension")
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkflowEmbeddingError("Embedding provider returned an invalid vector") from exc
        return vector, len(values)

    async def _insert_version(self, conn, workflow_id, revision, prepared, registration_type,
                              created_by, project_id, session_id, task_id):
        version_id = uuid4()
        document, search_text, vector, dimensions = prepared
        await conn.execute("""
            INSERT INTO workflow_version
            (version_id, workflow_id, revision, workflow, registration_type, created_by, project_id, session_id, task_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (version_id, workflow_id, revision, Jsonb(document), registration_type,
              created_by, project_id, session_id, task_id))
        await conn.execute("""
            INSERT INTO workflow_embeddings
            (embedding_id, version_id, search_text, embedding_model, embedding, embedding_dimension, text_recipe_version)
            VALUES (%s,%s,%s,%s,%s::vector,%s,'1')
        """, (uuid4(), version_id, search_text, self.embedding_provider.model, vector, dimensions))

    def _request_signature(self, key, *, operation, workflow, registration_type, user, source,
                           workflow_id=None, expected_revision=None):
        if key is None:
            return None
        if not isinstance(key, str) or not key.strip() or len(key) > 255 or key != key.strip():
            raise ValueError("idempotency_key must be a nonblank string of at most 255 characters without surrounding whitespace")
        if registration_type not in ("USER", "AGENT"):
            raise ValueError("registration_type must be USER or AGENT")
        try:
            document = StandardWorkflow.model_validate(workflow).model_dump(mode="json", exclude_unset=True)
        except ValueError as exc:
            raise WorkflowValidationError(str(exc)) from exc
        payload = {"operation": operation, "workflow": document,
                   "registration_type": registration_type, "user": str(user),
                   "source": [str(x) if x is not None else None for x in source],
                   "workflow_id": str(workflow_id) if workflow_id is not None else None,
                   "expected_revision": expected_revision}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    async def _request_result(self, conn, user, key, signature, *, lock=False):
        if signature is None:
            return None
        if lock:
            # Serialize competing writes for one requester/key, after embeddings finish.
            lock_id = int.from_bytes(hashlib.sha256(f"workflow-write:{user}:{key}".encode()).digest()[:8],
                                     "big", signed=True)
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))
        rows = await _rows(conn, """SELECT request_hash, result FROM workflow_catalog_requests
                                    WHERE requested_by=%s AND request_key=%s""", (user, key))
        if not rows:
            return None
        if rows[0]["request_hash"] != signature:
            raise WorkflowIdempotencyConflictError("Idempotency key was used for a different request")
        result = rows[0]["result"]
        for field in ("workflow_id", "version_id", "created_by", "updated_by", "deleted_by",
                      "version_created_by", "project_id", "session_id", "task_id"):
            if result.get(field) is not None:
                result[field] = UUID(result[field])
        for field in ("created_at", "updated_at", "deleted_at", "version_created_at"):
            if result.get(field) is not None:
                result[field] = datetime.fromisoformat(result[field])
        return result

    async def _save_request_result(self, conn, user, key, signature, result):
        if signature is None:
            return
        snapshot = {k: str(v) if isinstance(v, UUID) else v.isoformat() if isinstance(v, datetime) else v
                    for k, v in result.items()}
        await conn.execute("""INSERT INTO workflow_catalog_requests(requested_by, request_key, request_hash, result)
                              VALUES (%s,%s,%s,%s)""", (user, key, signature, Jsonb(snapshot)))

    async def _replay_request(self, pool, user, key, signature):
        if signature is None:
            return None
        async with pool.connection() as conn:
            return await self._request_result(conn, user, key, signature)

    async def register(self, *, workflow: dict, registration_type: str, created_by,
                       project_id=None, session_id=None, task_id=None, idempotency_key: str | None = None) -> dict:
        workflow_id, user = uuid4(), _uuid(created_by)
        source = tuple(_optional_uuid(x) for x in (project_id, session_id, task_id))
        pool = await workflow_connection_pool(self.database_url)
        signature = self._request_signature(idempotency_key, operation="register", workflow=workflow,
                                            registration_type=registration_type, user=user, source=source)
        replay = await self._replay_request(pool, user, idempotency_key, signature)
        if replay is not None:
            return replay
        prepared = await self._prepare(workflow, workflow_id, registration_type)
        async with pool.connection() as conn, conn.transaction():
            replay = await self._request_result(conn, user, idempotency_key, signature, lock=True)
            if replay is not None:
                return replay
            await conn.execute("INSERT INTO workflow_catalog(workflow_id,created_by,updated_by) VALUES (%s,%s,%s)", (workflow_id, user, user))
            await self._insert_version(conn, workflow_id, 1, prepared, registration_type, user, *source)
            result = (await _rows(conn, CURRENT + " WHERE c.workflow_id=%s", (workflow_id,)))[0]
            await self._save_request_result(conn, user, idempotency_key, signature, result)
            return result

    async def _lock(self, conn, workflow_id, *, include_deleted=False):
        rows = await _rows(conn, "SELECT * FROM workflow_catalog WHERE workflow_id=%s FOR UPDATE", (workflow_id,))
        if not rows or (rows[0]["deleted_at"] is not None and not include_deleted):
            raise WorkflowNotFoundError(str(workflow_id))
        return rows[0]

    async def update(self, workflow_id, *, expected_revision: int, workflow: dict,
                     registration_type: str, updated_by, project_id=None, session_id=None, task_id=None,
                     idempotency_key: str | None = None) -> dict:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        workflow_id, user = _uuid(workflow_id), _uuid(updated_by)
        source = tuple(_optional_uuid(x) for x in (project_id, session_id, task_id))
        pool = await workflow_connection_pool(self.database_url)
        signature = self._request_signature(idempotency_key, operation="update", workflow=workflow,
                                            registration_type=registration_type, user=user, source=source,
                                            workflow_id=workflow_id, expected_revision=expected_revision)
        replay = await self._replay_request(pool, user, idempotency_key, signature)
        if replay is not None:
            return replay
        # Provider calls finish before acquiring the write transaction/row lock.
        prepared = await self._prepare(workflow, workflow_id, registration_type)
        async with pool.connection() as conn, conn.transaction():
            replay = await self._request_result(conn, user, idempotency_key, signature, lock=True)
            if replay is not None:
                return replay
            catalog = await self._lock(conn, workflow_id)
            if catalog["current_revision"] != expected_revision:
                raise WorkflowRevisionConflictError(expected_revision, catalog["current_revision"])
            revision = expected_revision + 1
            await self._insert_version(conn, workflow_id, revision, prepared, registration_type, user, *source)
            await conn.execute("UPDATE workflow_catalog SET current_revision=%s, updated_by=%s, updated_at=now() WHERE workflow_id=%s", (revision, user, workflow_id))
            result = (await _rows(conn, CURRENT + " WHERE c.workflow_id=%s", (workflow_id,)))[0]
            await self._save_request_result(conn, user, idempotency_key, signature, result)
            return result

    async def get(self, workflow_id, *, revision: int | None = None) -> dict:
        workflow_id = _uuid(workflow_id)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("revision must be a positive integer")
        sql = CURRENT.replace("FROM workflow_catalog c", ", e.search_text FROM workflow_catalog c")
        sql += " LEFT JOIN workflow_embeddings e ON e.version_id=v.version_id AND e.embedding_model=%s"
        params = [self.embedding_provider.model, workflow_id]
        if revision is not None:
            sql = sql.replace("v.revision=c.current_revision", "v.revision=%s")
            params.insert(0, revision)
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn:
            rows = await _rows(conn, sql + " WHERE c.workflow_id=%s AND c.deleted_at IS NULL", params)
            if not rows:
                if revision is not None and await _rows(conn, "SELECT workflow_id FROM workflow_catalog WHERE workflow_id=%s AND deleted_at IS NULL", (workflow_id,)):
                    raise WorkflowVersionNotFoundError(str(workflow_id))
                raise WorkflowNotFoundError(str(workflow_id))
            return rows[0]

    async def list(self, *, q=None, recommend_yn=None, execution_mode=None,
                   registration_type=None, created_by=None, page=1, page_size=20,
                   sort_by="updated_at", sort_order="desc") -> dict:
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("page must be positive and page_size must be 1..100")
        if recommend_yn not in (None, "Y", "N"):
            raise ValueError("recommend_yn must be Y or N")
        if execution_mode not in (None, "static", "adaptive"):
            raise ValueError("execution_mode must be static or adaptive")
        if registration_type not in (None, "USER", "AGENT"):
            raise ValueError("registration_type must be USER or AGENT")
        if sort_by not in ("created_at", "updated_at", "name") or sort_order not in ("asc", "desc"):
            raise ValueError("Invalid sort field or direction")
        if q is not None and not isinstance(q, str):
            raise ValueError("q must be a string")
        query = (q or "").strip()
        user = _optional_uuid(created_by)
        source = """
            FROM workflow_catalog c JOIN workflow_version v
              ON v.workflow_id=c.workflow_id AND v.revision=c.current_revision
            WHERE c.deleted_at IS NULL
              AND (%s::text IS NULL OR c.recommend_yn=%s)
              AND (%s::text IS NULL OR v.workflow->'workflow'->>'execution_mode'=%s)
              AND (%s::text IS NULL OR v.registration_type=%s)
              AND (%s::uuid IS NULL OR c.created_by=%s)
              AND (%s='' OR strpos(lower(v.workflow->'workflow'->>'name'),lower(%s))>0
                         OR strpos(lower(v.workflow->'workflow'->>'user_request'),lower(%s))>0)
        """
        params = (recommend_yn, recommend_yn, execution_mode, execution_mode,
                  registration_type, registration_type, user, user, query, query, query)
        columns = """SELECT c.workflow_id, c.current_revision, c.recommend_yn,
            c.created_by, c.updated_by, c.created_at, c.updated_at, v.version_id, v.registration_type,
            v.workflow->'workflow'->>'name' AS name,
            v.workflow->'workflow'->>'user_request' AS user_request,
            v.workflow->'workflow'->>'execution_mode' AS execution_mode,
            (v.workflow->'workflow'->>'allow_immediate_execution')::boolean AS allow_immediate_execution
        """
        # Sort expressions are selected exclusively from the validated allowlist.
        order = pg_sql.SQL("name") if sort_by == "name" else pg_sql.Identifier("c", sort_by)
        statement = pg_sql.SQL(columns + source + " ORDER BY {} {}, c.workflow_id ASC LIMIT %s OFFSET %s").format(order, pg_sql.SQL(sort_order.upper()))
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn, conn.transaction():
            # Count and page share a snapshot even if a concurrent writer commits.
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            total = (await _rows(conn, "SELECT count(*) AS total_count " + source, params))[0]["total_count"]
            items = await _rows(conn, statement, params + (page_size, (page - 1)*page_size))
            return {"items": items, "page": page, "page_size": page_size, "total_count": total}

    async def set_recommendation(self, workflow_id, *, recommend_yn: str, updated_by) -> dict:
        if recommend_yn not in ("Y", "N"):
            raise ValueError("recommend_yn must be Y or N")
        workflow_id, user = _uuid(workflow_id), _uuid(updated_by)
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn, conn.transaction():
            row = await self._lock(conn, workflow_id)
            if row["recommend_yn"] != recommend_yn:
                await conn.execute("UPDATE workflow_catalog SET recommend_yn=%s, updated_by=%s, updated_at=now() WHERE workflow_id=%s", (recommend_yn, user, workflow_id))
            return (await _rows(conn, CURRENT + " WHERE c.workflow_id=%s", (workflow_id,)))[0]

    async def delete(self, workflow_id, *, deleted_by) -> None:
        workflow_id, user = _uuid(workflow_id), _uuid(deleted_by)
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn, conn.transaction():
            row = await self._lock(conn, workflow_id, include_deleted=True)
            if row["deleted_at"] is None:
                await conn.execute("UPDATE workflow_catalog SET deleted_at=now(), deleted_by=%s, updated_at=now(), updated_by=%s WHERE workflow_id=%s", (user, user, workflow_id))

    async def search(self, query: str, *, limit=3, threshold=None, execution_mode=None, exclude_workflow_ids=None) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be 1..20")
        if threshold is not None and (type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1):
            raise ValueError("threshold must be a finite number in -1..1")
        if execution_mode not in (None, "static", "adaptive"):
            raise ValueError("execution_mode must be static or adaptive")
        excluded_ids = [UUID(str(value)) for value in (exclude_workflow_ids or [])]
        vector, dimensions = await self._embed(query.strip())
        pool = await workflow_connection_pool(self.database_url)
        async with pool.connection() as conn:
            # MATERIALIZED prevents evaluating cosine distance on other models'
            # dimensions before the WHERE filter has been applied.
            items = await _rows(conn, """
                WITH eligible AS MATERIALIZED (
                    SELECT c.workflow_id, c.current_revision, v.version_id, v.workflow, e.embedding, e.search_text
                    FROM workflow_catalog c JOIN workflow_version v
                      ON v.workflow_id=c.workflow_id AND v.revision=c.current_revision
                    JOIN workflow_embeddings e ON e.version_id=v.version_id
                    WHERE c.deleted_at IS NULL AND c.recommend_yn='Y'
                      AND NOT (c.workflow_id = ANY(%s::uuid[]))
                      AND e.embedding_model=%s AND e.embedding_dimension=%s
                      AND (%s::text IS NULL OR v.workflow->'workflow'->>'execution_mode'=%s)
                ), scored AS (
                    SELECT workflow_id, current_revision AS revision, version_id, workflow, search_text,
                           greatest(-1.0, least(1.0, 1 - (embedding <=> %s::vector))) AS similarity
                    FROM eligible
                )
                SELECT * FROM scored WHERE (%s::float8 IS NULL OR similarity >= %s)
                ORDER BY similarity DESC, workflow_id LIMIT %s
            """, (excluded_ids, self.embedding_provider.model, dimensions, execution_mode, execution_mode, vector, threshold, threshold, limit))
            return {"items": items, "returned_count": len(items), "limit": limit,
                    "threshold": threshold, "embedding_model": self.embedding_provider.model}
