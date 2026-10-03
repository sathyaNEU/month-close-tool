"""Measure the read path through the running stack (devproxy -> app -> Spark Connect), for Gate 1b.

1. Writes a 100k-row smoke run as dave, who sees both entities (the widest scope).
2. Times three requests, 5 warm-ups then 100 calls each: the first page of rows (500), a filtered page
   (currency, amount range and row-id prefix), and the grouped totals. The gate needs every p95 at or
   below 1.5 s.
3. Records baselines that are not gated: the same three at 1M rows (2 warm-ups, 20 calls), and four
   concurrent clients on the 100k run.

Prints one JSON document; exits 1 if a 100k p95 misses the budget.

    uv run python tools/measure_read_path.py [--base http://127.0.0.1:8088] [--skip-1m]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import uuid

import httpx

BUDGET_S = 1.5
TERMINAL = {"succeeded", "failed", "cancelled", "submit_failed"}


def client(base: str, user: str = "dave") -> httpx.Client:
    http = httpx.Client(base_url=base, timeout=120)
    http.get(f"/__dev/login/{user}").raise_for_status()
    return http


def smoke_run(http: httpx.Client, rows: int, timeout: float) -> str:
    r = http.post("/api/v1/smoke/runs", json={"rows": rows}, headers={"Idempotency-Key": f"bench-{uuid.uuid4()}"})
    if r.status_code != 202:
        raise SystemExit(f"could not start a {rows}-row run: {r.status_code} {r.text}")
    run_id: str = r.json()["run_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = http.get(f"/api/v1/smoke/runs/{run_id}").json()["state"]
        if state in TERMINAL:
            if state != "succeeded":
                raise SystemExit(f"the {rows}-row run ended {state}")
            return run_id
        time.sleep(1)
    raise SystemExit(f"the {rows}-row run did not finish in {timeout}s")


def requests_for(run_id: str) -> dict[str, tuple[str, dict[str, str | int]]]:
    base = f"/api/v1/smoke/runs/{run_id}"
    return {
        "page": (f"{base}/rows", {"page_size": 500}),
        "filtered": (
            f"{base}/rows",
            {"page_size": 500, "currency": "USD", "min_amount": -250000, "max_amount": 250000, "row_prefix": "a"},
        ),
        "totals": (f"{base}/totals", {}),
    }


def timed(http: httpx.Client, path: str, params: dict[str, str | int]) -> float:
    started = time.perf_counter()
    r = http.get(path, params=params)
    elapsed = time.perf_counter() - started
    if r.status_code != 200:
        raise SystemExit(f"{path} {params} -> {r.status_code} {r.text[:300]}")
    return elapsed


def summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    p95 = ordered[max(0, -(-len(ordered) * 95 // 100) - 1)]  # nearest-rank percentile
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered) * 1000),
        "p95_ms": round(p95 * 1000),
        "max_ms": round(ordered[-1] * 1000),
    }


def measure(http: httpx.Client, run_id: str, warmups: int, calls: int) -> dict[str, dict[str, float | int]]:
    out = {}
    for name, (path, params) in requests_for(run_id).items():
        for _ in range(warmups):
            timed(http, path, params)
        out[name] = summary([timed(http, path, params) for _ in range(calls)])
    return out


def concurrent(base: str, run_id: str, clients: int = 4, each: int = 25) -> dict[str, float | int]:
    samples: list[float] = []
    lock = threading.Lock()
    reqs = list(requests_for(run_id).values())

    def worker(offset: int) -> None:
        http = client(base)
        mine = [timed(http, *reqs[(offset + i) % len(reqs)]) for i in range(each)]
        with lock:
            samples.extend(mine)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"clients": clients, **summary(samples)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8088")
    parser.add_argument("--skip-1m", action="store_true", help="skip the 1M-row baseline")
    args = parser.parse_args()
    http = client(args.base)
    run_100k = smoke_run(http, 100_000, timeout=600)
    result: dict[str, object] = {"budget_s": BUDGET_S, "100k": measure(http, run_100k, warmups=5, calls=100)}
    result["concurrent_100k"] = concurrent(args.base, run_100k)
    if not args.skip_1m:
        run_1m = smoke_run(http, 1_000_000, timeout=1200)
        result["1m_baseline"] = measure(http, run_1m, warmups=2, calls=20)
    gated: dict[str, dict[str, float | int]] = result["100k"]  # type: ignore[assignment]
    ok = all(float(s["p95_ms"]) <= BUDGET_S * 1000 for s in gated.values())
    result["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
