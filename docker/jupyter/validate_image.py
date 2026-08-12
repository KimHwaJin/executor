from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

JUPYTER = Path("/opt/venvs/jupyter/bin/jupyter")
PROFILES: dict[str, dict[str, Any]] = {
    "basic": {
        "python": Path("/opt/venvs/basic/bin/python"),
        "version": [3, 11],
        "imports": [
            "duckdb",
            "matplotlib",
            "numpy",
            "openpyxl",
            "pandas",
            "plotly",
            "polars",
            "pyarrow",
            "scipy",
            "seaborn",
            "statsmodels",
        ],
    },
    "ml": {
        "python": Path("/opt/venvs/ml/bin/python"),
        "version": [3, 12],
        "imports": [
            "imblearn",
            "joblib",
            "lightgbm",
            "optuna",
            "shap",
            "sklearn",
            "xgboost",
        ],
    },
}


def _run(*command: str) -> str:
    return subprocess.check_output(command, text=True).strip()


def main() -> None:
    kernelspecs = json.loads(_run(str(JUPYTER), "kernelspec", "list", "--json"))["kernelspecs"]
    if not set(PROFILES).issubset(kernelspecs):
        raise RuntimeError(f"Required kernelspecs are missing: {sorted(kernelspecs)}")

    results: dict[str, dict[str, object]] = {}
    for name, profile in PROFILES.items():
        python: Path = profile["python"]
        expected_version: list[int] = profile["version"]
        imports: list[str] = profile["imports"]
        probe = (
            "import importlib, json, sys; "
            f"modules={imports!r}; "
            "[importlib.import_module(module) for module in modules]; "
            "print(json.dumps({'version': list(sys.version_info[:2]), 'imports': modules}))"
        )
        result = json.loads(_run(str(python), "-c", probe))
        if result["version"] != expected_version:
            raise RuntimeError(
                f"{name} uses Python {result['version']}, expected {expected_version}."
            )

        argv = kernelspecs[name]["spec"]["argv"]
        if Path(argv[0]) != python:
            raise RuntimeError(f"{name} kernelspec points to {argv[0]}, expected {python}.")
        results[name] = result

    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
