"""Run the generator several times in one Spark session and print the landing digests of every run.

    CLOSE_SPARK_MASTER=local[4] python -m close_datagen.determinism --profile small --seeds 11,23,47 --runs 3

Each run writes into its own empty lake root, so every digest describes one complete generation. The host
tool ``tools/datagen_determinism.py`` runs this under two masters and compares the results.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from close_datagen.job import run
from close_engine.runctx import RunContext


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="small")
    parser.add_argument("--seeds", default="11,23,47")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    out: dict[str, list[dict[str, str]]] = {}
    with tempfile.TemporaryDirectory(prefix="datagen-det-") as tmp:
        for seed in (int(s) for s in args.seeds.split(",")):
            for n in range(args.runs):
                root = Path(tmp, f"s{seed}-r{n}")
                ctx = RunContext(
                    f"det-{seed}-{n}", 1, "datagen:det", 1, datetime(2026, 9, 28, tzinfo=UTC), root.as_uri(), None
                )
                result = run(ctx, args.profile, seed)
                if not result["ok"]:
                    sys.stdout.write(json.dumps({"error": result}, default=str) + "\n")
                    return 1
                out.setdefault(str(seed), []).append(result["digests"])  # type: ignore[arg-type]
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
