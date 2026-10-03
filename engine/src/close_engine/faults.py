"""Fault-injection pause points, for tests that need precise interleavings (zombie writers, crashes).

``CLOSE_FAULTS`` holds ``name=/path/to/file`` pairs separated by commas. When a job reaches a pause
point with that name it waits until the file exists. The mechanism is disabled on Databricks (and
whenever CLOSE_FAULTS is unset), so production jobs can never block on it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path

from close_engine.session import on_databricks

DEFAULT_TIMEOUT_S = 300.0


class FaultTimeoutError(RuntimeError):
    pass


def _points(environ: Mapping[str, str]) -> dict[str, str]:
    raw = environ.get("CLOSE_FAULTS", "")
    points = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        name, _, path = item.partition("=")
        if name and path:
            points[name] = path
    return points


def pause_point(name: str, environ: Mapping[str, str] = os.environ, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    """Block until the release file for ``name`` exists. Returns True if the job actually paused."""
    if on_databricks(environ):
        return False
    path = _points(environ).get(name)
    if path is None:
        return False
    Path(f"{path}.reached").touch()  # tell the test we are here
    deadline = time.monotonic() + timeout_s
    while not Path(path).exists():
        if time.monotonic() > deadline:
            raise FaultTimeoutError(f"pause point {name!r} was never released ({path})")
        time.sleep(0.05)
    return True
