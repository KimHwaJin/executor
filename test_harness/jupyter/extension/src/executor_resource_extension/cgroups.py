"""Resolve the current process's cgroups without reading host-wide totals."""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CGROUP_V1 = "CGROUP_V1"
CGROUP_V2 = "CGROUP_V2"


@dataclass(frozen=True)
class Controller:
    source: str | None
    root: Path | None
    error: Exception | None = None

    def file(self, name: str) -> Path:
        if self.error is not None:
            raise self.error
        if self.root is None:
            raise ValueError("Current process cgroup could not be resolved")
        return self.root / name


def resolve_controllers(
    root: Path | None,
    membership: Path = Path("/proc/self/cgroup"),
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> dict[str, Controller]:
    """An explicit root is a v2 leaf or a v1 controller-mount directory."""
    names = ("cpu", "cpuacct", "memory")
    if root is not None:
        v1 = any((root / name).is_dir() for name in names)
        v1 = v1 or (root / "cpu,cpuacct").is_dir()
        if not v1:
            return {name: Controller(CGROUP_V2, root) for name in names}
        result = {}
        for name in names:
            directory = root / name
            if name != "memory" and not directory.is_dir():
                directory = root / "cpu,cpuacct"
            result[name] = Controller(CGROUP_V1, directory)
        return result

    try:
        memberships: dict[str, str] = {}
        for line in membership.read_text().splitlines():
            _, controllers, path = line.split(":", 2)
            for controller in controllers.split(","):
                memberships[controller] = path
        mounts = []
        for line in mountinfo.read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, filesystem = left.split(), right.split()
            if filesystem[0] in {"cgroup", "cgroup2"}:
                if len(fields) < 6 or len(filesystem) < 3:
                    raise ValueError("Invalid cgroup mountinfo")
                mounts.append((fields, filesystem))
    except (OSError, ValueError, IndexError) as exc:
        # No guessed mount-root fallback: it could expose node-wide usage.
        error = (
            ValueError("Invalid cgroup metadata")
            if isinstance(exc, IndexError)
            else exc
        )
        return {name: Controller(None, None, error) for name in names}

    result = {}
    for name in names:
        key = name if name in memberships else ""
        version = CGROUP_V1 if key else CGROUP_V2
        candidates = []
        for fields, filesystem in mounts:
            if key:
                if filesystem[0] != "cgroup":
                    continue
                if key not in filesystem[2].split(","):
                    continue
            elif filesystem[0] != "cgroup2":
                continue
            if key not in memberships:
                continue
            mount_root = _unescape(fields[3])
            path = _map_path(mount_root, memberships[key], fields[4])
            if path is not None:
                candidates.append((len(mount_root), path))
        if candidates:
            # Prefer the most specific bind mount when several are visible.
            candidates.sort(key=lambda item: item[0], reverse=True)
            result[name] = Controller(version, candidates[0][1])
        else:
            result[name] = Controller(None, None)
    return result


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def _map_path(root: str, membership: str, mount: str) -> Path | None:
    group = PurePosixPath(membership)
    mount_root = PurePosixPath(root)
    if not group.is_absolute() or ".." in group.parts:
        return None
    if not mount_root.is_absolute() or ".." in mount_root.parts:
        return None
    try:
        relative = group.relative_to(mount_root)
    except ValueError:
        # A private cgroup namespace reports its own root as '/'.
        if group != PurePosixPath("/"):
            return None
        relative = PurePosixPath(".")
    return Path(_unescape(mount)) / str(relative)
