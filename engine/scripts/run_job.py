"""spark-submit launcher: ``spark-submit engine/scripts/run_job.py <kind> [job args...]``.

spark-submit runs a Python *file*, not a module, so this file only forwards to the job dispatcher.
All logic lives in the ``close_engine`` wheel.
"""

from __future__ import annotations

import sys

from close_engine.jobs.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
