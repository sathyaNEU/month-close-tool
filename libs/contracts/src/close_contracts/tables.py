"""Logical table names. Engine code never hard-codes a physical table name or path.

A :class:`LogicalTable` is resolved to a physical identifier by a
:class:`close_contracts.resolver.TableResolver`: a Delta path locally, a Unity Catalog three-level name
on Databricks. SQL templates reference tables only through ``IDENTIFIER(:t)`` parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from close_contracts.enums import Schema

_TABLE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True, order=True)
class LogicalTable:
    """A table identified by schema and name, independent of catalog or storage location."""

    schema: Schema
    name: str

    def __post_init__(self) -> None:
        if not _TABLE_NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid table name {self.name!r}: expected ^[a-z][a-z0-9_]{{0,62}}$")

    @property
    def key(self) -> str:
        """Stable string key, e.g. ``ops.run_manifests`` (used in manifests and input pins)."""
        return f"{self.schema.value}.{self.name}"


class Tables:
    """Registry of platform tables. Grows stage by stage; entries are never renamed."""

    OPS_SMOKE_RESULTS: Final = LogicalTable(Schema.OPS, "smoke_results")
    OPS_RUN_MANIFESTS: Final = LogicalTable(Schema.OPS, "run_manifests")
    OPS_SCHEMA_MIGRATIONS: Final = LogicalTable(Schema.OPS, "schema_migrations")
    OPS_WRITER_CLAIMS: Final = LogicalTable(Schema.OPS, "writer_claims")
    LEDGER_JOURNAL_LINES: Final = LogicalTable(Schema.LEDGER, "journal_lines")  # Stage 7b
    LEDGER_JOURNAL_POSTINGS: Final = LogicalTable(Schema.LEDGER, "journal_postings")
    REF_FX_RATES: Final = LogicalTable(Schema.REF, "fx_rates")  # Stage 7c: read only by the narrow rate lookup
    REF_ENTITIES: Final = LogicalTable(Schema.REF, "entities")  # Stage 8b: read only by the narrow calendar lookup
    REF_CALENDARS: Final = LogicalTable(Schema.REF, "calendars")
    LEDGER_SUBLEDGER_LINES: Final = LogicalTable(Schema.LEDGER, "subledger_lines")  # Stage 8a: the journaling stand-in
    LEDGER_GL_ROLLUP_LINES: Final = LogicalTable(Schema.LEDGER, "gl_rollup_lines")

    @classmethod
    def all(cls) -> tuple[LogicalTable, ...]:
        found = [v for v in vars(cls).values() if isinstance(v, LogicalTable)]
        return tuple(sorted(found))
