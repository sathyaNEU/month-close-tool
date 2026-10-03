"""JSON logs carrying the request id from the Databricks Apps proxy (``X-Request-Id``)."""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_EXTRAS = ("method", "path", "status", "duration_ms", "event_id", "topic", "task", "claim_seq", "instance")


class RequestIdFilter(logging.Filter):
    """Stamps the current request id on each record when it is emitted, so it survives buffering."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
        }
        for key in _EXTRAS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure(environ: dict[str, str] | None = None) -> None:
    """JSON to stderr at INFO; ``CLOSE_LOG_LEVELS=logger=LEVEL,...`` adjusts single loggers
    (the e2e suite turns on ``close_platform.scheduler=DEBUG`` to see every claimed tick)."""
    env = os.environ if environ is None else environ
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for item in filter(None, (part.strip() for part in env.get("CLOSE_LOG_LEVELS", "").split(","))):
        name, _, level = item.partition("=")
        logging.getLogger(name.strip()).setLevel(level.strip().upper())
