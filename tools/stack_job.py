"""Run one bundle job through the Jobs API and print its outcome as JSON (runs INSIDE the app container).

The app container holds the service principal's Jobs API credentials (``DATABRICKS_*``) and is on the
backend network, so a host tool pipes this file into it:

    docker compose exec -T app python - <job-name> <job-id> [name=value ...] < tools/stack_job.py

The job id is the one pinned in compose.yaml (``EMULATOR_JOB_IDS``); the tool checks the job's name with
``jobs/get`` before running it.

The job's own parameters are given as ``name=value``; ``run_id`` and ``requested_at`` are filled in when not
given. The printed JSON has the run's life-cycle and result state, its wall-clock seconds from submission
to termination, and the job's ``CLOSE_JOB_RESULT`` line.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from databricks.sdk import WorkspaceClient

PREFIX = "CLOSE_JOB_RESULT "


def main(argv: list[str]) -> int:
    name, job_id_text, *pairs = argv
    job_id = int(job_id_text)
    params = dict(p.split("=", 1) for p in pairs)
    params.setdefault("run_id", uuid.uuid4().hex)
    params.setdefault("requested_at", datetime.now(tz=UTC).isoformat())
    w = WorkspaceClient()
    job = w.jobs.get(job_id=job_id)
    expected_name = f"close-{name.replace('_', '-')}"
    if job.settings is None or job.settings.name != expected_name:
        raise SystemExit(f"job {job_id} is not {expected_name}")
    started = time.monotonic()
    run = w.jobs.run_now(job_id=job_id, job_parameters=params).result(timeout=timedelta(hours=1))
    seconds = time.monotonic() - started
    task_run = run.tasks[0].run_id if run.tasks else run.run_id
    logs = getattr(w.jobs.get_run_output(run_id=task_run), "logs", "") or "" if task_run is not None else ""
    lines = [ln[len(PREFIX) :] for ln in logs.splitlines() if ln.startswith(PREFIX)]
    state = run.state
    print(
        json.dumps(
            {
                "job": name,
                "run_id": run.run_id,
                "life_cycle": state.life_cycle_state.value if state and state.life_cycle_state else None,
                "result_state": state.result_state.value if state and state.result_state else None,
                "seconds": round(seconds, 1),
                "result": json.loads(lines[0]) if lines else None,
            },
            sort_keys=True,
        )
    )
    return 0 if state and state.result_state and state.result_state.value == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
