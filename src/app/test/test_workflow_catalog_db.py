"""Real PostgreSQL/pgvector tests; every test owns a disposable database.

Set WORKFLOW_CATALOG_TEST_ADMIN_URL to a local PostgreSQL admin connection.
Never migrate the configured application DB. No credentials are logged.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from sqlalchemy.engine import URL
import pytest
import pytest_asyncio

from app.services.async_io_resources import close_async_io_resources
from app.services.workflow_catalog_service import (
    MockEmbeddingProvider, WorkflowCatalogService, WorkflowNotFoundError,
    WorkflowRevisionConflictError, WorkflowEmbeddingError, WorkflowEmbeddingTimeoutError,
    WorkflowVersionNotFoundError,
    WorkflowIdempotencyConflictError,
)
from app.services.workflow_persistence import PostgresWorkflowStore

ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = json.loads((Path(__file__).parent / "fixtures/workflow_standard/static.json").read_text())
USER = uuid4()


def migrate(url, target="head", *, preserve=False, downgrade=False, archive_dir=None):
    settings = conninfo_to_dict(url)
    query = {k: v for k, v in settings.items() if k not in ("dbname", "user", "password")}
    migration_url = URL.create("postgresql", username=settings.get("user"), password=settings.get("password"),
                               database=settings["dbname"], query=query).render_as_string(hide_password=False)
    env = dict(os.environ, EW_DATABASE_URL=migration_url)
    command = [sys.executable, "-m", "alembic", "-x",
               "execution_plans_archive_dir=" + str(archive_dir or ROOT / ".cache" / "workflow_migration_test_archives" / settings["dbname"])]
    if preserve:
        command += ["-x", "preserve_legacy_catalog=true"]
    command += ["downgrade" if downgrade else "upgrade", target]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)


@pytest.fixture
def database_url():
    admin = os.environ.get("WORKFLOW_CATALOG_TEST_ADMIN_URL")
    if not admin:
        pytest.skip("WORKFLOW_CATALOG_TEST_ADMIN_URL is required for real pgvector tests")
    settings = conninfo_to_dict(admin)
    host = settings.get("host", "")
    if not (host.startswith("/") or host in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Tests require an explicit local PostgreSQL host")
    name = "workflow_catalog_test_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin, dbname=name)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest_asyncio.fixture
async def service(database_url):
    result = migrate(database_url)
    assert result.returncode == 0, result.stderr
    try:
        yield WorkflowCatalogService(database_url, MockEmbeddingProvider())
    finally:
        await close_async_io_resources()


async def register(service, **kwargs):
    return await service.register(workflow=deepcopy(DOCUMENT), registration_type="USER", created_by=USER, **kwargs)


@pytest.mark.asyncio
async def test_idempotent_register_replays_original_snapshot_without_embedding(service, database_url):
    from unittest.mock import AsyncMock
    service.embedding_provider.embed = AsyncMock(wraps=service.embedding_provider.embed)
    first = await register(service, idempotency_key="register-once")
    await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=USER)
    await service.delete(first["workflow_id"], deleted_by=USER)
    replay = await register(service, idempotency_key="register-once")
    assert replay == first and replay["recommend_yn"] == "N"
    assert service.embedding_provider.embed.call_count == 1
    with psycopg.connect(database_url) as conn:
        assert [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                ("workflow_catalog", "workflow_version", "workflow_embeddings", "workflow_catalog_requests")] == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_idempotency_key_conflict_and_requester_scope(service):
    first = await register(service, idempotency_key="shared-key")
    changed = deepcopy(DOCUMENT)
    changed["workflow"]["name"] = "changed"
    with pytest.raises(WorkflowIdempotencyConflictError):
        await service.register(workflow=changed, registration_type="USER", created_by=USER, idempotency_key="shared-key")
    with pytest.raises(WorkflowIdempotencyConflictError):
        await register(service, idempotency_key="shared-key", project_id=uuid4())
    with pytest.raises(WorkflowIdempotencyConflictError):
        await service.update(first["workflow_id"], expected_revision=1, workflow=DOCUMENT,
                             registration_type="USER", updated_by=USER, idempotency_key="shared-key")
    other = await service.register(workflow=DOCUMENT, registration_type="USER", created_by=uuid4(),
                                   idempotency_key="shared-key")
    assert other["workflow_id"] != first["workflow_id"]


@pytest.mark.asyncio
async def test_idempotent_update_replays_success_before_revision_check(service, database_url):
    first = await register(service)
    request = dict(expected_revision=1, workflow=deepcopy(DOCUMENT), registration_type="USER", updated_by=USER,
                   idempotency_key="update-once")
    updated = await service.update(first["workflow_id"], **request)
    await service.update(first["workflow_id"], expected_revision=2, workflow=DOCUMENT,
                         registration_type="USER", updated_by=USER)
    assert await service.update(first["workflow_id"], **request) == updated
    with pytest.raises(WorkflowIdempotencyConflictError):
        await service.update(first["workflow_id"], **{**request, "expected_revision": 3})
    with pytest.raises(WorkflowRevisionConflictError):
        await service.update(first["workflow_id"], **{**request, "idempotency_key": None})
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["register", "update"])
async def test_idempotent_concurrent_writes_commit_once(service, database_url, operation):
    first = await register(service) if operation == "update" else None
    original = service.embedding_provider.embed
    ready = asyncio.Event()
    calls = 0
    async def embedding(text):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Both requests are embedding; neither keeps its preflight transaction.
            with psycopg.connect(database_url) as conn:
                assert conn.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                                    "AND state='idle in transaction'").fetchone()[0] == 0
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return await original(text)
    service.embedding_provider.embed = embedding
    async def write():
        if operation == "register":
            return await register(service, idempotency_key="concurrent")
        return await service.update(first["workflow_id"], expected_revision=1, workflow=DOCUMENT,
                                    registration_type="USER", updated_by=USER, idempotency_key="concurrent")
    left, right = await asyncio.gather(write(), write())
    assert left == right and calls == 2
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == (2 if first else 1)
        assert conn.execute("SELECT count(*) FROM workflow_catalog_requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_failed_idempotent_write_rolls_back_request_record_and_can_retry(service, database_url):
    original = service._save_request_result
    async def fail(conn, *args):
        await original(conn, *args)
        raise RuntimeError("simulated failure after request record")
    service._save_request_result = fail
    with pytest.raises(RuntimeError, match="simulated failure"):
        await register(service, idempotency_key="retry-failure")
    with psycopg.connect(database_url) as conn:
        for table in ("workflow_catalog", "workflow_version", "workflow_embeddings", "workflow_catalog_requests"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    service._save_request_result = original
    await register(service, idempotency_key="retry-failure")


@pytest.mark.asyncio
async def test_agent_collector_retry_uses_stable_key_and_same_version(service, database_url):
    from app.graphs.nodes.generate_workflow import make_add_current_workflow_candidate
    from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
    state = {"user_id": str(USER), "task_id": str(uuid4()), "session_id": str(uuid4()),
             "workflow_definition": deepcopy(DOCUMENT), "workflow": compile_workflow(DOCUMENT, workflow_id=str(uuid4())),
             "workflow_origin": "generated", "workflow_revision": 1, "workflow_candidates": [],
             "execution_inputs": [], "candidate_selection_payload_version": 2}
    alternative = deepcopy(DOCUMENT)
    tools = alternative["workflow"]["steps"][-1]["tools"]
    tools.append(deepcopy(tools[-1]))
    state["generated_workflow_drafts"] = [
        {"workflow_definition": document, "workflow": compile_workflow(document, workflow_id=str(uuid4())),
         "workflow_revision": 1, "workflow_origin": "generated", "execution_inputs": []}
        for document in (deepcopy(DOCUMENT), alternative)
    ]
    collect = make_add_current_workflow_candidate(service)
    original = service._save_request_result
    async def fail_second(conn, user, key, signature, result):
        await original(conn, user, key, signature, result)
        if key.endswith(":2"):
            raise RuntimeError("simulated second candidate failure")
    service._save_request_result = fail_second
    with pytest.raises(RuntimeError, match="second candidate"):
        await collect(state)
    service._save_request_result = original
    left, right = await collect(state), await collect(state)
    assert left["workflow_candidates"] == right["workflow_candidates"]
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_catalog_requests").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 2
        keys = [r[0] for r in conn.execute("SELECT request_key FROM workflow_catalog_requests ORDER BY request_key")]
        assert keys == [f"agent:workflow-register:{state['task_id']}:1:{i}" for i in (1, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", " ", " padded", "x" * 256, 123])
async def test_invalid_idempotency_key(service, key):
    with pytest.raises(ValueError, match="idempotency_key"):
        await register(service, idempotency_key=key)


@pytest.mark.asyncio
async def test_lifecycle_and_current_search(service, database_url):
    sources = {k: uuid4() for k in ("project_id", "session_id", "task_id")}
    first = await register(service, **sources)
    wid = first["workflow_id"]
    assert first["current_revision"] == first["revision"] == 1
    assert first["recommend_yn"] == "N"
    assert first["workflow"] == DOCUMENT
    assert all(first[k] == v for k, v in sources.items())
    assert (await service.search("analysis"))["items"] == []
    await service.set_recommendation(wid, recommend_yn="Y", updated_by=USER)
    document = deepcopy(DOCUMENT)
    document["workflow"]["name"] = "Second version"
    updater = uuid4()
    second = await service.update(wid, expected_revision=1, workflow=document,
                                  registration_type="AGENT", updated_by=updater)
    assert second["current_revision"] == 2 and second["recommend_yn"] == "Y"
    assert second["created_by"] == USER and second["version_created_by"] == updater
    assert second["registration_type"] == "AGENT"
    assert all(second[k] is None for k in sources)
    assert (await service.get(wid, revision=1))["workflow"] == DOCUMENT
    assert [x["revision"] for x in (await service.search("analysis"))["items"]] == [2]
    assert len((await service.list(recommend_yn="Y"))["items"]) == 1
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT embedding_dimension, vector_dims(embedding) FROM workflow_embeddings").fetchall() == [(1024, 1024)] * 2
    await service.delete(wid, deleted_by=USER)
    await service.delete(wid, deleted_by=uuid4())
    assert (await service.search("analysis"))["items"] == [] and (await service.list())["items"] == []
    for action in (service.get(wid), service.delete(uuid4(), deleted_by=USER),
                   service.set_recommendation(wid, recommend_yn="Y", updated_by=USER)):
        with pytest.raises(WorkflowNotFoundError):
            await action
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM workflow_embeddings").fetchone()[0] == 2
        assert conn.execute("SELECT deleted_by FROM workflow_catalog").fetchone()[0] == USER


@pytest.mark.asyncio
async def test_concurrent_updates_only_one_wins(service, database_url):
    first = await register(service)
    async def update():
        return await service.update(first["workflow_id"], expected_revision=1,
                                    workflow=deepcopy(DOCUMENT), registration_type="USER", updated_by=USER)
    results = await asyncio.gather(update(), update(), return_exceptions=True)
    assert sum(isinstance(x, dict) for x in results) == 1
    conflict = next(x for x in results if isinstance(x, WorkflowRevisionConflictError))
    assert (conflict.expected_revision, conflict.current_revision) == (1, 2)
    assert (await service.get(first["workflow_id"]))["recommend_yn"] == "N"
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM workflow_embeddings").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_embedding_failure_and_validation_leave_no_rows(service, database_url):
    class FailingProvider:
        model = "bge-m3"
        async def embed(self, text):
            raise RuntimeError("provider unavailable")
    service.embedding_provider = FailingProvider()
    with pytest.raises(WorkflowEmbeddingError, match="Embedding provider failed"):
        await register(service)
    bad = deepcopy(DOCUMENT)
    bad["workflow"]["steps"][0]["skill"] = "unknown_skill"
    with pytest.raises(ValueError, match="unknown Skill"):
        await service.register(workflow=bad, registration_type="USER", created_by=USER)
    compiled = {"schema_version": "1.3", "workflow": {}}
    with pytest.raises(ValueError):
        await service.register(workflow=compiled, registration_type="USER", created_by=USER)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_db_failure_rolls_back_registration_and_update(service, database_url):
    first = await register(service)
    prepare = service._prepare
    async def wrong_dimension(*args):
        document, text, vector, dim = await prepare(*args)
        return document, text, vector, dim + 1
    service._prepare = wrong_dimension
    with pytest.raises(psycopg.errors.CheckViolation):
        await register(service)
    with pytest.raises(psycopg.errors.CheckViolation):
        await service.update(first["workflow_id"], expected_revision=1,
                             workflow=DOCUMENT, registration_type="USER", updated_by=USER)
    assert (await service.get(first["workflow_id"]))["current_revision"] == 1
    with psycopg.connect(database_url) as conn:
        for table in ("workflow_catalog", "workflow_version", "workflow_embeddings"):
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_search_excludes_other_model_and_dimension(service, database_url):
    first = await register(service)
    await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=USER)
    with psycopg.connect(database_url) as conn:
        conn.execute("""INSERT INTO workflow_embeddings
            (embedding_id, version_id, search_text, embedding_model, embedding, embedding_dimension, text_recipe_version)
            VALUES (%s,%s,'other','other-model','[1,2,3]',3,'1')""", (uuid4(), first["version_id"]))
    assert len((await service.search("analysis"))["items"]) == 1


@pytest.mark.parametrize("statement", [
    "UPDATE workflow_catalog SET current_revision=0",
    "UPDATE workflow_catalog SET current_revision=99",
    "UPDATE workflow_catalog SET recommend_yn='X'",
    "UPDATE workflow_catalog SET deleted_at=now()",
    "UPDATE workflow_version SET registration_type='INVALID'",
    "UPDATE workflow_version SET revision=0",
    "UPDATE workflow_version SET workflow='{}'::jsonb",
    "UPDATE workflow_embeddings SET embedding_dimension=0",
    "UPDATE workflow_embeddings SET embedding_dimension=3",
    "UPDATE workflow_embeddings SET search_text=' '",
    "DELETE FROM workflow_version",
    "DELETE FROM workflow_catalog",
])
@pytest.mark.asyncio
async def test_constraints(service, database_url, statement):
    await register(service)
    with pytest.raises(psycopg.IntegrityError):
        with psycopg.connect(database_url) as conn:
            conn.execute(statement)


def seed_legacy(url):
    catalog_ids = [uuid4(), uuid4()]
    execution = uuid4()
    with psycopg.connect(url) as conn:
        for index, catalog in enumerate(catalog_ids):
            conn.execute("""INSERT INTO workflow_catalog
                (catalog_id,workflow_id,revision,source_session_id,source_task_id,intent,workflow_status,workflow)
                VALUES (%s,'legacy-text-id',1,'legacy-session',%s,'analysis','ready',%s)""",
                (catalog, f"task-{index}", Jsonb({"schema_version":"1.3", "workflow":{"id":"legacy-text-id"}})))
        conn.execute("""INSERT INTO workflow_executions
            (execution_id,catalog_id,workflow_id,workflow_revision,session_id,task_id,original_workflow)
            VALUES (%s,%s,'legacy-text-id',1,'legacy-session','task-0','{}')""", (execution, catalog_ids[0]))
        conn.execute("INSERT INTO workflow_adaptive_history(execution_id,decision_round,changes,effective_workflow) VALUES (%s,1,'[]','{}')", (execution,))
    return catalog_ids, execution


def test_populated_migration_guard_preserves_exact_execution_fk(database_url):
    assert migrate(database_url, "ew_0002").returncode == 0
    catalogs, execution = seed_legacy(database_url)
    blocked = migrate(database_url)
    assert blocked.returncode != 0 and "preserve_legacy_catalog" in blocked.stderr
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('workflow_catalog_legacy')").fetchone()[0] is None
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0] == "ew_0002"
    result = migrate(database_url, "ew_0003", preserve=True)
    assert result.returncode == 0, result.stderr
    with psycopg.connect(database_url) as conn:
        assert set(x[0] for x in conn.execute("SELECT catalog_id FROM workflow_catalog_legacy")) == set(catalogs)
        assert conn.execute("SELECT catalog_id FROM workflow_executions WHERE execution_id=%s", (execution,)).fetchone()[0] == catalogs[0]
        assert conn.execute("SELECT count(*) FROM workflow_adaptive_history_view").fetchone()[0] == 1
        assert conn.execute("SELECT confrelid::regclass::text FROM pg_constraint WHERE conname='workflow_executions_catalog_id_fkey'").fetchone()[0] == "workflow_catalog_legacy"
        assert conn.execute("SELECT count(*) FROM workflow_catalog_legacy_mapping WHERE version_id IS NULL").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 0
    result = migrate(database_url, "ew_0002", downgrade=True)
    assert result.returncode == 0, result.stderr
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 2
        assert conn.execute("SELECT catalog_id FROM workflow_executions").fetchone()[0] == catalogs[0]


@pytest.mark.asyncio
async def test_direct_execution_store_records_history_without_legacy(service, database_url):
    store = PostgresWorkflowStore(database_url)
    sid, tid, execution = str(uuid4()), str(uuid4()), str(uuid4())
    compiled = {"schema_version":"1.3", "workflow":{"id":str(uuid4())}}
    await store.start_execution(execution_id=execution, session_id=sid,
                                task_id=tid, revision=1, workflow=compiled, status="running")
    await store.record_adaptive_round(execution_id=execution, decision_round=1, changes=[], effective_workflow=compiled)
    await store.finish_execution(execution_id=execution, status="SUCCEEDED", final_workflow=compiled)
    await store.finish_execution(execution_id=execution, status="FAILED", final_workflow={})
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT execution_status,successful,original_workflow,final_workflow FROM workflow_executions").fetchone() == ("SUCCEEDED",True,compiled,compiled)
        assert conn.execute("SELECT count(*) FROM workflow_adaptive_history_view").fetchone()[0] == 1
        assert conn.execute("SELECT to_regclass('workflow_catalog_legacy'),to_regclass('workflow_catalog_legacy_mapping')").fetchone() == (None,None)
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_downgrade_refuses_new_data(service, database_url):
    await register(service)
    result = migrate(database_url, "ew_0002", downgrade=True)
    assert result.returncode != 0 and "reviewed restoration migration" in result.stderr
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0] == "ew_0006"
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unique_revision_and_embedding_model(service, database_url):
    first = await register(service)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute("""INSERT INTO workflow_version
                (version_id,workflow_id,revision,workflow,registration_type,created_by)
                SELECT %s,workflow_id,revision,workflow,registration_type,created_by FROM workflow_version""", (uuid4(),))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(database_url) as conn:
            conn.execute("""INSERT INTO workflow_embeddings
                (embedding_id,version_id,search_text,embedding_model,embedding,embedding_dimension,text_recipe_version)
                SELECT %s,version_id,search_text,embedding_model,embedding,embedding_dimension,text_recipe_version
                FROM workflow_embeddings WHERE version_id=%s""", (uuid4(), first["version_id"]))


@pytest.mark.asyncio
async def test_agent_models_match_migration_and_vector_roundtrip(service, database_url):
    from sqlalchemy import create_engine, select, update
    from app.models.workflow_catalog import AgentWorkflowBase, WorkflowEmbedding
    first = await register(service)
    with psycopg.connect(database_url) as conn:
        for table in AgentWorkflowBase.metadata.sorted_tables:
            actual = dict(conn.execute("SELECT column_name,is_nullable FROM information_schema.columns WHERE table_name=%s AND table_schema='public'", (table.name,)))
            assert actual == {c.name: "YES" if c.nullable else "NO" for c in table.columns}
        user_fks = conn.execute("""SELECT count(*) FROM pg_constraint
            WHERE contype='f' AND conrelid='workflow_version'::regclass
              AND confrelid NOT IN ('workflow_catalog'::regclass)""").fetchone()[0]
        assert user_fks == 0
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(database_url))
    try:
        with engine.begin() as conn:
            conn.execute(update(WorkflowEmbedding).where(WorkflowEmbedding.version_id == first["version_id"]).values(embedding=[0.1]*1024))
            vector = conn.execute(select(WorkflowEmbedding.embedding)).scalar_one()
            assert len(vector) == 1024 and vector[0] == pytest.approx(0.1)
    finally:
        engine.dispose()


def test_migration_rejects_existing_embedding_table(database_url):
    assert migrate(database_url, "ew_0002").returncode == 0
    with psycopg.connect(database_url) as conn:
        conn.execute("CREATE TABLE workflow_embeddings (legacy_value TEXT)")
    result = migrate(database_url)
    assert result.returncode != 0 and "Agent DB / chatapp DB separation" in result.stderr
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0] == "ew_0002"
        assert conn.execute("SELECT to_regclass('workflow_catalog_legacy')").fetchone()[0] is None


def assert_api_schema(schema_name, data):
    """Validate router projections against the real OpenAPI, not copied schemas."""
    from datetime import datetime, timezone
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
    contract = yaml.safe_load((ROOT / "docs/workflow_catalog_api/openapi.yaml").read_text())
    schema = {"$ref": "#/components/schemas/" + schema_name, "components": contract["components"]}
    def encode(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        return str(value)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(json.loads(json.dumps(data, default=encode)))


@pytest.mark.asyncio
async def test_openapi_examples_and_response_projections(service):
    import yaml
    contract = yaml.safe_load((ROOT / "docs/workflow_catalog_api/openapi.yaml").read_text())
    examples = contract["paths"]["/workflows"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    for example in examples.values():
        response = await service.register(**deepcopy(example["value"]))
        keys = contract["components"]["schemas"]["WriteResponse"]["required"]
        assert_api_schema("WriteResponse", {k: response[k] for k in keys})
        changed = await service.set_recommendation(response["workflow_id"], recommend_yn="Y", updated_by=USER)
        keys = contract["components"]["schemas"]["RecommendationResponse"]["required"]
        assert_api_schema("RecommendationResponse", {k: changed[k] for k in keys})
        detail = await service.get(response["workflow_id"])
        version = {k: detail[k] for k in ("version_id", "revision", "workflow", "registration_type", "project_id", "session_id", "task_id", "search_text")}
        version.update(created_by=detail["version_created_by"], created_at=detail["version_created_at"])
        result = {k: detail[k] for k in ("workflow_id", "current_revision", "recommend_yn", "created_by", "updated_by", "created_at", "updated_at")}
        result["version"] = version
        assert_api_schema("DetailResponse", result)
    assert_api_schema("ListResponse", await service.list())
    assert_api_schema("SearchResponse", await service.search("NCE"))


@pytest.mark.asyncio
async def test_recommendation_repeat_preserves_user_and_time(service, database_url):
    first = await register(service)
    repeat = await service.set_recommendation(first["workflow_id"], recommend_yn="N", updated_by=uuid4())
    assert repeat["updated_at"] == first["updated_at"] and repeat["updated_by"] == USER
    changed = await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=uuid4())
    repeat = await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=uuid4())
    assert repeat["updated_at"] == changed["updated_at"] and repeat["updated_by"] == changed["updated_by"]
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workflow_embeddings").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_management_list_filters_pagination_and_literal_q(service):
    created = []
    for name in ("Alpha", "A_%Literal", "Zulu"):
        document = deepcopy(DOCUMENT)
        document["workflow"]["name"] = name
        document["workflow"]["user_request"] = "Unique source request"
        created.append(await service.register(workflow=document, registration_type="AGENT" if name == "Zulu" else "USER", created_by=USER))
    await service.set_recommendation(created[0]["workflow_id"], recommend_yn="Y", updated_by=USER)
    class NoEmbeddingForList:
        model = "mock/bge-m3/1024/v1"
        async def embed(self, text):
            raise AssertionError("Management list must not call the embedding provider")
    service.embedding_provider = NoEmbeddingForList()
    page = await service.list(page_size=1, page=2, sort_by="name", sort_order="asc")
    assert page["total_count"] == 3 and page["items"][0]["name"] == "Alpha"
    assert_api_schema("ListResponse", page)
    beyond = await service.list(page=10, page_size=1)
    assert beyond["total_count"] == 3 and beyond["items"] == []
    filtered = await service.list(q=" _%lIT ", registration_type="USER", created_by=USER, execution_mode="static", recommend_yn="N")
    assert filtered["total_count"] == 1 and filtered["items"][0]["name"] == "A_%Literal"
    assert (await service.list(q="unique SOURCE"))["total_count"] == 3
    assert (await service.list(q="  ", recommend_yn="Y"))["total_count"] == 1
    assert (await service.list(created_by=uuid4()))["total_count"] == 0
    assert (await service.list(execution_mode="adaptive"))["items"] == []


@pytest.mark.asyncio
async def test_search_threshold_mode_default_and_tie_order(service, database_url):
    class ConstantProvider:
        model = "test/bge-m3"
        async def embed(self, text):
            assert text == text.strip()
            return [1.0] + [0.0]*1023
    service.embedding_provider = ConstantProvider()
    ids = []
    for _ in range(4):
        row = await register(service)
        ids.append(row["workflow_id"])
        await service.set_recommendation(row["workflow_id"], recommend_yn="Y", updated_by=USER)
    result = await service.search("  Query  ")
    assert result["limit"] == result["returned_count"] == 3
    assert [x["workflow_id"] for x in result["items"]] == sorted(ids)[:3]
    assert all(x["similarity"] == pytest.approx(1) and x["search_text"] for x in result["items"])
    assert_api_schema("SearchResponse", result)
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE workflow_embeddings SET embedding=%s::vector", (json.dumps([-1.0]+[0.0]*1023),))
    assert (await service.search("Query", threshold=0))["returned_count"] == 0
    assert (await service.search("Query", threshold=-1, limit=20))["returned_count"] == 4
    assert (await service.search("Query", execution_mode="adaptive"))["items"] == []


@pytest.mark.asyncio
async def test_historical_search_text_and_missing_version(service, database_url):
    first = await register(service)
    assert (await service.get(first["workflow_id"], revision=1))["search_text"]
    await service.update(first["workflow_id"], workflow=DOCUMENT, registration_type="AGENT", updated_by=USER, expected_revision=1)
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE workflow_embeddings SET embedding_model='other' WHERE version_id=%s", (first["version_id"],))
    assert (await service.get(first["workflow_id"], revision=1))["search_text"] is None
    with pytest.raises(WorkflowVersionNotFoundError):
        await service.get(first["workflow_id"], revision=99)
    with pytest.raises(WorkflowNotFoundError) as error:
        await service.get(uuid4(), revision=99)
    assert error.value.code == "WORKFLOW_NOT_FOUND"


@pytest.mark.parametrize("kind", ["timeout", "bad_dimension", "nan", "zero"])
@pytest.mark.asyncio
async def test_embedding_errors_are_distinct_and_atomic(service, database_url, kind):
    class BadProvider:
        model = "bge-m3"
        async def embed(self, text):
            if kind == "timeout":
                raise TimeoutError("private provider details")
            if kind == "bad_dimension":
                return [1.0]*3
            if kind == "nan":
                return [float('nan')]*1024
            return [0.0]*1024
    service.embedding_provider = BadProvider()
    expected = WorkflowEmbeddingTimeoutError if kind == "timeout" else WorkflowEmbeddingError
    for call in (register(service), service.search("Query")):
        with pytest.raises(expected) as error:
            await call
        assert "private" not in str(error.value)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_catalog").fetchone()[0] == 0


@pytest.mark.parametrize("method,kwargs", [
    ("search", {"limit":21}), ("search", {"threshold":1.1}),
    ("search", {"threshold":float('nan')}), ("search", {"threshold":True}),
    ("search", {"execution_mode":"unknown"}), ("list", {"page":0}),
    ("list", {"page_size":101}), ("list", {"sort_by":"workflow;drop"}),
    ("list", {"sort_order":"unknown"}), ("list", {"registration_type":"unknown"}),
])
@pytest.mark.asyncio
async def test_openapi_parameter_ranges(service, method, kwargs):
    with pytest.raises(ValueError):
        if method == "search":
            await service.search("Query", **kwargs)
        else:
            await service.list(**kwargs)


@pytest.mark.asyncio
async def test_update_rechecks_recommendation_and_delete_after_embedding(service, database_url):
    row = await register(service)
    class GateProvider(MockEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()
        async def embed(self, text):
            self.started.set()
            await self.release.wait()
            return await super().embed(text)
    for should_delete in (False, True):
        gate = GateProvider()
        service.embedding_provider = gate
        revision = (await service.get(row["workflow_id"]))["current_revision"]
        task = asyncio.create_task(service.update(row["workflow_id"], expected_revision=revision,
                                                  workflow=DOCUMENT, registration_type="USER", updated_by=USER))
        try:
            await asyncio.wait_for(gate.started.wait(), timeout=5)
            if should_delete:
                await service.delete(row["workflow_id"], deleted_by=USER)
            else:
                # This finishes while embedding is paused: no catalog lock is held.
                await asyncio.wait_for(service.set_recommendation(row["workflow_id"], recommend_yn="Y", updated_by=USER), timeout=5)
            gate.release.set()
            if should_delete:
                with pytest.raises(WorkflowNotFoundError):
                    await task
            else:
                assert (await task)["recommend_yn"] == "Y"
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM workflow_embeddings").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_http_provider_registration_update_and_search(service, database_url):
    import httpx
    from app.services.workflow_embedding_provider import OpenAICompatibleEmbeddingProvider
    calls = []
    def transport(request):
        body = json.loads(request.content)
        assert body["model"] == "bge-m3" and body["input"]
        calls.append(body["input"])
        return httpx.Response(200, json={"data":[{"index":0,"embedding":[0.25]*1024}],"model":"bge-m3"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.embedding_provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://model.test/v1", api_key="test-key", model="bge-m3", client=client)
        first = await register(service)
        await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=USER)
        result = await service.search("user question")
        assert result["embedding_model"] == "bge-m3" and result["returned_count"] == 1
        await service.update(first["workflow_id"], workflow=DOCUMENT, registration_type="USER",
                             updated_by=USER, expected_revision=1)
    assert len(calls) == 3 and calls[1] == "user question"
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT embedding_model,embedding_dimension,vector_dims(embedding) FROM workflow_embeddings").fetchall() == [("bge-m3",1024,1024)]*2


@pytest.mark.asyncio
async def test_agent_generation_recommendation_and_execution_version_mapping(service, database_url, tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
    from app.test.test_workflow_recommendation_integration import dependencies, settings, state
    class ConstantProvider:
        model = "test/agent/1024"
        async def embed(self, text):
            return [0.5]*1024
    service.embedding_provider = ConstantProvider()
    deps, generator = dependencies(service)
    store = PostgresWorkflowStore(database_url)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver(), workflow_store=store)
    generated_state = state()
    generated = await graph.ainvoke(generated_state, config={"configurable":{"thread_id":generated_state["session_id"]}})
    candidate = generated["workflow_candidates"][0]
    assert candidate["origin"] == "generated" and len(generator.calls) == 1
    first = await service.get(candidate["workflow_id"])
    assert first["recommend_yn"] == "N" and first["registration_type"] == "AGENT"
    assert first["created_by"] == UUID(generated_state["user_id"])
    assert "inputs" not in first["workflow"]["workflow"]
    await service.set_recommendation(first["workflow_id"], recommend_yn="Y", updated_by=USER)
    second = await service.update(first["workflow_id"], expected_revision=1, workflow=first["workflow"],
                                  registration_type="AGENT", updated_by=USER)
    next_state = state()
    config = {"configurable":{"thread_id":next_state["session_id"]}}
    recommended = await graph.ainvoke(next_state, config=config)
    chosen = recommended["workflow_candidates"][0]
    assert chosen["origin"] == "recommended" and chosen["revision"] == 2
    assert chosen["version_id"] == str(second["version_id"]) and len(generator.calls) == 1
    selected = await graph.ainvoke(Command(resume={"intent":"select","candidate_id":chosen["id"]}), config=config)
    assert selected["workflow_content_revision"] == 2
    execution_id = str(uuid4())
    await store.start_execution(execution_id=execution_id, version_id=selected["workflow_version_id"],
                                session_id=selected["session_id"], task_id=selected["task_id"],
                                revision=selected["workflow_content_revision"], workflow=selected["workflow"], status="running")
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT workflow_id,workflow_revision,version_id FROM workflow_executions WHERE execution_id=%s", (UUID(execution_id),)).fetchone() == (str(first["workflow_id"]),2,second["version_id"])
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM workflow_version WHERE version_id=%s",(first['version_id'],)).fetchone()[0] == 1
    assert 'execution_plan_id' not in candidate and 'execution_plan_id' not in chosen


@pytest.mark.asyncio
async def test_three_recommendations_keep_distinct_version_mappings(service, database_url, tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
    from app.test.test_workflow_recommendation_integration import dependencies, settings, state
    class ConstantProvider:
        model = "test/agent/multiple"
        async def embed(self, text):
            return [0.5] * 1024
    service.embedding_provider = ConstantProvider()
    eligible = []
    for _ in range(4):
        row = await register(service)
        await service.set_recommendation(row["workflow_id"], recommend_yn="Y", updated_by=USER)
        eligible.append(row)
    await register(service)  # recommend_yn=N is excluded.
    deleted = await register(service)
    await service.set_recommendation(deleted["workflow_id"], recommend_yn="Y", updated_by=USER)
    await service.delete(deleted["workflow_id"], deleted_by=USER)
    deps, generator = dependencies(service)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver(),
                                         workflow_store=PostgresWorkflowStore(database_url))
    initial = state()
    config = {"configurable": {"thread_id": initial["session_id"]}}
    result = await graph.ainvoke(initial, config=config)
    candidates = result["workflow_candidates"]
    assert len(candidates) == 3 and generator.calls == []
    expected = sorted(eligible, key=lambda row: row["workflow_id"])[:3]
    assert [c["workflow_id"] for c in candidates] == [str(row["workflow_id"]) for row in expected]
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_executions").fetchone()[0] == 0
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
        assert {UUID(c['version_id']) for c in candidates} <= {row['version_id'] for row in eligible}
    selected = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": candidates[2]["id"]}), config=config)
    assert selected["workflow_version_id"] == candidates[2]["version_id"]
    assert 'execution_plan_id' not in candidates[2]


@pytest.mark.asyncio
async def test_search_excludes_previous_ids_before_limiting(service):
    class ConstantProvider:
        model = "test/search/exclusion"
        async def embed(self, text):
            return [0.5] * 1024
    service.embedding_provider = ConstantProvider()
    rows = []
    for _ in range(5):
        row = await register(service)
        await service.set_recommendation(row["workflow_id"], recommend_yn="Y", updated_by=USER)
        rows.append(row)
    first = await service.search("request", threshold=0.8, limit=3)
    excluded = [item["workflow_id"] for item in first["items"]]
    second = await service.search("request", threshold=0.8, limit=3, exclude_workflow_ids=excluded)
    assert len(first["items"]) == 3 and len(second["items"]) == 2
    assert not set(excluded) & {item["workflow_id"] for item in second["items"]}
    assert not (await service.search("request", threshold=0.8, exclude_workflow_ids=[row["workflow_id"] for row in rows]))["items"]


def test_execution_migration_preserves_all_legacy_metadata_and_history(database_url):
    assert migrate(database_url, "ew_0002").returncode == 0
    plans, execution = seed_legacy(database_url)
    with psycopg.connect(database_url) as conn:
        conn.execute("UPDATE workflow_catalog SET success_count=3,failure_count=2,validation_status='validated',source_execution_id=%s", (execution,))
        old = dict(conn.execute("SELECT catalog_id,to_jsonb(c) FROM workflow_catalog c"))
        history = conn.execute("SELECT * FROM workflow_adaptive_history").fetchall()
        executions = conn.execute("SELECT original_workflow,final_workflow,execution_status,started_at FROM workflow_executions").fetchall()
    result = migrate(database_url, "ew_0004", preserve=True)
    assert result.returncode == 0, result.stderr
    with psycopg.connect(database_url) as conn:
        assert dict(conn.execute("SELECT execution_plan_id,legacy_metadata FROM workflow_execution_plans")) == old
        assert conn.execute("SELECT original_workflow,final_workflow,execution_status,started_at FROM workflow_executions").fetchall() == executions
        assert conn.execute("SELECT execution_plan_id,version_id FROM workflow_executions").fetchone() == (plans[0],None)
        assert conn.execute("SELECT * FROM workflow_adaptive_history").fetchall() == history
        assert conn.execute("SELECT execution_plan_id,version_id FROM workflow_adaptive_history_view").fetchone() == (plans[0],None)
        assert conn.execute("SELECT to_regclass('workflow_catalog_legacy'),to_regclass('workflow_catalog_legacy_mapping')").fetchone() == (None,None)
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0] == 0
        assert conn.execute("SELECT confrelid::regclass::text FROM pg_constraint WHERE conname='workflow_executions_plan_fkey'").fetchone()[0] == 'workflow_execution_plans'


@pytest.mark.asyncio
async def test_execution_version_snapshot_inputs_and_retry_identity(service, database_url):
    from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
    first = await register(service)
    store = PostgresWorkflowStore(database_url)
    sid,tid,execution = str(uuid4()),str(uuid4()),str(uuid4())
    compiled = compile_workflow(DOCUMENT, workflow_id=str(first['workflow_id']))
    args = dict(execution_id=execution,session_id=sid,task_id=tid,
                revision=1, workflow=compiled,status='submitted', version_id=str(first['version_id']),
                execution_inputs=[{'parameter':'lot_cd','value':'LOT_A'}])
    await store.start_execution(**args)
    await store.start_execution(**{**args,'workflow':{'workflow':{'id':str(first['workflow_id'])}},'execution_inputs':[]})
    second = await service.update(first['workflow_id'],expected_revision=1,workflow=DOCUMENT,registration_type='USER',updated_by=USER)
    with pytest.raises(ValueError, match='version'):
        await store.start_execution(**{**args,'version_id':str(second['version_id'])})
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_id,original_workflow,execution_inputs FROM workflow_executions").fetchone() == (first['version_id'],compiled,args['execution_inputs'])
        assert conn.execute("SELECT current_revision FROM workflow_catalog").fetchone()[0] == 2
    with psycopg.connect(database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE workflow_executions SET version_id=%s", (second['version_id'],))
    with pytest.raises(ValueError,match='another Workflow version'):
        await store.start_execution(**{**args,'version_id':str(second['version_id']),'revision':2})
    with pytest.raises(ValueError,match='another Workflow version'):
        await store.start_execution(**{**args,'task_id':str(uuid4())})
    await store.finish_execution(execution_id=execution,status='SUCCEEDED',final_workflow=compiled,successful=False)
    await store.start_execution(**args)
    await service.delete(first['workflow_id'],deleted_by=USER)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_id,execution_status,successful FROM workflow_executions").fetchone() == (first['version_id'],'SUCCEEDED',False)
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None


def test_execution_migration_blocks_unreviewed_external_fk(database_url):
    assert migrate(database_url, 'ew_0002').returncode == 0
    plans,execution = seed_legacy(database_url)
    assert migrate(database_url, 'ew_0003', preserve=True).returncode == 0
    with psycopg.connect(database_url) as conn:
        conn.execute("CREATE TABLE external_reference (catalog_id UUID REFERENCES workflow_catalog_legacy(catalog_id))")
        conn.execute("INSERT INTO external_reference VALUES (%s)",(plans[0],))
    result = migrate(database_url)
    assert result.returncode != 0
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0] == 'ew_0003'
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
        assert conn.execute("SELECT catalog_id FROM workflow_executions").fetchone()[0] == plans[0]
        assert conn.execute("SELECT count(*) FROM workflow_catalog_legacy").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('valid_mapping', [True,False])
async def test_migration_maps_known_version_and_rejects_wrong_mapping(database_url, valid_mapping):
    assert migrate(database_url,'ew_0002').returncode == 0
    plans,execution = seed_legacy(database_url)
    assert migrate(database_url,'ew_0003',preserve=True).returncode == 0
    catalog = WorkflowCatalogService(database_url,MockEmbeddingProvider())
    try:
        row = await register(catalog)
    finally:
        await close_async_io_resources()
    with psycopg.connect(database_url) as conn:
        if valid_mapping:
            conn.execute("UPDATE workflow_catalog_legacy SET workflow_id=%s WHERE catalog_id=%s",(str(row['workflow_id']),plans[0]))
            conn.execute("UPDATE workflow_executions SET workflow_id=%s WHERE execution_id=%s",(str(row['workflow_id']),execution))
        conn.execute("UPDATE workflow_catalog_legacy_mapping SET version_id=%s,mapped_at=now() WHERE catalog_id=%s",(row['version_id'],plans[0]))
    result = migrate(database_url,"ew_0004")
    if valid_mapping:
        assert result.returncode == 0,result.stderr
        with psycopg.connect(database_url) as conn:
            assert conn.execute("SELECT execution_plan_id,version_id FROM workflow_executions").fetchone() == (plans[0],row['version_id'])
            assert conn.execute("SELECT version_id FROM workflow_execution_plans WHERE execution_plan_id=%s",(plans[0],)).fetchone()[0] == row['version_id']
    else:
        assert result.returncode != 0 and 'mapping does not match' in result.stderr
        with psycopg.connect(database_url) as conn:
            assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0] == 'ew_0003'
            assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
            assert conn.execute("SELECT count(*) FROM workflow_catalog_legacy").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_direct_execution_inputs_reject_nonarray_and_missing_version(service,database_url):
    row = await register(service)
    store = PostgresWorkflowStore(database_url)
    args = dict(execution_id=str(uuid4()),session_id=str(uuid4()),task_id=str(uuid4()),revision=1,
                workflow={'workflow':{'id':str(row['workflow_id'])}},status='running',version_id=str(row['version_id']))
    with pytest.raises(ValueError,match='array'):
        await store.start_execution(**args,execution_inputs={})
    with pytest.raises(ValueError,match='version'):
        await store.start_execution(**{**args,'version_id':str(uuid4())})
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_executions").fetchone()[0] == 0


def test_plan_removal_skips_export_and_preserves_execution_history(database_url,tmp_path):
    assert migrate(database_url,'ew_0002').returncode == 0
    plans,execution = seed_legacy(database_url)
    assert migrate(database_url,'ew_0004',preserve=True).returncode == 0
    with psycopg.connect(database_url) as conn:
        before=conn.execute("SELECT execution_id,version_id,original_workflow,final_workflow,execution_inputs,execution_status,started_at FROM workflow_executions").fetchall()
        old=dict(conn.execute("SELECT execution_plan_id,legacy_metadata FROM workflow_execution_plans"))
        adaptive=conn.execute("SELECT * FROM workflow_adaptive_history").fetchall()
    result=migrate(database_url,archive_dir=tmp_path)
    assert result.returncode == 0,result.stderr
    assert not list(tmp_path.glob('execution_plans_ew_0005_*.json'))
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
        assert conn.execute("SELECT execution_id,version_id,original_workflow,final_workflow,execution_inputs,execution_status,started_at FROM workflow_executions").fetchall()==before
        assert conn.execute("SELECT * FROM workflow_adaptive_history").fetchall()==adaptive
        assert conn.execute("SELECT version_id FROM workflow_adaptive_history_view").fetchone()==(None,)
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='workflow_executions' AND column_name='execution_plan_id'").fetchone()[0]==0


def test_plan_removal_does_not_need_writable_archive_directory(database_url,tmp_path):
    assert migrate(database_url,'ew_0002').returncode==0
    plans,execution=seed_legacy(database_url)
    assert migrate(database_url,'ew_0004',preserve=True).returncode==0
    invalid=tmp_path/'not-a-directory'
    invalid.write_text('occupied')
    result=migrate(database_url,archive_dir=invalid)
    assert result.returncode == 0,result.stderr
    assert invalid.read_text()=='occupied'
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0]=='ew_0006'
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
        assert conn.execute("SELECT execution_id FROM workflow_executions").fetchone()[0]==execution


def test_plan_removal_external_dependency_rolls_back(database_url,tmp_path):
    assert migrate(database_url,'ew_0002').returncode==0
    plans,execution=seed_legacy(database_url)
    assert migrate(database_url,'ew_0004',preserve=True).returncode==0
    with psycopg.connect(database_url) as conn:
        conn.execute("CREATE TABLE external_plan_reference(plan_id UUID REFERENCES workflow_execution_plans(execution_plan_id))")
    result=migrate(database_url,archive_dir=tmp_path)
    assert result.returncode != 0
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM ew_alembic_version").fetchone()[0]=='ew_0004'
        assert conn.execute("SELECT count(*) FROM workflow_execution_plans").fetchone()[0]==2
        assert conn.execute("SELECT execution_plan_id FROM workflow_executions").fetchone()[0]==plans[0]
    assert not list(tmp_path.glob('execution_plans_ew_0005_*.json'))


@pytest.mark.asyncio
async def test_known_execution_version_survives_plan_removal(database_url,tmp_path):
    assert migrate(database_url,'ew_0004').returncode==0
    service=WorkflowCatalogService(database_url,MockEmbeddingProvider())
    try:
        first=await register(service)
    finally:
        await close_async_io_resources()
    plan,execution=uuid4(),uuid4()
    sid,tid=str(uuid4()),str(uuid4())
    original={'schema_version':'1.3','workflow':{'id':str(first['workflow_id']),'inputs':{'lot_cd':'LOT_A'}}}
    inputs=[{'parameter':'lot_cd','value':'LOT_A'}]
    with psycopg.connect(database_url) as conn:
        conn.execute("INSERT INTO workflow_execution_plans(execution_plan_id,version_id,workflow_id,revision,session_id,task_id,intent,compiled_workflow) VALUES (%s,%s,%s,1,%s,%s,'analysis',%s)",(plan,first['version_id'],str(first['workflow_id']),sid,tid,Jsonb(original)))
        conn.execute("INSERT INTO workflow_executions(execution_id,execution_plan_id,version_id,workflow_id,workflow_revision,session_id,task_id,original_workflow,execution_inputs) VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s)",(execution,plan,first['version_id'],str(first['workflow_id']),sid,tid,Jsonb(original),Jsonb(inputs)))
    result=migrate(database_url,archive_dir=tmp_path)
    assert result.returncode==0,result.stderr
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT version_id,original_workflow,execution_inputs FROM workflow_executions").fetchone()==(first['version_id'],original,inputs)
        assert conn.execute("SELECT count(*) FROM workflow_version").fetchone()[0]==1
        assert conn.execute("SELECT to_regclass('workflow_execution_plans')").fetchone()[0] is None
