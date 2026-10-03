"""Accounting events from landing rows, through each event type's sources (ADR-0015).

For each source of each event type:
1. Apply the filter first when it reads only the source's own columns, so rows out of scope never need
   their lookups.
2. Look up each configured row of another landing table by its full identity key. The looked-up columns join
   the row as ``<alias>_<column>``.
3. Apply a filter that reads looked-up columns. If one of its lookups found nothing, the row is rejected,
   because whether it is in scope cannot be decided.
4. Map the event's fields with the DSL.

A row whose lookup finds nothing, whose mapping cannot evaluate, or whose required fields are null is rejected.
It is counted, never dropped silently, so each source's control total is read = events + excluded + rejected.
An event's id hashes its type, its source and the row's identity. Its row hash covers the row and the rows it
looked up, so a correction to either makes a new event version.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from close_contracts.calendar import BusinessCalendar
from close_contracts.config.journal_validate import landing_schema, source_row_schema
from close_contracts.config.journaling import REQUIRED_FIELDS, EventSource, EventType, Lookup
from close_contracts.default_sources import IDENTITY_KEYS, source_key
from close_contracts.dsl import DslEvalError, DslType, Ir, IrCall, IrCol, evaluate, typecheck
from close_contracts.ids import hash_id

Row = Mapping[str, Any]
Calendars = Mapping[str, BusinessCalendar] | None


@dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    source_id: str
    source_table: str
    source_key: str
    row_hash: str
    fields: dict[str, Any]


@dataclass
class Derived:
    events: list[Event] = field(default_factory=list)
    control: dict[str, Counter[str]] = field(default_factory=dict)  # "TYPE/source" -> read, events, excluded, rejected
    rejected: list[tuple[str, str, str]] = field(default_factory=list)  # (TYPE/source, source key, reason)


def columns_of(ir: Ir) -> set[str]:
    if isinstance(ir, IrCol):
        return {ir.name}
    if isinstance(ir, IrCall):
        return set().union(*(columns_of(a) for a in ir.args)) if ir.args else set()
    return set()


def _hash(parts: Sequence[Row]) -> str:
    text = json.dumps([dict(p) for p in parts], sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class _Source:
    event_type: EventType
    source: EventSource
    filter: Ir | None
    early: bool  # the filter reads only the source's own columns
    mapping: dict[str, Ir]
    lookups: tuple[tuple[Lookup, tuple[Ir, ...]], ...]

    @property
    def label(self) -> str:
        return f"{self.event_type.event_type}/{self.source.source_id}"


def _compile(event_type: EventType, source: EventSource) -> _Source:
    schema = source_row_schema(source)
    filter_ir = typecheck(source.filter, schema, expect=DslType.BOOLEAN) if source.filter is not None else None
    own = set(landing_schema(source.landing_table) or {})
    early = filter_ir is not None and columns_of(filter_ir) <= own
    mapping = {name: typecheck(expr, schema) for name, expr in source.mapping.items()}
    lookups = tuple((lk, tuple(typecheck(lk.on[k], schema) for k in IDENTITY_KEYS[lk.table])) for lk in source.lookups)
    return _Source(event_type, source, filter_ir, early, mapping, lookups)


class _Deriver:
    def __init__(self, tables: Mapping[str, Sequence[Row]], calendars: Calendars) -> None:
        self.tables, self.calendars = tables, calendars
        self.indexes: dict[str, dict[tuple[Any, ...], Row]] = {}

    def index(self, table: str) -> dict[tuple[Any, ...], Row]:
        if table not in self.indexes:
            keys = IDENTITY_KEYS[table]
            rows: Iterable[Row] = self.tables.get(table, ())
            self.indexes[table] = {tuple(r[k] for k in keys): r for r in rows}
        return self.indexes[table]

    def row(self, s: _Source, row: Row) -> Event | tuple[str, str]:
        """An event, or ("excluded" | "rejected", reason)."""
        cal = self.calendars
        if s.early and s.filter is not None and evaluate(s.filter, row, calendars=cal) is not True:
            return "excluded", ""
        combined, found = dict(row), [row]
        for lk, on in s.lookups:
            target_key = tuple(evaluate(ir, row, calendars=cal) for ir in on)
            target = self.index(lk.table).get(target_key)
            if target is None:
                return "rejected", f"lookup {lk.alias}: no {lk.table} row for {list(target_key)}"
            combined |= {f"{lk.alias}_{col}": value for col, value in target.items()}
            found.append(target)
        if not s.early and s.filter is not None and evaluate(s.filter, combined, calendars=cal) is not True:
            return "excluded", ""
        try:
            fields = {name: evaluate(ir, combined, calendars=cal) for name, ir in s.mapping.items()}
        except DslEvalError as exc:
            return "rejected", str(exc)
        if any(fields.get(f) is None for f in REQUIRED_FIELDS):
            return "rejected", "a required field is null"
        key = source_key([row[k] for k in IDENTITY_KEYS[s.source.landing_table]])
        code = s.event_type.event_type
        return Event(
            hash_id("event", code, s.source.source_id, key),
            code,
            s.source.source_id,
            s.source.landing_table,
            key,
            _hash(found),
            {name: fields.get(name) for name in s.event_type.fields()},
        )


def derive(
    event_types: Sequence[EventType], tables: Mapping[str, Sequence[Row]], calendars: Calendars = None
) -> Derived:
    """Every event of every type from the landing ``tables`` (keyed ``landing.<name>``)."""
    out = Derived()
    deriver = _Deriver(tables, calendars)
    for event_type in event_types:
        for source in event_type.sources:
            s = _compile(event_type, source)
            counts = out.control.setdefault(s.label, Counter())
            for row in tables.get(source.landing_table, ()):
                counts["read"] += 1
                result = deriver.row(s, row)
                if isinstance(result, Event):
                    out.events.append(result)
                    counts["events"] += 1
                else:
                    counts[result[0]] += 1
                    if result[0] == "rejected":
                        key = source_key([row[k] for k in IDENTITY_KEYS[source.landing_table]])
                        out.rejected.append((s.label, key, result[1]))
    out.events.sort(key=lambda e: e.event_id)
    return out
