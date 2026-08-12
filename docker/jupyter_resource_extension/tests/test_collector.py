from __future__ import annotations

import os
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

from executor_resource_extension.collector import CGROUP_V2, PSUTIL, ResourceCollector

UserIds = namedtuple("UserIds", "real effective saved")
MemoryInfo = namedtuple("MemoryInfo", "rss")
CpuTimes = namedtuple("CpuTimes", "user system")


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int,
        uid: int,
        rss: int,
        user: float,
        system: float,
        create_time: float,
    ) -> None:
        self.info = {
            "pid": pid,
            "uids": UserIds(uid, uid, uid),
            "memory_info": MemoryInfo(rss),
            "cpu_times": CpuTimes(user, system),
            "create_time": create_time,
        }


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
            process = FakeProcess(
                pid=1,
                uid=os.getuid(),
                rss=128,
                user=1.0,
                system=0.0,
                create_time=1.0,
            )
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=None,
                configured_memory_bytes=None,
                process_iter=lambda _attributes: [process],
                monotonic=Clock(10.0, 12.0),
            )

            first = collector.collect()
            (root / "cpu.stat").write_text("usage_usec 2000000\n", encoding="utf-8")
            second = collector.collect()

        self.assertIsNone(first["cpu"]["used_cores"])
        self.assertEqual(second["cpu"]["used_cores"], 0.5)
        self.assertEqual(second["cpu"]["capacity_cores"], 2.0)
        self.assertEqual(second["cpu"]["source"], CGROUP_V2)
        self.assertEqual(second["memory"]["used_bytes"], 256)
        self.assertEqual(second["memory"]["capacity_bytes"], 1024)
        self.assertEqual(second["memory"]["utilization"], 0.25)
        self.assertEqual(second["memory"]["source"], CGROUP_V2)

    def test_uses_cgroup_cpu_with_psutil_memory_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
            (root / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
            process = FakeProcess(
                pid=7,
                uid=os.getuid(),
                rss=512,
                user=1.0,
                system=0.0,
                create_time=1.0,
            )
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=4.0,
                configured_memory_bytes=2048,
                process_iter=lambda _attributes: [process],
                monotonic=Clock(10.0),
            )

            result = collector.collect()

        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["cpu"]["source"], CGROUP_V2)
        self.assertFalse(result["cpu"]["estimated"])
        self.assertEqual(result["memory"]["source"], PSUTIL)
        self.assertTrue(result["memory"]["estimated"])
        self.assertEqual(result["memory"]["used_bytes"], 512)
        self.assertEqual(result["memory"]["capacity_bytes"], 2048)

    def test_falls_back_to_psutil_and_configured_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = FakeProcess(
                pid=7,
                uid=os.getuid(),
                rss=512,
                user=1.0,
                system=0.0,
                create_time=1.0,
            )
            processes = [process]
            collector = ResourceCollector(
                cgroup_root=root,
                configured_cpu_cores=4.0,
                configured_memory_bytes=2048,
                process_iter=lambda _attributes: processes,
                monotonic=Clock(10.0, 12.0),
            )

            first = collector.collect()
            processes[0] = FakeProcess(
                pid=7,
                uid=os.getuid(),
                rss=768,
                user=3.0,
                system=0.0,
                create_time=1.0,
            )
            second = collector.collect()

        self.assertIsNone(first["cpu"]["used_cores"])
        self.assertEqual(second["cpu"]["used_cores"], 1.0)
        self.assertEqual(second["cpu"]["capacity_cores"], 4.0)
        self.assertEqual(second["cpu"]["source"], PSUTIL)
        self.assertTrue(second["cpu"]["estimated"])
        self.assertEqual(second["memory"]["used_bytes"], 768)
        self.assertEqual(second["memory"]["capacity_bytes"], 2048)
        self.assertEqual(second["memory"]["source"], PSUTIL)
        self.assertTrue(second["memory"]["estimated"])


if __name__ == "__main__":
    unittest.main()
