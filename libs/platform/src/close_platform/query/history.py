"""The read path's only SQL: reading a Delta table's latest version with ``DESCRIBE HISTORY``.

There is no DataFrame API for it over Spark Connect (``DeltaTable`` needs the JVM), so this module is
the second allow-listed ``spark.sql`` gateway (lint rule SB011). The identifier comes from a
TableResolver, which only renders validated logical tables, and nothing else is interpolated.
"""

from __future__ import annotations

from typing import Any

from close_contracts.resolver import TableResolver
from close_contracts.tables import LogicalTable

_MISSING_MARKERS = ("DELTA_MISSING_DELTA_TABLE", "PATH_NOT_FOUND", "TABLE_OR_VIEW_NOT_FOUND", "is not a Delta table")


class MissingTableError(LookupError):
    pass


def latest_version(session: Any, resolver: TableResolver, table: LogicalTable) -> int:
    identifier = resolver.table(table)
    try:
        arrow = session.sql(f"DESCRIBE HISTORY {identifier} LIMIT 1").select("version").toArrow()
    except Exception as exc:  # pyspark raises AnalysisException subclasses that differ between classic and Connect
        if any(marker in str(exc) for marker in _MISSING_MARKERS):
            raise MissingTableError(table.key) from exc
        raise
    versions = arrow.column("version").to_pylist()
    if not versions:
        raise MissingTableError(table.key)
    return int(versions[0])
