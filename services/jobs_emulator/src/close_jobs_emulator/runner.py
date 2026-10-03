"""Executes queued runs one at a time (local memory policy), like a single-node Lakeflow job cluster.

Each run is a separate process group, so cancellation and timeouts can stop the whole Spark driver
tree: SIGTERM first, SIGKILL after a grace period. Stdout is reserved for the job's single
``CLOSE_JOB_RESULT`` line (returned by ``runs/get-output``); Spark logs go to stderr.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from close_jobs_emulator.bundle import JobDefinition
from close_jobs_emulator.store import RunRow, RunStore

log = logging.getLogger(__name__)

CommandBuilder = Callable[[JobDefinition, list[str]], Sequence[str]]
# A job gets the container environment minus the emulator's own credentials and the raw S3 keys
# (the launcher hands those to Spark as AWS_* variables). CLOSE_FAULTS still reaches the job.
SCRUBBED_PREFIXES = ("EMULATOR_", "CLOSE_S3_")


class Outcome(NamedTuple):
    """How Lakeflow Jobs reports a finished run (legacy ``result_state`` + 2.2 ``termination_details``)."""

    result_state: str
    termination_code: str
    termination_type: str


SUCCEEDED = Outcome("SUCCESS", "SUCCESS", "SUCCESS")
FAILED = Outcome("FAILED", "RUN_EXECUTION_ERROR", "CLIENT_ERROR")
CANCELED = Outcome("CANCELED", "USER_CANCELED", "CLIENT_ERROR")
TIMED_OUT = Outcome("TIMEDOUT", "RUN_EXECUTION_ERROR", "CLIENT_ERROR")


@dataclass
class Runner:
    store: RunStore
    jobs: Mapping[int, JobDefinition]
    build_command: CommandBuilder
    logs_dir: Path
    env: Mapping[str, str] = field(default_factory=dict)
    cancel_grace_s: float = 20.0
    poll_s: float = 0.2
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="jobs-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            run = self.store.next_queued()
            if run is None:
                self._stop.wait(self.poll_s)
                continue
            try:
                self._execute(run)
            except Exception:  # never let one broken run stop the runner
                log.exception("run %s crashed the runner", run.run_id)
                self.store.update(
                    run.run_id,
                    state_v2="TERMINATED",
                    life_cycle_state="INTERNAL_ERROR",
                    result_state="FAILED",
                    termination_code="INTERNAL_ERROR",
                    termination_type="INTERNAL_ERROR",
                    state_message="emulator failed to execute the run",
                    end_time=time.time(),
                )

    def _execute(self, run: RunRow) -> None:
        job = self.jobs[run.job_id]
        if run.cancel_requested:
            self._finish(run.run_id, CANCELED, "cancelled while queued", None)
            return
        stdout_path = self.logs_dir / f"{run.run_id}.stdout"
        stderr_path = self.logs_dir / f"{run.run_id}.stderr"
        args = job.render_args(run.job_parameters)
        command = list(self.build_command(job, args))
        self.store.update(
            run.run_id,
            state_v2="RUNNING",
            life_cycle_state="RUNNING",
            start_time=time.time(),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(  # noqa: S603 (command comes from the bundle, not from API input)
                command, stdout=out, stderr=err, env=self._child_env(), start_new_session=True
            )
        deadline = time.monotonic() + job.timeout_seconds
        stop_reason: str | None = None
        term_sent_at: float | None = None
        while proc.poll() is None:
            current = self.store.get(run.run_id)
            if stop_reason is None and current is not None and current.cancel_requested:
                stop_reason = "cancel"
            if stop_reason is None and time.monotonic() > deadline:
                stop_reason = "timeout"
            if stop_reason is not None and term_sent_at is None:
                self.store.update(run.run_id, state_v2="TERMINATING", life_cycle_state="TERMINATING")
                self._signal(proc, signal.SIGTERM)
                term_sent_at = time.monotonic()
            elif term_sent_at is not None and time.monotonic() - term_sent_at > self.cancel_grace_s:
                self._signal(proc, signal.SIGKILL)
            time.sleep(self.poll_s)
        code = proc.returncode
        if stop_reason == "cancel":
            self._finish(run.run_id, CANCELED, "run cancelled", code)
        elif stop_reason == "timeout":
            self._finish(run.run_id, TIMED_OUT, "run timed out", code)
        elif code == 0:
            self._finish(run.run_id, SUCCEEDED, "", code)
        else:
            self._finish(run.run_id, FAILED, f"exit code {code}", code)

    def _child_env(self) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES)}
        return {**base, **self.env}

    @staticmethod
    def _signal(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)

    def _finish(self, run_id: int, outcome: Outcome, message: str, exit_code: int | None) -> None:
        self.store.update(
            run_id,
            state_v2="TERMINATED",
            life_cycle_state="TERMINATED",
            result_state=outcome.result_state,
            termination_code=outcome.termination_code,
            termination_type=outcome.termination_type,
            state_message=message,
            end_time=time.time(),
            exit_code=exit_code,
        )
