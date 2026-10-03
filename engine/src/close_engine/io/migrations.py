"""Delta schema migrations (roadmap engineering rule 8): numbered ``V###__name.sql`` files.

The files ship inside the engine wheel (``close_engine/sql/ddl``) so ``ddl_apply`` reads exactly what was
released. A file holds one or more DDL statements, each ending with ``;`` at the end of a line, and names
tables only through ``{schema__table}`` placeholders, which must be registered in
:mod:`close_contracts.table_schemas` and are rendered by the resolver through :func:`close_engine.io.sql.run_ddl`
(so data values never reach SQL text). Every file is checksummed; ``ddl_apply`` records the checksum in
``ops.schema_migrations`` and refuses to run if a file that was already applied has changed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

from close_contracts.table_schemas import REGISTRY, Column, TableSchema
from close_contracts.tables import LogicalTable

PACKAGE: Final = "close_engine.sql.ddl"
_FILE: Final = re.compile(r"^V(\d{3})__([a-z0-9_]+)\.sql$")
_PLACEHOLDER: Final = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_BY_PLACEHOLDER: Final = {f"{t.schema.value}__{t.name}": t for t in REGISTRY}


class MigrationError(ValueError):
    """A malformed migration set or file."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    statements: tuple[str, ...]


def split_statements(text: str) -> tuple[str, ...]:
    """Statements end with ``;`` at the end of a line; ``--`` comment lines are dropped."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    body = "\n".join(lines)
    parts = [p.strip() for p in re.split(r";[ \t]*(?:\n|$)", body)]
    return tuple(p for p in parts if p)


def tables_in(statement: str) -> dict[str, LogicalTable]:
    """The placeholders of a statement mapped to registered tables."""
    out = {}
    for name in _PLACEHOLDER.findall(statement):
        table = _BY_PLACEHOLDER.get(name)
        if table is None:
            raise MigrationError(f"placeholder {{{name}}} is not a registered table (schema__table)")
        out[name] = table
    return out


def parse(filename: str, data: bytes) -> Migration:
    m = _FILE.match(filename)
    if m is None:
        raise MigrationError(f"{filename}: migrations are named V###__name.sql")
    statements = split_statements(data.decode("utf-8"))
    if not statements:
        raise MigrationError(f"{filename}: no statements")
    for statement in statements:
        tables_in(statement)
    return Migration(int(m.group(1)), m.group(2), hashlib.sha256(data).hexdigest(), statements)


def load() -> list[Migration]:
    """Every packaged migration, in version order; versions must run 1, 2, 3 ... without gaps."""
    root = resources.files(PACKAGE)
    found = [parse(entry.name, entry.read_bytes()) for entry in root.iterdir() if entry.name.endswith(".sql")]
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps or repeats, got {versions}")
    return found


def placeholder(table: LogicalTable) -> str:
    return f"{{{table.schema.value}__{table.name}}}"


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def create_table_sql(schema: TableSchema) -> str:
    """``CREATE TABLE IF NOT EXISTS`` for a registry entry, as written into the initial migrations."""

    def column(c: Column) -> str:
        not_null = "" if c.nullable else " NOT NULL"
        comment = f" COMMENT {_quote(c.comment)}" if c.comment else ""
        return f"  {c.name} {c.type.upper()}{not_null}{comment}"

    columns = ",\n".join(column(c) for c in schema.columns)
    parts = [f"CREATE TABLE IF NOT EXISTS {placeholder(schema.table)} (\n{columns}\n) USING DELTA"]
    if schema.partitioned_by:
        parts.append(f"PARTITIONED BY ({', '.join(schema.partitioned_by)})")
    if schema.comment:
        parts.append(f"COMMENT {_quote(schema.comment)}")
    properties = dict(schema.extra)
    if schema.append_only:
        properties["delta.appendOnly"] = "true"
    if properties:
        rendered = ", ".join(f"{_quote(k)} = {_quote(v)}" for k, v in sorted(properties.items()))
        parts.append(f"TBLPROPERTIES ({rendered})")
    return "\n".join(parts)
