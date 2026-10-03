"""Spark Connect sessions for the app's read path.

One remote session per process, at most ``concurrency`` queries at a time (a request that cannot get
a slot quickly gets QueryBusyError, a 503, instead of queueing forever), and a timeout on every query
that interrupts it on the server through a per-query tag, not merely abandons it. Tags are
thread-local in the Connect client and web server threads are reused, so every tag is removed in a
``finally``. If the server has lost our session (it restarted), the session is rebuilt and the query
retried once.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from close_platform.query.engine import QueryBusyError, QueryTimeoutError

T = TypeVar("T")
_SESSION_GONE = ("INVALID_HANDLE", "SESSION_CLOSED", "SESSION_NOT_FOUND", "session_id is not valid")


class ConnectSessions:
    def __init__(
        self,
        remote: str,
        *,
        concurrency: int = 4,
        timeout_s: float = 30.0,
        acquire_timeout_s: float = 5.0,
        builder: Callable[[str], Any] | None = None,
    ) -> None:
        self._remote = remote
        self._slots = threading.BoundedSemaphore(concurrency)
        self._timeout_s = timeout_s
        self._acquire_timeout_s = acquire_timeout_s
        self._builder = builder or _connect
        self._lock = threading.Lock()
        self._session: Any = None

    def _current(self) -> Any:
        with self._lock:
            if self._session is None:
                self._session = self._builder(self._remote)
            return self._session

    def _discard(self, stale: Any) -> None:
        with self._lock:
            if self._session is stale:
                self._session = None
        with contextlib.suppress(Exception):  # best effort: the server has usually dropped it already
            stale.stop()

    def run(self, work: Callable[[Any], T]) -> T:
        if not self._slots.acquire(timeout=self._acquire_timeout_s):
            raise QueryBusyError("every query slot is busy; retry shortly")
        try:
            for attempt in (1, 2):
                session = self._current()
                try:
                    return self._run_tagged(session, work)
                except QueryTimeoutError:
                    raise
                except Exception as exc:
                    if attempt == 1 and any(marker in str(exc) for marker in _SESSION_GONE):
                        self._discard(session)
                        continue
                    raise
            raise RuntimeError("unreachable")  # pragma: no cover
        finally:
            self._slots.release()

    def _run_tagged(self, session: Any, work: Callable[[Any], T]) -> T:
        tag = f"close-q-{uuid.uuid4().hex}"
        fired = threading.Event()

        def interrupt() -> None:
            fired.set()
            session.interruptTag(tag)

        session.addTag(tag)
        timer = threading.Timer(self._timeout_s, interrupt)
        timer.daemon = True
        timer.start()
        try:
            return work(session)
        except Exception as exc:
            if fired.is_set():
                raise QueryTimeoutError(f"query exceeded {self._timeout_s:.0f}s and was interrupted") from exc
            raise
        finally:
            timer.cancel()
            session.removeTag(tag)

    def readiness(self, expected_version: str, pinned: Mapping[str, str]) -> tuple[bool, str]:
        """The server runs the expected Spark version with every pinned setting in effect."""

        def check(session: Any) -> tuple[bool, str]:
            version = str(session.version)
            wrong = {k: session.conf.get(k) for k, v in pinned.items() if str(session.conf.get(k)).lower() != v.lower()}
            ok = version == expected_version and not wrong
            detail = f"Spark {version}" + (
                f", settings not pinned: {wrong}" if wrong else ", pinned settings in effect"
            )
            return ok, detail

        return self.run(check)


def _connect(remote: str) -> Any:
    from pyspark.sql import SparkSession

    return SparkSession.builder.remote(remote).create()
