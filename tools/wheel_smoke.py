"""Prove the wheels we ship work outside the dev environment (what Databricks will install).

Steps: build both wheels, check their contents (console script, py.typed, packaged test vectors),
install them into a fresh Python 3.12 venv together with the pinned runtime libraries only, run
``close-job smoke`` from that venv, and compare its digest with the same job run from the editable
dev environment. Exits non-zero on any difference.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "CLOSE_JOB_RESULT "
JOB_ARGS = ["smoke", "--run-id", "wheel-smoke", "--rows", "500"]


def _run(argv: list[str], env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, env=env, check=False, timeout=900)
    if proc.returncode != 0:
        raise SystemExit(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        )
    return proc.stdout


def _result(stdout: str) -> dict[str, object]:
    lines = [ln for ln in stdout.splitlines() if ln.startswith(RESULT_PREFIX)]
    if len(lines) != 1:
        raise SystemExit(f"expected one result line, got {len(lines)}:\n{stdout[-2000:]}")
    parsed: dict[str, object] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return parsed


def _check_wheel(path: Path, required: list[str]) -> None:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    missing = [r for r in required if not any(n.endswith(r) for n in names)]
    if missing:
        raise SystemExit(f"{path.name} is missing {missing}")
    print(f"wheel ok: {path.name} ({len(names)} files, contains {', '.join(required)})")


def _job_env(python: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYSPARK_GATEWAY")}
    env.update({"PYSPARK_PYTHON": str(python), "PYSPARK_DRIVER_PYTHON": str(python), "CLOSE_SPARK_MASTER": "local[2]"})
    return env


def main() -> int:
    pins = tomllib.loads((REPO / "tools" / "versions.lock").read_text(encoding="utf-8"))["python_packages"]
    with tempfile.TemporaryDirectory(prefix="close-wheel-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        dist = tmp / "dist"
        for package in ("close-contracts", "close-engine"):
            _run(["uv", "build", "--package", package, "--wheel", "--out-dir", str(dist)])
        contracts = next(dist.glob("close_contracts-*.whl"))
        engine = next(dist.glob("close_engine-*.whl"))
        _check_wheel(
            contracts,
            [
                "close_contracts/py.typed",
                "close_contracts/test_vectors/ids.json",
                "close_contracts/test_vectors/money.json",
            ],
        )
        _check_wheel(engine, ["close_engine/py.typed", "entry_points.txt"])
        with zipfile.ZipFile(engine) as zf:
            entry_points = zf.read(next(n for n in zf.namelist() if n.endswith("entry_points.txt"))).decode()
        if "close-job = close_engine.jobs.__main__:main" not in entry_points:
            raise SystemExit(f"close-job console script missing from {engine.name}:\n{entry_points}")

        venv = tmp / "venv"
        _run(["uv", "venv", str(venv), "--python", "3.12", "--quiet"])
        python = venv / "bin" / "python"
        runtime = [f"{name}=={version}" for name, version in pins.items() if name != "pydantic"]
        _run(["uv", "pip", "install", "--quiet", "--python", str(python), str(contracts), str(engine), *runtime])
        vectors = _run(
            [
                str(python),
                "-c",
                "from importlib import resources; import json; "
                "print(len(json.loads(resources.files('close_contracts').joinpath('test_vectors/ids.json').read_text())))",
            ]
        )
        print(f"packaged vectors readable from the installed wheel: {vectors.strip()} cases")

        from_wheel = _result(
            _run(
                [str(venv / "bin" / "close-job"), *JOB_ARGS, "--root-uri", (tmp / "lake-wheel").as_uri()],
                _job_env(python),
            )
        )
        dev_python = REPO / ".venv" / "bin" / "python"
        from_dev = _result(
            _run(
                [str(dev_python), "-m", "close_engine.jobs", *JOB_ARGS, "--root-uri", (tmp / "lake-dev").as_uri()],
                _job_env(dev_python),
            )
        )
        print(f"installed wheel : rows={from_wheel['rows']} digest={from_wheel['digest']}")
        print(f"editable dev env: rows={from_dev['rows']} digest={from_dev['digest']}")
        if from_wheel["digest"] != from_dev["digest"] or from_wheel["rows"] != 500:
            print("wheel smoke: FAILED (digest or row count differs)")
            return 1
    print("wheel smoke: OK (installed wheels reproduce the dev environment exactly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
