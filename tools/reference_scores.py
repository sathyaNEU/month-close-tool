"""Score the Stage 3 templates in the reference matcher against the generator's answer key.

    uv run python tools/reference_scores.py                      # small 11, 23, 47 and medium 11
    uv run python tools/reference_scores.py tiny:11 small:11      # any profile:seed list

For each profile and seed it generates the data in memory, normalizes every default data source, runs every
template match set and scores each family (``close_engine.reference.score``). It prints one JSON line with
the scores, the wall time per run and the process's peak memory, and exits 1 unless every run passes:

- 0 false automatic matches;
- clean recall of at least 99% (every Stage 3 template rule is keyed);
- every other expected outcome reached, breaks included.
"""

from __future__ import annotations

import gc
import json
import resource
import sys
import time
from typing import Any

from close_datagen.generate import generate
from close_datagen.profiles import PROFILES
from close_datagen.world import CALENDARS
from close_engine.reference.runner import normalize_all, run_all, score_all
from close_engine.reference.score import FamilyScore

RECALL_KEYED = 0.99
DEFAULT_RUNS = ("small:11", "small:23", "small:47", "medium:11")


def _family(score: FamilyScore) -> dict[str, Any]:
    others = sum(score.expected.values()) - score.clean_auto
    others_reached = sum(score.reached.values()) - score.clean_auto_reached
    ok = score.false_auto == 0 and score.recall >= RECALL_KEYED and others_reached == others
    return {
        "ok": ok,
        "false_auto": score.false_auto,
        "recall": round(score.recall, 6),
        "expected": dict(score.expected),
        "reached": dict(score.reached),
        "breaks": {name: {"reached": r, "total": t} for name, (r, t) in sorted(score.breaks.items())},
        "examples": score.examples[:5],
    }


def score_run(profile: str, seed: int) -> dict[str, Any]:
    started = time.monotonic()
    b = generate(seed, PROFILES[profile], f"reference-{profile}-{seed}")
    items = normalize_all(b.rows, CALENDARS)
    results = run_all(items, CALENDARS, f"reference-{profile}-{seed}")
    families = {s.family: _family(s) for s in score_all(results, b.group_rows(), b.member_rows())}
    return {
        "ok": all(f["ok"] for f in families.values()),
        "items": sum(len(v) for v in items.values()),
        "seconds": round(time.monotonic() - started, 1),
        "families": families,
    }


def _peak_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024, 1)  # spark-ban: allow SB008 reporting


def main(argv: list[str]) -> int:
    runs: dict[str, Any] = {}
    for spec in argv or DEFAULT_RUNS:
        profile, seed = spec.split(":")
        runs[spec] = score_run(profile, int(seed))
        gc.collect()  # release one run's data before the next
    verdict = "PASS" if all(r["ok"] for r in runs.values()) else "FAIL"
    sys.stdout.write(json.dumps({"verdict": verdict, "peak_rss_mib": _peak_mib(), "runs": runs}, sort_keys=True) + "\n")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
