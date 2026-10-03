"""One smoke run through the running local stack (devproxy on 127.0.0.1:8088), as a dev user.

Logs in, starts a run with an Idempotency-Key, waits until it is terminal, and prints the run as the
user sees it (results filtered to that user's entities). Exit code 0 only if the run succeeded.

    uv run python tools/stack_smoke.py [--user alice] [--rows 1000] [--timeout 180]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "submit_failed"}


def run_once(base: str, user: str, rows: int, timeout: float) -> dict[str, object]:
    with httpx.Client(base_url=base, timeout=30) as client:
        client.get(f"/__dev/login/{user}").raise_for_status()
        started = time.monotonic()
        r = client.post("/api/v1/smoke/runs", json={"rows": rows}, headers={"Idempotency-Key": f"smoke-{uuid.uuid4()}"})
        if r.status_code != 202:
            raise SystemExit(f"start failed: {r.status_code} {r.text}")
        run_id = r.json()["run_id"]
        while True:
            run: dict[str, object] = client.get(f"/api/v1/smoke/runs/{run_id}").json()
            if run["state"] in TERMINAL:
                run["elapsed_s"] = round(time.monotonic() - started, 1)
                return run
            if time.monotonic() - started > timeout:
                raise SystemExit(f"run {run_id} not terminal after {timeout}s (state {run['state']})")
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8088")
    parser.add_argument("--user", default="alice")
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    run = run_once(args.base, args.user, args.rows, args.timeout)
    print(json.dumps(run, indent=2, sort_keys=True))
    return 0 if run["state"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
