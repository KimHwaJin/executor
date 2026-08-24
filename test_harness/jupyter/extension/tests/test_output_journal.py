from __future__ import annotations

import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from executor_resource_extension.output_journal import (
    JournalIdentity,
    OutputJournalConflictError,
    OutputJournalError,
    OutputJournalStorage,
)
from executor_resource_extension.storage import RuntimeStorage


def _identity(
    workspace: str,
    *,
    fencing_token: int = 7,
    runtime_session_id: str = "kernel-session-1",
) -> JournalIdentity:
    return JournalIdentity.from_mapping(
        {
            "workspace_path": workspace,
            "execution_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "step_id": "33333333-3333-4333-8333-333333333333",
            "sequence": 0,
            "execution_attempt_id": ("44444444-4444-4444-8444-444444444444"),
            "fencing_token": fencing_token,
            "runtime_target_id": ("55555555-5555-4555-8555-555555555555"),
            "runtime_session_id": runtime_session_id,
        }
    )


def _records() -> list[dict[str, object]]:
    return [
        {
            "kind": "STREAM",
            "stream_name": "stdout",
            "representations": [
                {
                    "media_type": "text/plain",
                    "encoding": "UTF8",
                    "content": "complete text output",
                }
            ],
            "metadata": {"source": "iopub"},
        },
        {
            "kind": "DISPLAY",
            "representations": [
                {
                    "media_type": "image/png",
                    "encoding": "BASE64",
                    "content": base64.b64encode(b"png-bytes").decode(),
                }
            ],
        },
    ]


class OutputJournalStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = "users/u1/projects/p1/sessions/s1/executions/e1"
        RuntimeStorage(self.root).prepare_workspace(self.workspace)
        self.storage = OutputJournalStorage(self.root)
        self.identity = _identity(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_begin_and_append_persist_content_without_echoing_bodies(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")
        batch_id = str(uuid4())

        appended = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=batch_id,
            records=_records(),
        )

        self.assertEqual(appended["committed_offset"], 2)
        self.assertEqual(appended["output_count"], 2)
        self.assertEqual(appended["representation_count"], 2)
        self.assertEqual(appended["total_bytes"], 29)
        self.assertFalse(appended["replayed"])
        serialized = json.dumps(appended)
        self.assertNotIn("complete text output", serialized)
        self.assertNotIn(base64.b64encode(b"png-bytes").decode(), serialized)
        self.assertNotIn("storage_path", serialized)
        self.assertIn("journal://", serialized)

        content = sorted(
            path.read_bytes()
            for path in (self.root / self.workspace / "outputs").rglob("*.bin")
        )
        self.assertEqual(content, [b"complete text output", b"png-bytes"])

    def test_replaying_batch_is_idempotent_and_conflicting_body_fails(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")
        with self.assertRaises(OutputJournalConflictError):
            self.storage.begin(self.identity, "print('different')")
        batch_id = str(uuid4())
        first = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=batch_id,
            records=_records(),
        )

        replay = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=batch_id,
            records=_records(),
        )

        self.assertEqual(replay["outputs"], first["outputs"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(OutputJournalConflictError):
            self.storage.append(
                self.identity,
                journal_id=begun["journal_id"],
                expected_offset=0,
                batch_id=batch_id,
                records=[
                    {
                        "kind": "STREAM",
                        "representations": [
                            {
                                "media_type": "text/plain",
                                "encoding": "UTF8",
                                "content": "different",
                            }
                        ],
                    }
                ],
            )

    def test_offset_conflict_and_concurrent_append_allow_one_winner(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")

        with self.assertRaises(OutputJournalConflictError):
            self.storage.append(
                self.identity,
                journal_id=begun["journal_id"],
                expected_offset=10,
                batch_id=str(uuid4()),
                records=_records(),
            )

        def append(batch_id: str) -> dict[str, object]:
            return self.storage.append(
                self.identity,
                journal_id=begun["journal_id"],
                expected_offset=0,
                batch_id=batch_id,
                records=_records(),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(append, str(uuid4())) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except OutputJournalConflictError:
                outcomes.append(None)

        self.assertEqual(sum(item is not None for item in outcomes), 1)

    def test_finalize_is_idempotent_and_blocks_later_mutation(self) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")
        self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )

        finalized = self.storage.finalize(
            self.identity, journal_id=begun["journal_id"]
        )
        replay = self.storage.finalize(
            self.identity, journal_id=begun["journal_id"]
        )

        self.assertEqual(finalized, replay)
        self.assertEqual(finalized["state"], "FINALIZED")
        self.assertEqual(len(finalized["checksum_sha256"]), 64)
        with self.assertRaises(OutputJournalConflictError):
            self.storage.abort(
                self.identity,
                journal_id=begun["journal_id"],
                reason="too late",
            )
        with self.assertRaises(OutputJournalConflictError):
            self.storage.append(
                self.identity,
                journal_id=begun["journal_id"],
                expected_offset=2,
                batch_id=str(uuid4()),
                records=_records(),
            )

    def test_materializes_complete_notebook_from_terminal_journal(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "print('complete')")
        self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )
        self.storage.finalize(self.identity, journal_id=begun["journal_id"])

        result = self.storage.materialize_notebook(
            workspace_path=self.workspace,
            runtime_profile="basic",
            cells=[
                {
                    "sequence": 0,
                    "execution_count": 7,
                    "journal_id": begun["journal_id"],
                    "journal": self.identity.as_dict(),
                }
            ],
        )

        notebook_path = self.root / result["notebook_path"]
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        self.assertEqual(result["cell_count"], 1)
        self.assertEqual(result["output_count"], 2)
        self.assertEqual(notebook["nbformat"], 4)
        self.assertEqual(notebook["metadata"]["kernelspec"]["name"], "basic")
        self.assertEqual(notebook["cells"][0]["execution_count"], 7)
        self.assertEqual(
            notebook["cells"][0]["outputs"][0]["text"],
            "complete text output",
        )
        self.assertEqual(
            notebook["cells"][0]["outputs"][1]["data"]["image/png"],
            base64.b64encode(b"png-bytes").decode(),
        )

    def test_materialization_rejects_open_or_foreign_journal(self) -> None:
        begun = self.storage.begin(self.identity, "1 + 1")
        cell = {
            "sequence": 0,
            "journal_id": begun["journal_id"],
            "journal": self.identity.as_dict(),
        }
        with self.assertRaises(OutputJournalConflictError):
            self.storage.materialize_notebook(
                workspace_path=self.workspace,
                runtime_profile="basic",
                cells=[cell],
            )

        self.storage.abort(
            self.identity,
            journal_id=begun["journal_id"],
            reason="expected",
        )
        cell["journal"] = {
            **self.identity.as_dict(),
            "workspace_path": "users/u1/projects/p1/sessions/s1/executions/e2",
        }
        with self.assertRaises(OutputJournalConflictError):
            self.storage.materialize_notebook(
                workspace_path=self.workspace,
                runtime_profile="basic",
                cells=[cell],
            )

    def test_abort_is_idempotent_and_preserves_committed_content(self) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")
        self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )

        aborted = self.storage.abort(
            self.identity,
            journal_id=begun["journal_id"],
            reason="worker disconnected",
        )
        replay = self.storage.abort(
            self.identity,
            journal_id=begun["journal_id"],
            reason="a later duplicate reason",
        )

        self.assertEqual(aborted, replay)
        self.assertEqual(aborted["state"], "ABORTED")
        self.assertEqual(aborted["abort_reason"], "worker disconnected")
        self.assertEqual(
            len(list((self.root / self.workspace / "outputs").rglob("*.bin"))),
            2,
        )
        with self.assertRaises(OutputJournalConflictError):
            self.storage.finalize(
                self.identity, journal_id=begun["journal_id"]
            )

    def test_repairs_state_after_batch_commit_precedes_state_replace(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "print('journal')")
        self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )
        state_path = next(
            (self.root / self.workspace / "outputs").rglob("journal.json")
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(
            {
                "committed_offset": 0,
                "batch_count": 0,
                "output_count": 0,
                "representation_count": 0,
                "total_bytes": 0,
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")

        appended = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=2,
            batch_id=str(uuid4()),
            records=_records()[:1],
        )

        self.assertEqual(appended["committed_offset"], 3)
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["batch_count"], 2)
        self.assertEqual(repaired["output_count"], 3)

    def test_fencing_token_separates_storage_and_identity_mismatch_fails(
        self,
    ) -> None:
        first = self.storage.begin(self.identity, "print('first')")
        second = self.storage.begin(
            _identity(self.workspace, fencing_token=8), "print('second')"
        )

        self.assertNotEqual(first["journal_id"], second["journal_id"])
        journal_files = list(
            (self.root / self.workspace / "outputs").rglob("journal.json")
        )
        self.assertEqual(len(journal_files), 2)
        with self.assertRaises(OutputJournalConflictError):
            self.storage.begin(
                _identity(
                    self.workspace,
                    runtime_session_id="different-session",
                ),
                "print('different')",
            )

    def test_rejects_unsafe_identity_and_invalid_representation(self) -> None:
        with self.assertRaises(OutputJournalError):
            self.storage.begin(_identity("../escape"), "print('unsafe')")
        begun = self.storage.begin(self.identity, "print('journal')")
        with self.assertRaises(OutputJournalError):
            self.storage.append(
                self.identity,
                journal_id=begun["journal_id"],
                expected_offset=0,
                batch_id=str(uuid4()),
                records=[
                    {
                        "kind": "DISPLAY",
                        "representations": [
                            {
                                "media_type": "image/png",
                                "encoding": "BASE64",
                                "content": "not-base64!",
                            }
                        ],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
