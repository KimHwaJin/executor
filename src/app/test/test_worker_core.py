"""Worker core against real PostgreSQL and Redis.

Set EW_TEST_DATABASE_URL (a disposable PostgreSQL database; migrations are
applied here) and EW_TEST_REDIS_URL. Without both, the tests are skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg

from app.worker import (
    EventContext,
    ExecutorEvent,
    ExecutorWorker,
    IgnoreEvent,
    Route,
    Settings,
)
from app.worker.contracts import RouteTable

DATABASE_URL = os.environ.get("EW_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("EW_TEST_REDIS_URL")
ROOT = Path(__file__).resolve().parents[3]
TENANT = "core"


def setUpModule() -> None:
    if DATABASE_URL and REDIS_URL:
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "EW_DATABASE_URL": DATABASE_URL},
            check=True,
            capture_output=True,
        )


def event(
    execution_id: UUID,
    sequence: int,
    event_type: str = "execution.completed",
    payload: dict[str, Any] | None = None,
    tenant: str = TENANT,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "event_id": str(uuid4()),
        "event_type": event_type,
        "schema_version": "1.0",
        "execution_id": str(execution_id),
        "event_sequence": sequence,
        "payload": payload or {"status": "SUCCEEDED"},
        "occurred_at": datetime.now(UTC).isoformat(),
    }


BATCH_DONE = {
    "status": "SUCCEEDED",
    "execution": {"trigger": {"type": "BATCH", "actor": None}, "context": {"user_id": "u"}},
}
INTERACTIVE_DONE = {
    "status": "SUCCEEDED",
    "execution": {"trigger": {"type": "INTERACTIVE", "actor": None}, "context": {"user_id": "u"}},
}


def is_batch_completion(item: ExecutorEvent) -> bool:
    execution = item.payload.get("execution") or {}
    return (
        item.event_type == "execution.completed"
        and (execution.get("trigger") or {}).get("type") == "BATCH"
    )


def redis_fields(item: dict[str, Any]) -> dict[str, str]:
    return {
        **{key: str(value) for key, value in item.items() if key != "payload"},
        "payload": json.dumps(item["payload"]),
    }


@unittest.skipUnless(
    DATABASE_URL and REDIS_URL, "EW_TEST_DATABASE_URL/EW_TEST_REDIS_URL unset"
)
class WorkerCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.namespace = f"test-{uuid4().hex[:12]}"
        self.stream = f"{self.namespace}.events"
        self.calls: list[EventContext] = []
        self.history: dict[UUID, list[dict[str, Any]]] = {}

    def settings(self, **overrides: Any) -> Settings:
        return Settings(
            database_url=DATABASE_URL,
            redis_url=REDIS_URL,
            namespace=self.namespace,
            executor_event_stream=self.stream,
            executor_base_url="http://executor.test/api/v1",
            health_port=0,
            concurrency=2,
            poll_seconds=0.05,
            idle_poll_seconds=0.2,
            claim_idle_milliseconds=2001,
            lease_ttl_seconds=3,
            lease_renew_seconds=1,
            **overrides,
        )

    async def record(self, context: EventContext) -> None:
        self.calls.append(context)

    def history_endpoint(self, request: httpx.Request) -> httpx.Response:
        execution_id = UUID(request.url.path.split("/")[-2])
        after = int(request.url.params["after_sequence"])
        items = [
            item
            for item in self.history.get(execution_id, [])
            if item["event_sequence"] > after
        ]
        return httpx.Response(200, json={"items": items, "has_more": False})

    @asynccontextmanager
    async def running(self, routes: dict[str, Any] | list[Route], **overrides: Any):
        if isinstance(routes, dict):
            routes = [Route(TENANT, {kind}, handler) for kind, handler in routes.items()]
        async with ExecutorWorker(self.settings(**overrides), routes) as worker:
            mocked = httpx.AsyncClient(
                base_url=worker.settings.executor_base_url + "/",
                transport=httpx.MockTransport(self.history_endpoint),
            )
            await worker.http.aclose()
            worker.http = worker.router.http = mocked
            task = asyncio.create_task(worker.run())
            try:
                yield worker
            finally:
                worker.request_stop()
                await asyncio.wait_for(task, 30)
                await mocked.aclose()
                await worker.redis.delete(
                    *(stream for _, stream, _ in worker.streams),
                    *(f"{self.namespace}:{kind}:dlq" for kind, _, _ in worker.streams),
                )

    async def publish(
        self, worker: ExecutorWorker, *items: dict[str, Any], tenant: str = TENANT
    ) -> None:
        for item in items:
            await worker.redis.xadd(
                worker.settings.event_stream(tenant), redis_fields(item)
            )

    async def eventually(self, condition, timeout: float = 15) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("Condition was not reached in time")
            await asyncio.sleep(0.05)

    async def command_states(self, execution_id: UUID) -> list[tuple[int, str]]:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            cur = await conn.execute(
                """SELECT sequence, state FROM ew_commands
                WHERE namespace=%s AND execution_id=%s ORDER BY sequence""",
                (self.namespace, execution_id),
            )
            return await cur.fetchall()

    async def test_routes_handled_events_once_in_sequence_order(self) -> None:
        execution_id = uuid4()
        first = event(execution_id, 1, "execution.started")
        second = event(execution_id, 2, "execution.operation_completed")
        third = event(execution_id, 3, "execution.completed")
        handlers = {
            "execution.operation_completed": self.record,
            "execution.completed": self.record,
        }
        async with self.running(handlers) as worker:
            await worker.store.register(
                execution_id=execution_id, session_id="session-1", task_id="task-1", tenant=TENANT
            )
            await self.publish(worker, third, second, first, second)
            await self.eventually(lambda: len(self.calls) == 2)
            await asyncio.sleep(0.5)

        self.assertEqual(
            [call.event.event_sequence for call in self.calls], [2, 3]
        )
        self.assertEqual(
            {(c.session_id, c.task_id) for c in self.calls},
            {("session-1", "task-1")},
        )

    async def test_events_wait_for_the_execution_binding(self) -> None:
        execution_id = uuid4()
        async with self.running({"execution.completed": self.record}) as worker:
            await self.publish(worker, event(execution_id, 1))
            await asyncio.sleep(1)
            self.assertEqual(self.calls, [])
            await worker.store.register(
                execution_id=execution_id, session_id="session-2", task_id="task-2", tenant=TENANT
            )
            await self.eventually(lambda: len(self.calls) == 1)

    async def test_missing_sequence_is_recovered_from_executor_history(self) -> None:
        execution_id = uuid4()
        items = [
            event(execution_id, 1, "execution.operation_completed"),
            event(execution_id, 2, "execution.operation_completed"),
            event(execution_id, 3, "execution.completed"),
        ]
        self.history[execution_id] = items
        handlers = {
            "execution.operation_completed": self.record,
            "execution.completed": self.record,
        }
        async with self.running(handlers) as worker:
            await worker.store.register(
                execution_id=execution_id, session_id="session-3", task_id="task-3", tenant=TENANT
            )
            await self.publish(worker, items[0], items[2])
            await self.eventually(lambda: len(self.calls) == 3)

        self.assertEqual(
            [call.event.event_sequence for call in self.calls], [1, 2, 3]
        )

    async def test_ignored_event_does_not_block_the_next_one(self) -> None:
        execution_id = uuid4()

        async def ignore_operation(context: EventContext) -> None:
            if context.event.event_type == "execution.operation_completed":
                raise IgnoreEvent("not waiting for it")
            self.calls.append(context)

        handlers = {
            "execution.operation_completed": ignore_operation,
            "execution.completed": ignore_operation,
        }
        async with self.running(handlers) as worker:
            await worker.store.register(
                execution_id=execution_id, session_id="session-4", task_id="task-4", tenant=TENANT
            )
            await self.publish(
                worker,
                event(execution_id, 1, "execution.operation_completed"),
                event(execution_id, 2, "execution.completed"),
            )
            await self.eventually(lambda: len(self.calls) == 1)

        self.assertEqual(
            await self.command_states(execution_id), [(1, "IGNORED"), (2, "DONE")]
        )

    async def test_handler_failures_end_as_failed_after_max_attempts(self) -> None:
        execution_id = uuid4()
        attempts = 0

        async def fail(_: EventContext) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("agent failed")

        async with self.running(
            {"execution.completed": fail}, max_handler_attempts=2
        ) as worker:
            await worker.store.register(
                execution_id=execution_id, session_id="session-5", task_id="task-5", tenant=TENANT
            )
            await self.publish(worker, event(execution_id, 1))
            await self.eventually(lambda: attempts == 2, timeout=20)
            await asyncio.sleep(0.5)

        self.assertEqual(await self.command_states(execution_id), [(1, "FAILED")])

    async def test_one_tenant_serves_registered_and_claimed_executions(self) -> None:
        agent_id, batch_id, waiting_id = uuid4(), uuid4(), uuid4()
        handled: list[tuple[str, EventContext]] = []

        def handler(name: str):
            async def handle(context: EventContext) -> None:
                handled.append((name, context))

            return handle

        routes = [
            Route("svc", {"execution.completed"}, handler("agent")),
            Route("svc", {"execution.completed"}, handler("report"), claim=is_batch_completion),
        ]
        async with self.running(routes) as worker:
            await worker.store.register(
                execution_id=agent_id, session_id="s-1", task_id="t-1", tenant="svc"
            )
            await self.publish(
                worker,
                event(agent_id, 1, payload=INTERACTIVE_DONE, tenant="svc"),
                event(batch_id, 1, "execution.started", tenant="svc"),
                event(batch_id, 2, "execution.step_completed", tenant="svc"),
                event(batch_id, 3, payload=BATCH_DONE, tenant="svc"),
                event(waiting_id, 1, payload=INTERACTIVE_DONE, tenant="svc"),
                tenant="svc",
            )
            await self.eventually(lambda: len(handled) == 2)
            await asyncio.sleep(1)
            # An unregistered INTERACTIVE Execution keeps waiting for its Agent.
            self.assertEqual(len(handled), 2)
            await worker.store.register(
                execution_id=waiting_id, session_id="s-2", task_id="t-2", tenant="svc"
            )
            await self.eventually(lambda: len(handled) == 3)

        by_execution = {context.execution_id: (name, context) for name, context in handled}
        self.assertEqual(by_execution[agent_id][0], "agent")
        self.assertEqual(by_execution[waiting_id][0], "agent")
        name, claimed = by_execution[batch_id]
        self.assertEqual(
            (name, claimed.tenant, claimed.session_id, claimed.task_id, claimed.event.event_sequence),
            ("report", "svc", f"svc:{batch_id}", None, 3),
        )
        self.assertEqual(await self.command_states(batch_id), [(3, "DONE")])

    async def test_claim_never_rebinds_a_registered_execution(self) -> None:
        execution_id = uuid4()
        routes = [
            Route("svc", {"execution.completed"}, self.record),
            Route("svc", {"execution.started"}, self.record, claim=lambda _: True),
        ]
        async with self.running(routes) as worker:
            await worker.store.register(
                execution_id=execution_id, session_id="s-3", task_id="t-3", tenant="svc"
            )
            await self.publish(
                worker,
                event(execution_id, 1, "execution.started", tenant="svc"),
                event(execution_id, 2, tenant="svc"),
                tenant="svc",
            )
            await self.eventually(lambda: len(self.calls) == 1)

        (call,) = self.calls
        self.assertEqual((call.task_id, call.event.event_sequence), ("t-3", 2))

    async def test_each_tenant_stream_reaches_its_own_handler(self) -> None:
        report_id, flow_id = uuid4(), uuid4()
        handled: list[tuple[str, UUID]] = []

        def handler(name: str):
            async def handle(context: EventContext) -> None:
                handled.append((name, context.execution_id))

            return handle

        routes = [
            Route("report", {"execution.completed"}, handler("report"), claim=is_batch_completion),
            Route("flow", {"execution.completed"}, handler("flow")),
        ]
        async with self.running(routes) as worker:
            await worker.store.register(
                execution_id=flow_id, session_id="s-flow", task_id="t-flow", tenant="flow"
            )
            await self.publish(
                worker, event(report_id, 1, payload=BATCH_DONE, tenant="report"), tenant="report"
            )
            await self.publish(worker, event(flow_id, 1, tenant="flow"), tenant="flow")
            await self.eventually(lambda: len(handled) == 2)

        self.assertCountEqual(handled, [("report", report_id), ("flow", flow_id)])

    async def test_event_whose_tenant_does_not_match_its_stream_is_dead_lettered(
        self,
    ) -> None:
        execution_id = uuid4()
        routes = [Route("report", {"execution.completed"}, self.record, claim=is_batch_completion)]
        async with self.running(routes) as worker:
            await self.publish(
                worker, event(execution_id, 1, payload=BATCH_DONE, tenant="other"), tenant="report"
            )
            dlq = f"{self.namespace}:ingress.report:dlq"
            for _ in range(100):
                if await worker.redis.xlen(dlq):
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(await worker.redis.xlen(dlq), 1)
        self.assertEqual(self.calls, [])

    async def test_purge_keeps_the_binding_so_redelivery_does_not_rerun(self) -> None:
        execution_id = uuid4()
        delivered = event(execution_id, 1, payload=BATCH_DONE, tenant="report")
        routes = [Route("report", {"execution.completed"}, self.record, claim=is_batch_completion)]
        async with self.running(routes, retention_days=0) as worker:
            await self.publish(worker, delivered, tenant="report")
            await self.eventually(lambda: len(self.calls) == 1)
            self.assertGreater(await worker.store.purge(0, 100), 0)
            self.assertEqual(await self.command_states(execution_id), [])
            await self.publish(worker, delivered, tenant="report")
            await asyncio.sleep(1)
            self.assertEqual(await worker.store.purge(0, 100), 1)
        self.assertEqual(len(self.calls), 1)


    async def test_claim_starts_at_the_claiming_event(self) -> None:
        execution_id = uuid4()
        routes = [Route("svc", {"execution.completed"}, self.record, claim=is_batch_completion)]
        history_reads = []
        self.history_endpoint = lambda request: (
            history_reads.append(request) or httpx.Response(200, json={"items": []})
        )
        async with self.running(routes) as worker:
            # Earlier events never arrived (trimmed, or before subscribing).
            await self.publish(
                worker, event(execution_id, 7, payload=BATCH_DONE, tenant="svc"), tenant="svc"
            )
            await self.eventually(lambda: len(self.calls) == 1)
            await self.publish(
                worker, event(execution_id, 3, "execution.started", tenant="svc"), tenant="svc"
            )
            await asyncio.sleep(1)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(history_reads, [])
        self.assertEqual(await self.command_states(execution_id), [(7, "DONE")])

    async def test_claimed_event_without_a_claim_route_is_ignored(self) -> None:
        execution_id = uuid4()
        routes = [Route("svc", {"execution.completed"}, self.record, claim=is_batch_completion)]
        async with self.running(routes) as worker:
            # Another replica's registry has no claim Route for this event.
            worker.dispatcher.routes = RouteTable(
                [Route("svc", {"execution.completed"}, self.record)]
            )
            await self.publish(
                worker, event(execution_id, 1, payload=BATCH_DONE, tenant="svc"), tenant="svc"
            )
            for _ in range(200):
                if await self.command_states(execution_id) == [(1, "IGNORED")]:
                    break
                await asyncio.sleep(0.05)

        self.assertEqual(await self.command_states(execution_id), [(1, "IGNORED")])
        self.assertEqual(self.calls, [])

    async def test_purge_removes_only_old_finished_and_unbound_rows(self) -> None:
        failed_id, unbound_id = uuid4(), uuid4()

        async def fail(_: EventContext) -> None:
            raise RuntimeError("agent failed")

        async with self.running(
            {"execution.completed": fail}, max_handler_attempts=1
        ) as worker:
            await worker.store.register(
                execution_id=failed_id, session_id="s-f", task_id="t-f", tenant=TENANT
            )
            await self.publish(worker, event(failed_id, 1), event(unbound_id, 1))
            for _ in range(200):
                if await self.command_states(failed_id) == [(1, "FAILED")]:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(await worker.store.purge(0, 100), 1)

        self.assertEqual(await self.command_states(failed_id), [(1, "FAILED")])
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            cur = await conn.execute(
                "SELECT execution_id FROM ew_inbox WHERE namespace=%s",
                (self.namespace,),
            )
            self.assertEqual([row[0] for row in await cur.fetchall()], [failed_id])


class RouteTableTests(unittest.TestCase):
    async def handler(self, _: EventContext) -> None:
        return None

    def test_registered_and_claimed_routes_share_a_tenant(self) -> None:
        table = RouteTable(
            [
                Route("svc", {"execution.completed"}, self.handler),
                Route("svc", {"execution.completed"}, self.handler, claim=is_batch_completion),
                Route("other", {"execution.completed"}, self.handler),
            ]
        )
        self.assertEqual(table.tenants, ["other", "svc"])
        self.assertIsNotNone(table.handler("svc", "execution.completed", False))
        self.assertIsNotNone(table.handler("svc", "execution.completed", True))
        self.assertIsNone(table.handler("svc", "execution.started", True))
        batch = ExecutorEvent.model_validate(event(uuid4(), 1, payload=BATCH_DONE))
        interactive = ExecutorEvent.model_validate(event(uuid4(), 1, payload=INTERACTIVE_DONE))
        self.assertTrue(table.claims("svc", batch))
        self.assertFalse(table.claims("svc", interactive))
        self.assertFalse(table.claims("other", batch))

    def test_one_handler_per_tenant_event_and_kind(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate Route"):
            RouteTable(
                [
                    Route("flow", {"execution.completed"}, self.handler),
                    Route("flow", {"execution.completed"}, self.handler),
                ]
            )

    def test_route_needs_a_valid_tenant(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid tenant"):
            Route("Report", {"execution.completed"}, self.handler)


if __name__ == "__main__":
    unittest.main()
