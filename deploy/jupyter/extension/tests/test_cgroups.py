"""Controller discovery and v1 collection against synthetic proc/cgroup files."""

from pathlib import Path

import pytest
from executor_resource_extension.cgroups import resolve_controllers
from executor_resource_extension.collector import ResourceCollector


def metadata(tmp_path, groups, mounts):
    membership, mountinfo = tmp_path / "cgroup", tmp_path / "mountinfo"
    membership.write_text(groups)
    mountinfo.write_text(mounts)
    return membership, mountinfo


def mount(path, controllers, root="/"):
    filesystem = "cgroup" if controllers else "cgroup2"
    escaped = str(path).replace(" ", r"\040")
    return (
        f"1 0 0:1 {root} {escaped} rw - {filesystem} cgroup "
        f"rw{',' + controllers if controllers else ''}\n"
    )


@pytest.mark.parametrize("combined", [True, False])
@pytest.mark.parametrize("private", [True, False])
def test_v1_controller_mounts_and_cpu_units(tmp_path, combined, private):
    root = tmp_path / "controller mounts"
    group = "/" if private else "/kubepods/pod/container"
    mounted_root = "/kubepods/pod/container" if private else "/"
    suffix = "" if private else group.lstrip("/")
    groups, mounts = "", ""
    controllers = (
        ["cpu,cpuacct", "memory"] if combined else ["cpu", "cpuacct", "memory"]
    )
    paths = {}
    for index, names in enumerate(controllers, 1):
        directory = root / names / suffix
        directory.mkdir(parents=True)
        for name in names.split(","):
            paths[name] = directory
        groups += f"{index}:{names}:{group}\n"
        mounts += mount(root / names, names, mounted_root)
    files = {
        "cpu": {"cpu.cfs_quota_us": "5000", "cpu.cfs_period_us": "10000"},
        "cpuacct": {"cpuacct.usage": "1000000000", "cgroup.procs": "1\n7"},
        "memory": {
            "memory.usage_in_bytes": "256",
            "memory.limit_in_bytes": "1024",
        },
    }
    for name, values in files.items():
        for filename, value in values.items():
            (paths[name] / filename).write_text(value)
    membership, mountinfo = metadata(tmp_path, groups, mounts)
    clock = iter([10.0, 12.0])
    collector = ResourceCollector(
        cgroup_root=None,
        configured_cpu_cores=None,
        configured_memory_bytes=None,
        monotonic=lambda: next(clock),
        membership=membership,
        mountinfo=mountinfo,
    )
    assert collector.collect()["cpu"]["used_cores"] is None
    (paths["cpuacct"] / "cpuacct.usage").write_text("1500000000")
    result = collector.collect()
    assert result["cpu"] == {
        "used_cores": 0.25,
        "capacity_cores": 0.5,
        "utilization": 0.5,
        "source": "CGROUP_V1",
        "estimated": False,
        "errors": [],
    }
    assert result["memory"] == {
        "used_bytes": 256,
        "capacity_bytes": 1024,
        "utilization": 0.25,
        "source": "CGROUP_V1",
        "estimated": True,
        "errors": [],
    }
    assert result["process_count"] == 2


@pytest.mark.parametrize(
    "mount_root,group,relative",
    [
        ("/", "/user.slice/session", "user.slice/session"),
        ("/pod", "/pod/container", "container"),
        ("/pod/container", "/", ""),
        ("/", "/", ""),
    ],
)
def test_v2_resolves_current_leaf(tmp_path, mount_root, group, relative):
    root = tmp_path / "unified"
    membership, mountinfo = metadata(
        tmp_path, f"0::{group}\n", mount(root, "", mount_root)
    )
    result = resolve_controllers(None, membership, mountinfo)
    assert all(c.root == root / relative for c in result.values())
    assert all(c.source == "CGROUP_V2" for c in result.values())


