from __future__ import annotations

import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import nbformat
from executor_resource_extension.output_journal import (
    JournalIdentity,
    OutputJournalConflictError,
    OutputJournalError,
    OutputJournalNotFoundError,
    OutputJournalStorage,
)
from executor_resource_extension.storage import RuntimeStorage


def _identity(
    workspace: str,
    *,
    fencing_token: int = 7,
    runtime_session_id: str = "kernel-session-1",
    operation_id: str = "22222222-2222-4222-8222-222222222222",
    step_id: str = "33333333-3333-4333-8333-333333333333",
    sequence: int = 0,
) -> JournalIdentity:
    return JournalIdentity.from_mapping(
        {
            "workspace_path": workspace,
            "execution_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": operation_id,
            "step_id": step_id,
            "sequence": sequence,
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

        output_root = self.root / self.workspace / "outputs"
        journals = list(output_root.rglob("journal.jsonl"))
        images = list(output_root.rglob("*.png"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].read_bytes(), b"png-bytes")
        journal_lines = [
            json.loads(line)
            for line in journals[0].read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [line["record_type"] for line in journal_lines],
            ["HEADER", "BATCH"],
        )
        self.assertEqual(
            journal_lines[1]["outputs"][0]["representations"][0][
                "inline_content"
            ],
            "complete text output",
        )
        self.assertFalse(list(output_root.rglob("*.bin")))
        self.assertFalse(list(output_root.rglob("source.py")))
        self.assertFalse(list(output_root.rglob("batches")))

    def test_reads_text_and_image_representations_by_identity_and_range(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "display(value)")
        appended = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )
        text_output, image_output = appended["outputs"]
        text_representation = text_output["representations"][0]
        image_representation = image_output["representations"][0]

        text = self.storage.read(
            self.identity,
            journal_id=begun["journal_id"],
            output_id=text_output["output_id"],
            representation_id=text_representation["representation_id"],
            start=9,
            end_exclusive=13,
        )
        image = self.storage.read(
            self.identity,
            journal_id=begun["journal_id"],
            output_id=image_output["output_id"],
            representation_id=image_representation["representation_id"],
            start=0,
            end_exclusive=9,
        )

        self.assertEqual(text.body, b"text")
        self.assertEqual(text.size_bytes, 20)
        self.assertEqual(text.media_type, "text/plain")
        self.assertEqual(image.body, b"png-bytes")
        self.assertEqual(image.media_type, "image/png")

    def test_read_rejects_cross_output_representation_and_stale_fence(
        self,
    ) -> None:
        begun = self.storage.begin(self.identity, "display(value)")
        appended = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )
        first, second = appended["outputs"]

        with self.assertRaises(OutputJournalNotFoundError):
            self.storage.read(
                self.identity,
                journal_id=begun["journal_id"],
                output_id=first["output_id"],
                representation_id=second["representations"][0][
                    "representation_id"
                ],
                start=0,
                end_exclusive=1,
            )
        with self.assertRaises(OutputJournalNotFoundError):
            self.storage.read(
                _identity(self.workspace, fencing_token=8),
                journal_id=begun["journal_id"],
                output_id=first["output_id"],
                representation_id=first["representations"][0][
                    "representation_id"
                ],
                start=0,
                end_exclusive=1,
            )

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

    def test_stores_image_mime_types_with_native_extensions(self) -> None:
        begun = self.storage.begin(self.identity, "display(images)")
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
                            "media_type": "image/gif",
                            "encoding": "BASE64",
                            "content": base64.b64encode(b"GIF89a").decode(),
                        },
                        {
                            "media_type": "image/svg+xml",
                            "encoding": "UTF8",
                            "content": "<svg></svg>",
                        },
                    ],
                }
            ],
        )

        output_root = self.root / self.workspace / "outputs"
        self.assertEqual(
            next(output_root.rglob("*.gif")).read_bytes(), b"GIF89a"
        )
        self.assertEqual(
            next(output_root.rglob("*.svg")).read_text(encoding="utf-8"),
            "<svg></svg>",
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
        second_identity = _identity(
            self.workspace,
            operation_id="66666666-6666-4666-8666-666666666666",
            step_id="77777777-7777-4777-8777-777777777777",
            sequence=1,
        )
        prepared = self.storage.prepare_notebook(
            workspace_path=self.workspace,
            execution_id=self.identity.execution_id,
            runtime_profile="basic",
            cells=[
                {
                    "sequence": 0,
                    "operation_id": self.identity.operation_id,
                    "step_id": self.identity.step_id,
                    "source": "print('complete')",
                },
                {
                    "sequence": 1,
                    "operation_id": second_identity.operation_id,
                    "step_id": second_identity.step_id,
                    "source": "print('pending')",
                },
            ],
        )
        replayed = self.storage.prepare_notebook(
            workspace_path=self.workspace,
            execution_id=self.identity.execution_id,
            runtime_profile="basic",
            cells=[
                {
                    "sequence": 0,
                    "operation_id": self.identity.operation_id,
                    "step_id": self.identity.step_id,
                    "source": "print('complete')",
                }
            ],
        )
        self.assertEqual(prepared["prepared_cell_count"], 2)
        self.assertEqual(prepared["total_cell_count"], 2)
        self.assertEqual(replayed["total_cell_count"], 2)
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
        nbformat.validate(nbformat.from_dict(notebook))
        self.assertEqual(result["cell_count"], 1)
        self.assertEqual(result["output_count"], 2)
        self.assertEqual(notebook["nbformat"], 4)
        self.assertEqual(notebook["metadata"]["kernelspec"]["name"], "basic")
        self.assertEqual(len(notebook["cells"]), 2)
        self.assertEqual(notebook["cells"][0]["execution_count"], 7)
        self.assertEqual(
            notebook["cells"][0]["outputs"][0]["text"],
            "complete text output",
        )
        self.assertEqual(
            notebook["cells"][0]["outputs"][1]["data"]["image/png"],
            base64.b64encode(b"png-bytes").decode(),
        )
        self.assertEqual(notebook["cells"][1]["source"], "print('pending')")
        self.assertIsNone(notebook["cells"][1]["execution_count"])
        self.assertEqual(notebook["cells"][1]["outputs"], [])

        stale_identity = _identity(self.workspace, fencing_token=6)
        stale = self.storage.begin(stale_identity, "print('complete')")
        self.storage.append(
            stale_identity,
            journal_id=stale["journal_id"],
            expected_offset=0,
            batch_id=str(uuid4()),
            records=_records(),
        )
        self.storage.finalize(stale_identity, journal_id=stale["journal_id"])
        with self.assertRaises(OutputJournalConflictError):
            self.storage.materialize_notebook(
                workspace_path=self.workspace,
                runtime_profile="basic",
                cells=[
                    {
                        "sequence": 0,
                        "execution_count": 6,
                        "journal_id": stale["journal_id"],
                        "journal": stale_identity.as_dict(),
                    }
                ],
            )

        with self.assertRaises(OutputJournalConflictError):
            self.storage.prepare_notebook(
                workspace_path=self.workspace,
                execution_id=self.identity.execution_id,
                runtime_profile="basic",
                cells=[
                    {
                        "sequence": 0,
                        "operation_id": self.identity.operation_id,
                        "step_id": self.identity.step_id,
                        "source": "print('changed')",
                    }
                ],
            )

    def test_materialization_rejects_open_or_foreign_journal(self) -> None:
        self.storage.prepare_notebook(
            workspace_path=self.workspace,
            execution_id=self.identity.execution_id,
            runtime_profile="basic",
            cells=[
                {
                    "sequence": 0,
                    "operation_id": self.identity.operation_id,
                    "step_id": self.identity.step_id,
                    "source": "1 + 1",
                }
            ],
        )
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
        output_root = self.root / self.workspace / "outputs"
        self.assertEqual(len(list(output_root.rglob("journal.jsonl"))), 1)
        self.assertEqual(len(list(output_root.rglob("*.png"))), 1)
        with self.assertRaises(OutputJournalConflictError):
            self.storage.finalize(
                self.identity, journal_id=begun["journal_id"]
            )

    def test_repairs_incomplete_jsonl_tail_before_next_append(
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
        journal_path = next(
            (self.root / self.workspace / "outputs").rglob("journal.jsonl")
        )
        with journal_path.open("ab") as handle:
            handle.write(b'{"record_type":"BATCH"')

        appended = self.storage.append(
            self.identity,
            journal_id=begun["journal_id"],
            expected_offset=2,
            batch_id=str(uuid4()),
            records=_records()[:1],
        )

        self.assertEqual(appended["committed_offset"], 3)
        repaired = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [entry["record_type"] for entry in repaired],
            ["HEADER", "BATCH", "BATCH"],
        )

    def test_fencing_token_separates_storage_and_identity_mismatch_fails(
        self,
    ) -> None:
        first = self.storage.begin(self.identity, "print('first')")
        second = self.storage.begin(
            _identity(self.workspace, fencing_token=8), "print('second')"
        )

        self.assertNotEqual(first["journal_id"], second["journal_id"])
        journal_files = list(
            (self.root / self.workspace / "outputs").rglob("journal.jsonl")
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
