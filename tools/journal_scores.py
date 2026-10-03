"""Journal the generated marketplace with the SAMPLE configuration and hold the books to the oracle (Stage 6).

    uv run python tools/journal_scores.py                  # tiny 11 and small 11
    uv run python tools/journal_scores.py small:23         # any profile:seed list

For each profile and seed it generates the data in memory, derives the accounting events, books them with the
sample configuration and compares the books with the independent oracle. On the first run it also books every
policy combination (engine against oracle), and on the last it flips each policy switch in turn: the oracle's
books must change, and the engine must book what the oracle books under the flipped policy. It prints one JSON
line with the counts, the timings, the invariants at the end of the data and the process's peak memory, and exits
1 unless:

- every source row is accounted for and no event is rejected;
- no event type fails;
- the engine's books equal the oracle's, in transaction and functional amounts, for every policy tried;
- every flip changes the books, and the engine follows it;
- every invariant holds.
"""

from __future__ import annotations

import gc
import json
import resource
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from close_contracts.samples.journaling import SAMPLE_POLICY
from close_datagen.generate import generate
from close_datagen.profiles import PROFILES
from close_datagen.world import CALENDARS
from close_engine.reference.journal.events import Derived, derive
from close_engine.reference.journal.invariants import check_all
from close_engine.reference.journal.ledger import run
from close_engine.reference.journal.matrix import flips, presentation_matrix, program_matrix
from close_engine.reference.journal.oracle import engine_nets, oracle_nets
from close_engine.reference.journal.posting import JournalConfig, ReferenceData
from close_engine.reference.journal.runner import reference_from, sample_config

DEFAULT_RUNS = ("tiny:11", "small:11")


def _differing(a: Mapping[Any, int], b: Mapping[Any, int]) -> int:
    return sum(1 for k in a.keys() | b.keys() if a.get(k, 0) != b.get(k, 0))


def _matrix(config: JournalConfig, derived: Derived, ref: ReferenceData) -> dict[str, Any]:
    started = time.monotonic()
    policies = program_matrix(SAMPLE_POLICY) + presentation_matrix(SAMPLE_POLICY)
    mismatched = []
    for label, policy in policies:
        result = run(replace(config, policy=policy), derived.events, ref=ref)
        if result.failed_types or engine_nets(result.lines) != oracle_nets(derived.events, policy, ref):
            mismatched.append(label)
    return {"policies": len(policies), "mismatched": mismatched, "seconds": round(time.monotonic() - started, 1)}


def _flips(config: JournalConfig, derived: Derived, ref: ReferenceData) -> dict[str, Any]:
    started = time.monotonic()
    base = oracle_nets(derived.events, SAMPLE_POLICY, ref)
    tried = flips(SAMPLE_POLICY)
    unchanged, mismatched = [], []
    for label, policy in tried:
        expected = oracle_nets(derived.events, policy, ref)
        if expected == base:
            unchanged.append(label)
        result = run(replace(config, policy=policy), derived.events, ref=ref)
        if result.failed_types or engine_nets(result.lines) != expected:
            mismatched.append(label)
    seconds = round(time.monotonic() - started, 1)
    return {"flips": len(tried), "unchanged": unchanged, "mismatched": mismatched, "seconds": seconds}


def score_run(profile: str, seed: int, *, matrix: bool, flip: bool) -> dict[str, Any]:
    b = generate(seed, PROFILES[profile], f"journal-{profile}-{seed}")
    config, ref = sample_config(), reference_from(b.rows)
    started = time.monotonic()
    derived = derive(list(config.event_types.values()), b.rows, CALENDARS)
    derived_s = time.monotonic() - started
    started = time.monotonic()
    result = run(config, derived.events, ref=ref)
    booked_s = time.monotonic() - started
    engine, oracle = engine_nets(result.lines), oracle_nets(derived.events, SAMPLE_POLICY, ref)
    invariants = check_all(b.rows, result.lines, SAMPLE_POLICY)
    control = derived.control.values()
    out: dict[str, Any] = {
        "rows_read": sum(c["read"] for c in control),
        "accounted": all(c["read"] == c["events"] + c["excluded"] + c["rejected"] for c in control),
        "events": len(derived.events),
        "excluded": sum(c["excluded"] for c in control),
        "rejected": len(derived.rejected),
        "derive_seconds": round(derived_s, 1),
        "book_seconds": round(booked_s, 1),
        "lines": len(result.lines),
        "counts": dict(sorted(result.counts.items())),
        "suspense_lines": sum(1 for ln in result.lines if ln.is_suspense),
        "rounding_lines": sum(1 for ln in result.lines if ln.template == "rounding"),
        "failed_types": result.failed_types,
        "oracle": {
            "net_keys": len(oracle[0]),
            "differing_transaction": _differing(engine[0], oracle[0]),
            "differing_functional": _differing(engine[1], oracle[1]),
            "equal": engine == oracle,
        },
        "invariants": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in invariants.items()},
    }
    ok = out["accounted"] and not derived.rejected and not result.failed_types and engine == oracle
    ok = ok and all(ok_ for ok_, _ in invariants.values())
    if matrix:
        out["matrix"] = _matrix(config, derived, ref)
        ok = ok and not out["matrix"]["mismatched"]
    if flip:
        out["flip_guard"] = _flips(config, derived, ref)
        ok = ok and not out["flip_guard"]["unchanged"] and not out["flip_guard"]["mismatched"]
    out["ok"] = ok
    return out


def _peak_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024, 1)  # spark-ban: allow SB008 reporting


def main(argv: list[str]) -> int:
    specs = argv or list(DEFAULT_RUNS)
    runs: dict[str, Any] = {}
    for i, spec in enumerate(specs):
        profile, seed = spec.split(":")
        runs[spec] = score_run(profile, int(seed), matrix=i == 0, flip=i == len(specs) - 1)
        gc.collect()  # release one run's data before the next
    verdict = "PASS" if all(r["ok"] for r in runs.values()) else "FAIL"
    sys.stdout.write(json.dumps({"verdict": verdict, "peak_rss_mib": _peak_mib(), "runs": runs}, sort_keys=True) + "\n")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
