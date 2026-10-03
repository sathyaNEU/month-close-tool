"""The engine's only door to SQL text: DDL statements with resolver-rendered table identifiers.

Why this module exists (see docs/adr/0002-spark-delta-parity-allowlist.md):

* OSS Delta Lake 4.1-4.4 drops SQL parameter markers once its extension is loaded
  (delta-io/delta#7640), so ``spark.sql(query, args=...)`` cannot be used anywhere in the engine.
* Data operations therefore use the DataFrame / DeltaTable APIs (Column expressions, never SQL
  strings), and history / detail / optimize use the DeltaTable API too.
* The few statements without an API equivalent (CREATE TABLE, ALTER TABLE ... ADD CONSTRAINT /
  SET TBLPROPERTIES, COMMENT ON) go through :func:`run_ddl`. Table names are substituted only from
  :class:`~close_contracts.tables.LogicalTable` objects rendered by a
  :class:`~close_contracts.resolver.TableResolver` (which validates every component), and data values
  are never rendered into SQL.

The banned-API lint (rules SB010/SB011) enforces that ``spark.sql`` is called only here and never with
``args``.
"""

from __future__ import annotations

import re
from typing import Final

from pyspark.sql import SparkSession

from close_contracts.resolver import TableResolver
from close_contracts.tables import LogicalTable

_PLACEHOLDER: Final = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_ALLOWED_STATEMENT: Final = re.compile(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE|COMMENT\s+ON\s+TABLE)\b", re.IGNORECASE)
_PARAMETER_MARKER: Final = re.compile(r"(?<![:\w]):[A-Za-z_]\w*|\?")


class DDLTemplateError(ValueError):
    """Raised for a malformed DDL template or an attempt to run a non-DDL statement."""


def _strip_string_literals(sql: str) -> str:
    return re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)


def render(template: str, resolver: TableResolver, **tables: LogicalTable) -> str:
    """Substitute ``{name}`` placeholders with resolver identifiers for the given logical tables."""
    names = set(_PLACEHOLDER.findall(template))
    missing = names - tables.keys()
    extra = tables.keys() - names
    if missing or extra:
        raise DDLTemplateError(f"placeholder mismatch: missing={sorted(missing)} unexpected={sorted(extra)}")
    for key, value in tables.items():
        if not isinstance(value, LogicalTable):
            raise DDLTemplateError(f"placeholder {key!r} must be a LogicalTable, got {type(value).__name__}")
    if _PARAMETER_MARKER.search(_strip_string_literals(_PLACEHOLDER.sub("", template))):
        raise DDLTemplateError("SQL parameter markers are not allowed (OSS Delta drops them, delta-io/delta#7640)")
    return _PLACEHOLDER.sub(lambda m: resolver.table(tables[m.group(1)]), template)


def run_ddl(spark: SparkSession, template: str, resolver: TableResolver, **tables: LogicalTable) -> str:
    """Render and execute one CREATE TABLE / ALTER TABLE / COMMENT ON statement; returns the SQL run."""
    statement = render(template, resolver, **tables)
    if not _ALLOWED_STATEMENT.match(statement) or ";" in _strip_string_literals(statement):
        raise DDLTemplateError("run_ddl executes exactly one CREATE TABLE, ALTER TABLE or COMMENT ON TABLE statement")
    spark.sql(statement)
    return statement