def test_hybrid_uses_controller_specific_membership(tmp_path):
    membership, mountinfo = metadata(
        tmp_path,
        "1:cpu,cpuacct:/cpu-leaf\n0::/v2-leaf\n",
        mount(tmp_path / "legacy", "cpu,cpuacct")
        + mount(tmp_path / "unified", ""),
    )
    result = resolve_controllers(None, membership, mountinfo)
    assert result["cpu"].source == "CGROUP_V1"
    assert result["cpuacct"].root == tmp_path / "legacy/cpu-leaf"
    assert result["memory"].source == "CGROUP_V2"
    assert result["memory"].root == tmp_path / "unified/v2-leaf"


@pytest.mark.parametrize(
    "group", ["/other/container", "/../../host", "relative"]
)
def test_unmapped_membership_never_falls_back_to_mount_root(tmp_path, group):
    membership, mountinfo = metadata(
        tmp_path, f"0::{group}\n", mount(tmp_path / "unified", "", "/pod")
    )
    assert all(
        c.root is None
        for c in resolve_controllers(None, membership, mountinfo).values()
    )


@pytest.mark.parametrize("content", ["bad", "0::/\n"])
def test_bad_proc_metadata_returns_unknown_not_zero(tmp_path, content):
    membership, mountinfo = metadata(tmp_path, content, "bad - cgroup2\n")
    result = ResourceCollector(
        cgroup_root=None,
        configured_cpu_cores=None,
        configured_memory_bytes=None,
        membership=membership,
        mountinfo=mountinfo,
    ).collect()
    assert result["cpu"]["used_cores"] is None
    assert result["memory"]["used_bytes"] is None
    assert result["cpu"]["errors"]
    assert result["memory"]["errors"]


def test_v1_unlimited_and_missing_usage_preserve_explicit_capacity(tmp_path):
    for name in ("cpu", "cpuacct", "memory"):
        (tmp_path / name).mkdir()
    (tmp_path / "cpu/cpu.cfs_quota_us").write_text("-1")
    (tmp_path / "cpu/cpu.cfs_period_us").write_text("100000")
    (tmp_path / "memory/memory.limit_in_bytes").write_text(
        "9223372036854771712"
    )
    result = ResourceCollector(
        cgroup_root=tmp_path,
        configured_cpu_cores=4,
        configured_memory_bytes=1024,
    ).collect()
    assert result["cpu"]["capacity_cores"] == 4
    assert result["memory"]["capacity_bytes"] == 1024
    assert result["cpu"]["used_cores"] is None
    assert result["memory"]["used_bytes"] is None
    assert result["cpu"]["errors"] == ["cgroup_cpu:FileNotFoundError"]


def test_permission_denied_is_reported_not_masked(tmp_path, monkeypatch):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "cpu.stat":
            raise PermissionError("restricted")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = ResourceCollector(
        cgroup_root=tmp_path,
        configured_cpu_cores=None,
        configured_memory_bytes=None,
    ).collect()
    assert "cgroup_cpu:PermissionError" in result["cpu"]["errors"]
    assert result["cpu"]["used_cores"] is None


def test_counter_reset_does_not_report_negative_cpu(tmp_path):
    (tmp_path / "cpu,cpuacct").mkdir()
    (tmp_path / "memory").mkdir()
    usage = tmp_path / "cpu,cpuacct/cpuacct.usage"
    usage.write_text("2000000000")
    clock = iter([1.0, 2.0, 3.0])
    collector = ResourceCollector(
        cgroup_root=tmp_path,
        configured_cpu_cores=None,
        configured_memory_bytes=None,
        monotonic=lambda: next(clock),
    )
    collector.collect()
    usage.write_text("0")
    assert collector.collect()["cpu"]["used_cores"] is None
    usage.write_text("1000000000")
    assert collector.collect()["cpu"]["used_cores"] == 1.0


def test_environment_defaults_to_proc_discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("EXECUTOR_RESOURCE_CGROUP_ROOT", raising=False)
    called = []
    from executor_resource_extension import collector

    original = collector.resolve_controllers

    def resolve(root, *_):
        called.append(root)
        return original(tmp_path)

    monkeypatch.setattr(collector, "resolve_controllers", resolve)
    ResourceCollector.from_environment()
    assert called == [None]
