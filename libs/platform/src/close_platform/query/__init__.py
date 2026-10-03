"""The read path: typed QuerySpecs (close_contracts.query) run by a QueryEngine over a backend.

Backends: ``dataframe.DataFrameBackend`` (Spark Connect in the app, a local session in tests),
``reference.ReferenceBackend`` (pure Python, for tests) and ``sql.render`` (parameterised SQL for a
future SQL warehouse). Importing this package does not import pyspark; only the DataFrame backend and
the Spark Connect sessions do.
"""

from close_platform.query.engine import (
    Caller,
    Page,
    QueryBackend,
    QueryBusyError,
    QueryEngine,
    QueryPlan,
    QueryTimeoutError,
    QueryUnavailableError,
    TableMissingError,
)

__all__ = [
    "Caller",
    "Page",
    "QueryBackend",
    "QueryBusyError",
    "QueryEngine",
    "QueryPlan",
    "QueryTimeoutError",
    "QueryUnavailableError",
    "TableMissingError",
]
