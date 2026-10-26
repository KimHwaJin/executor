"""Offline HTTP, transaction boundary, pool reuse and cancellation checks."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import httpx

from agent_config import load_agent_settings
from app.services import async_io_resources as resources
from app.services import executor_client as executor
from app.services.workflow_persistence import PostgresWorkflowStore


class AsyncExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await resources.close_async_io_resources()

    async def test_post_preserves_body_timeout_and_response(self):
        payload = {"idempotency_key": "once", "context": {"task_id": "task"},
                   "text": "보고서"}

        async def handle(request):
            self.assertEqual(json.loads(request.content), payload)
            self.assertEqual(request.headers["Content-Type"], "application/json")
            self.assertEqual(request.extensions["timeout"]["read"], 7)
            return httpx.Response(202, json={"accepted": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(executor, "executor_http_client", return_value=client):
                result = await executor._post_json("http://executor/start", payload,
                                                   timeout_seconds=7)
        self.assertEqual(result, {"status_code": 202, "body": {"accepted": True}})

    async def test_get_notebook_query(self):
        async def handle(request):
            self.assertEqual(dict(request.url.params),
                             {"view": "FULL", "start_index": "200", "limit": "2"})
            return httpx.Response(200, json={"cells": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(executor, "executor_http_client", return_value=client):
                result = await executor.get_execution_notebook(
                    load_agent_settings({}), "execution", start_index=200, limit=2)
        self.assertEqual(result["body"], {"cells": []})

    async def test_http_failure_and_timeout_are_not_retried(self):
        for failure in (503, "timeout"):
            calls = []

            async def handle(request):
                calls.append(request)
                if failure == "timeout":
                    raise httpx.ReadTimeout("deadline", request=request)
                return httpx.Response(failure, text="unavailable")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with patch.object(executor, "executor_http_client", return_value=client), \
                     self.assertLogs(executor.logger, level="ERROR"), \
                     self.assertRaises(executor.ExecutorSubmitError):
                    await executor._post_json("http://executor/start", {}, timeout_seconds=1)
            self.assertEqual(len(calls), 1)

    async def test_get_rejects_non_object_json(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=[]))) as client:
            with patch.object(executor, "executor_http_client", return_value=client), \
                 self.assertRaisesRegex(executor.ExecutorSubmitError, "JSON object"):
                await executor._get_json("http://executor/result", timeout_seconds=1)

    async def test_http_waits_overlap_and_cancel_propagates(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0
        cancelled = []

        async def handle(request):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            try:
                await release.wait()
                return httpx.Response(200, json={})
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(executor, "executor_http_client", return_value=client):
                tasks = [asyncio.create_task(executor._get_json(
                    "http://executor/result", timeout_seconds=1)) for _ in range(2)]
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    tasks[0].cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await tasks[0]
                    release.set()
                    self.assertEqual((await tasks[1])["body"], {})
                    self.assertEqual(cancelled, [True])
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_pools_are_reused_and_closed_by_host(self):
        pool = Mock(open=AsyncMock(), close=AsyncMock())
        with patch.object(resources, "AsyncConnectionPool", return_value=pool) as factory:
            first, second = await asyncio.gather(
                resources.workflow_connection_pool("postgresql://unused"),
                resources.workflow_connection_pool("postgresql://unused"))
        self.assertIs(first, second)
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["open"])
        self.assertEqual(factory.call_args.kwargs["min_size"], 0)
        http = resources.executor_http_client()
        self.assertIs(http, resources.executor_http_client())
        await resources.close_async_io_resources()
        self.assertTrue(http.is_closed)
        pool.close.assert_awaited_once()
        await resources.close_async_io_resources()  # shutdown is idempotent


class AsyncWorkflowStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.execution_id = str(uuid4())
        self.catalog_id = str(uuid4())
        self.cursor = Mock(fetchone=AsyncMock(return_value=(self.catalog_id, None)))
        self.connection = Mock(execute=AsyncMock(return_value=self.cursor))
        self.transaction = MagicMock(
            __aenter__=AsyncMock(return_value=self.connection),
            __aexit__=AsyncMock(return_value=False))
        self.pool = Mock(connection=Mock(return_value=self.transaction))
        patcher = patch("app.services.workflow_persistence.workflow_connection_pool",
                        new=AsyncMock(return_value=self.pool))
        self.pool_getter = patcher.start()
        self.addCleanup(patcher.stop)
        self.store = PostgresWorkflowStore("postgresql+psycopg://unused")

    async def test_execution_write_uses_pool_transaction(self):
        await self.store.start_execution(
            execution_id=self.execution_id,
            session_id=str(uuid4()), task_id=str(uuid4()), revision=2,
            workflow={"workflow": {"id": "workflow"}}, status="submitted")
        self.pool.connection.assert_called_once()
        self.assertEqual(self.connection.execute.await_count, 1)
        self.transaction.__aexit__.assert_awaited_once_with(None, None, None)
        self.pool_getter.assert_awaited_once_with("postgresql://unused")

    async def test_finish_updates_only_unfinished_executions(self):
        await self.store.finish_execution(execution_id=self.execution_id,
            status="SUCCEEDED", final_workflow={}, successful=False)
        query, parameters = self.connection.execute.call_args.args
        self.assertIn("finished_at IS NULL", query)
        self.assertFalse(parameters[2])
        self.assertEqual(self.connection.execute.await_count, 1)

    async def test_error_leaves_transaction_with_exception_for_rollback(self):
        self.connection.execute.side_effect = RuntimeError("DB failure")
        with self.assertRaisesRegex(RuntimeError, "DB failure"):
            await self.store.record_adaptive_round(execution_id=self.execution_id,
                decision_round=1, changes=[], effective_workflow={})
        self.assertIs(self.transaction.__aexit__.call_args.args[0], RuntimeError)

    async def test_cancellation_exits_transaction(self):
        entered = asyncio.Event()

        async def execute(*args):
            entered.set()
            await asyncio.Event().wait()

        self.connection.execute.side_effect = execute
        task = asyncio.create_task(self.store.record_adaptive_round(
            execution_id=self.execution_id, decision_round=1, changes=[], effective_workflow={}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIs(self.transaction.__aexit__.call_args.args[0], asyncio.CancelledError)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
