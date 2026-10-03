"""Gate 2b through the stack: migrations and the generator run as jobs through the Jobs API, timed.

1. ``ddl_apply`` on a fresh lake prefix: 60 s or less, every migration applied.
2. ``ddl_apply`` again on the same prefix: nothing applied.
3. ``datagen`` small on a fresh prefix: 3 minutes or less, control totals match.
4. ``datagen`` medium on a fresh prefix: 25 minutes or less, control totals match.

Times are wall-clock from submission to termination, spark-submit start-up included. Needs the stack up.

    uv run python tools/measure_datagen.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
JOB_IDS = {"ddl_apply": 1002, "datagen": 1003}  # pinned in compose.yaml (EMULATOR_JOB_IDS)


def job(name: str, **params: str) -> dict[str, Any]:
    args = [f"{k}={v}" for k, v in params.items()]
    cmd = ["docker", "compose", "exec", "-T", "app", "python", "-", name, str(JOB_IDS[name]), *args]
    with (REPO / "tools" / "stack_job.py").open("rb") as script:
        proc = subprocess.run(cmd, cwd=REPO, stdin=script, capture_output=True, check=False)
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.startswith("{")]
    if not lines:
        raise SystemExit(f"{name} produced no result:\n{proc.stderr.decode()[-3000:]}")
    out: dict[str, Any] = json.loads(lines[-1])
    return out


def main() -> int:
    stamp = int(time.time())
    ddl_root = f"s3a://close-lake/gate-ddl-{stamp}"
    first = job("ddl_apply", root_uri=ddl_root)
    rerun = job("ddl_apply", root_uri=ddl_root)
    small = job("datagen", profile="small", seed="11", root_uri=f"s3a://close-lake/gate-small-{stamp}")
    medium = job("datagen", profile="medium", seed="11", root_uri=f"s3a://close-lake/gate-medium-{stamp}")

    def ok(run: dict[str, Any]) -> bool:
        return run["result_state"] == "SUCCESS" and bool((run["result"] or {}).get("ok"))

    checks = {
        "ddl_apply_fresh": ok(first) and first["seconds"] <= 60 and bool(first["result"]["applied"]),
        "ddl_apply_rerun_applies_nothing": ok(rerun) and rerun["result"]["applied"] == [],
        "small_within_3_min": ok(small) and small["seconds"] <= 180,
        "medium_within_25_min": ok(medium) and medium["seconds"] <= 1500,
    }
    summary = {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "ddl_apply": {
            "seconds": first["seconds"],
            "applied": first["result"]["applied"],
            "rerun_applied": rerun["result"]["applied"],
        },
        "small": {
            "seconds": small["seconds"],
            "landing_rows": small["result"]["landing_rows"],
            "breaks": small["result"]["breaks"],
        },
        "medium": {"seconds": medium["seconds"], "landing_rows": medium["result"]["landing_rows"]},
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
