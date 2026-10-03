"""Reference normalization in pure Python: landing rows to canonical items through a bound data source.

It is the oracle for the Spark normalize job. Each row the source's filter keeps is mapped with the DSL
interpreter; a row the mapping cannot evaluate is counted as rejected, never dropped silently. The item id is
a hash of the data source and the row's identity key, so the same source row is always the same item.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from close_contracts.binding import BoundSource
from close_contracts.calendar import BusinessCalendar
from close_contracts.default_sources import source_key
from close_contracts.dsl import DslEvalError, evaluate
from close_contracts.ids import hash_id


@dataclass(frozen=True)
class Item:
    item_id: str
    source_id: str
    source_key: str
    fields: dict[str, Any]


@dataclass
class Normalized:
    items: list[Item] = field(default_factory=list)
    excluded: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (source key, reason)


def normalize(
    bound: BoundSource, rows: Iterable[Mapping[str, Any]], calendars: Mapping[str, BusinessCalendar] | None = None
) -> Normalized:
    source = bound.source
    out = Normalized()
    for row in rows:
        key = source_key([row[k] for k in source.identity_key])
        if bound.filter is not None and evaluate(bound.filter, row, calendars=calendars) is not True:
            out.excluded += 1
            continue
        try:
            mapped = {name: evaluate(ir, row, calendars=calendars) for name, ir in bound.mapping.items()}
        except DslEvalError as exc:
            out.rejected.append((key, str(exc)))
            continue
        if any(mapped.get(f) is None for f in ("entity_id", "currency", "amount_minor", "transaction_date")):
            out.rejected.append((key, "a required field is null"))
            continue
        out.items.append(Item(hash_id("item", source.source_id, key), source.source_id, key, mapped))
    return out
