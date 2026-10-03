"""Gate 2b: seeds {11, 23, 47} give identical landing digests across 3 runs, and with local[1] vs local[4].

Runs ``close_datagen.determinism`` once with ``local[4]`` (three runs per seed) and once with ``local[1]`` (one run
per seed), then prints a JSON verdict. Different seeds must give different digests.

    make datagen-determinism
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEEDS = "11,23,47"


def _digests(master: str, runs: int) -> dict[str, list[dict[str, str]]]:
    env = {**os.environ, "CLOSE_SPARK_MASTER": master}
    cmd = ["uv", "run", "--frozen", "python", "-m", "close_datagen.determinism", "--seeds", SEEDS, "--runs", str(runs)]
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, check=False)
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("{")), "")
    if proc.returncode != 0 or not line:
        raise SystemExit(f"determinism run with {master} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    data: dict[str, list[dict[str, str]]] = json.loads(line)
    return data


def main() -> int:
    four, one = _digests("local[4]", 3), _digests("local[1]", 1)
    per_seed = {}
    for seed, runs in four.items():
        per_seed[seed] = {
            "runs": len(runs),
            "tables": len(runs[0]),
            "identical_across_runs": all(r == runs[0] for r in runs),
            "local1_equals_local4": one[seed][0] == runs[0],
        }
    firsts = [runs[0] for runs in four.values()]
    seeds_differ = all(a != b for i, a in enumerate(firsts) for b in firsts[i + 1 :])
    ok = seeds_differ and all(v["identical_across_runs"] and v["local1_equals_local4"] for v in per_seed.values())
    print(
        json.dumps(
            {"verdict": "PASS" if ok else "FAIL", "seeds_differ": seeds_differ, "seeds": per_seed}, sort_keys=True
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
