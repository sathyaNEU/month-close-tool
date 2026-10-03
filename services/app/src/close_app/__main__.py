"""``python -m close_app``: the Databricks App command (app.yaml) and the local container command.

The port comes from ``DATABRICKS_APP_PORT`` (set by Databricks Apps; compose sets it too), read here
rather than on the command line so no shell expansion is needed.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "close_app.main:app_factory",
        factory=True,
        host=os.environ.get("CLOSE_BIND_HOST", "0.0.0.0"),  # noqa: S104  # nosec B104 (reachable only via the proxy)
        port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
        access_log=False,  # close_app.logs writes one JSON line per request
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
