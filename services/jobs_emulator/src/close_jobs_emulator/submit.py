"""Launch one engine job with exactly the emulator's spark-submit command, outside the emulator.

The fault-injection zombie writer uses this, so it runs with the same jars, Delta and S3A settings
as a real run but with no control plane behind it:

    docker compose exec faults python -m close_jobs_emulator.submit smoke --run-id ... --fencing-token 1

Arguments after the module name are the job's own (the same list the bundle renders for a run).
The exit code is the job's (3 means it was fenced).
"""

from __future__ import annotations

import os
import subprocess
import sys

from close_jobs_emulator.__main__ import spark_submit_builder
from close_jobs_emulator.runner import SCRUBBED_PREFIXES


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("usage: python -m close_jobs_emulator.submit <job> [job args...]")
    build, child_env = spark_submit_builder(os.environ)
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES)} | child_env
    return subprocess.run(list(build(None, args)), env=env, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    sys.exit(main())
