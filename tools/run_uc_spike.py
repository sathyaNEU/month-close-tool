"""Run spike S1: the engine's Unity Catalog code paths against Unity Catalog OSS 0.6.0 (Gate 1b).

Starts the ``close-uc`` Compose project with a fresh storage root, runs
``engine/tests/uc/uc_spike.py`` in its own JVM, saves the per-operation results to
``.local/gate/uc-spike.json``, prints them, and removes the project again. ADR-0010 interprets them.
Exit code 1 if any operation failed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".local" / "uc-spike"
OUT = REPO / ".local" / "gate" / "uc-spike.json"
URI = "http://127.0.0.1:18080"
COMPOSE = ["docker", "compose", "-f", "compose.uc.yaml"]
DEFAULTS = """server.env=dev
server.authorization=disable
"""


def _up(env: dict[str, str]) -> None:
    subprocess.run([*COMPOSE, "up", "-d"], cwd=REPO, env=env, check=True, capture_output=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{URI}/api/2.1/unity-catalog/catalogs", timeout=3) as r:  # noqa: S310  # nosec B310
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit("Unity Catalog did not come up")


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    (ROOT / "storage").mkdir(parents=True)
    (ROOT / "server.properties").write_text(DEFAULTS + f"storage-root.tables=file://{ROOT / 'storage'}\n")
    env = {**os.environ, "UC_SPIKE_ROOT": str(ROOT)}
    try:
        _up(env)
        proc = subprocess.run(
            [sys.executable, str(REPO / "engine" / "tests" / "uc" / "uc_spike.py"), URI, str(ROOT)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=1800, check=False,
        )  # fmt: skip
    finally:
        subprocess.run([*COMPOSE, "down", "-v"], cwd=REPO, env=env, check=False, capture_output=True)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        raise SystemExit(f"spike failed to run:\n{proc.stderr[-3000:]}")
    results = json.loads(lines[-1])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, sort_keys=True))
    for name, r in sorted(results.items()):
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {name}: {str(r['detail'])[:200]}")
    passed = sum(1 for r in results.values() if r["ok"])
    print(f"uc-spike: {passed}/{len(results)} operations work against Unity Catalog OSS 0.6.0")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
