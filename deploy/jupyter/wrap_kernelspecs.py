"""Start every kernelspec through the Executor kernel launcher (build time)."""

import json
import sys
from pathlib import Path

for spec_file in Path(sys.argv[1]).glob("*/kernel.json"):
    spec = json.loads(spec_file.read_text())
    spec["argv"] = [
        sys.executable,
        "-m",
        "executor_resource_extension.kernel_launcher",
        *spec["argv"],
    ]
    spec_file.write_text(json.dumps(spec, indent=1))
