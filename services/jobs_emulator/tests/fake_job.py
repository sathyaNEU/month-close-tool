"""Stand-in for ``engine/scripts/run_job.py`` in emulator tests (no Spark): behaviour by --rows.

rows == 13  -> exit 1 (job failure)
rows == 7   -> sleep 600 (for cancel / timeout tests; handles SIGTERM like a real driver)
otherwise   -> print one CLOSE_JOB_RESULT line echoing the parameters and exit 0
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

args = sys.argv[1:]
values = {args[i].lstrip("-"): args[i + 1] for i in range(1, len(args) - 1, 2)}
rows = int(values.get("rows", "0"))
if rows == 13:
    sys.stderr.write("simulated failure\n")
    sys.exit(1)
if rows == 7:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    time.sleep(600)
print("some log line on stdout that is not the result")
env_keys = sorted(k for k in os.environ if k.startswith(("EMULATOR_", "CLOSE_", "AWS_")))
result = {"job": args[0], "params": values, "rows": rows, "fenced": False, "env_keys": env_keys}
print("CLOSE_JOB_RESULT " + json.dumps(result))
