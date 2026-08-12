from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from executor_resource_extension.collector import CGROUP_V2, ResourceCollector


class Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class ResourceCollectorTests(unittest.TestCase):
    def test_collects_cgroup_v2_usage_and_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
            (root / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
            (root / "memory.current").write_text("256\n", encoding="utf-8")
            (root / "memory.max").write_text("1024\n", encoding="utf-8")
            (root / "cgroup.procs").write_text("1\n7\n", encoding="utf-8")
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=None,
                configured_memory_bytes=None,
                monotonic=Clock(10.0, 12.0),
            )

            first = collector.collect()
            (root / "cpu.stat").write_text("usage_usec 2000000\n", encoding="utf-8")
            second = collector.collect()

        self.assertEqual(second["schema_version"], "1.0")
        self.assertEqual(second["process_count"], 2)
        self.assertIsNone(first["cpu"]["used_cores"])
        self.assertEqual(second["cpu"]["used_cores"], 0.5)
        self.assertEqual(second["cpu"]["capacity_cores"], 2.0)
        self.assertEqual(second["cpu"]["source"], CGROUP_V2)
        self.assertFalse(second["cpu"]["estimated"])
        self.assertEqual(second["memory"]["used_bytes"], 256)
        self.assertEqual(second["memory"]["capacity_bytes"], 1024)
        self.assertEqual(second["memory"]["utilization"], 0.25)
        self.assertEqual(second["memory"]["source"], CGROUP_V2)
        self.assertFalse(second["memory"]["estimated"])

    def test_returns_partial_result_when_memory_cgroup_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
            (root / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=4.0,
                configured_memory_bytes=2048,
                monotonic=Clock(10.0),
            )

            result = collector.collect()

        self.assertEqual(result["cpu"]["source"], CGROUP_V2)
        self.assertEqual(result["cpu"]["capacity_cores"], 2.0)
        self.assertEqual(result["cpu"]["errors"], [])
        self.assertEqual(result["memory"]["source"], CGROUP_V2)
        self.assertIsNone(result["process_count"])
        self.assertIsNone(result["memory"]["used_bytes"])
        self.assertEqual(result["memory"]["capacity_bytes"], 2048)
        self.assertIsNone(result["memory"]["utilization"])
        self.assertEqual(
            result["memory"]["errors"],
            [
                "cgroup_memory:FileNotFoundError",
                "cgroup_memory_capacity:FileNotFoundError",
            ],
        )

    def test_uses_configured_capacity_when_cgroup_limits_are_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
            (root / "cpu.max").write_text("max 100000\n", encoding="utf-8")
            (root / "memory.current").write_text("512\n", encoding="utf-8")
            (root / "memory.max").write_text("max\n", encoding="utf-8")
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=4.0,
                configured_memory_bytes=2048,
                monotonic=Clock(10.0),
            )

            result = collector.collect()

        self.assertEqual(result["cpu"]["capacity_cores"], 4.0)
        self.assertEqual(result["memory"]["capacity_bytes"], 2048)
        self.assertEqual(result["memory"]["used_bytes"], 512)
        self.assertEqual(result["memory"]["utilization"], 0.25)


if __name__ == "__main__":
    unittest.main()
