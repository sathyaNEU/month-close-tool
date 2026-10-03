"""Assembling a journaling run: configuration (the samples by default) and reference data from the lake's
``ref`` tables (entities, currencies, daily spot rates, period status)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from close_contracts.calendar import BusinessCalendar
from close_contracts.config.accounting_policy import AccountingPolicy
from close_contracts.samples.journaling import (
    SAMPLE_CHART,
    SAMPLE_DEFINITIONS,
    SAMPLE_DIMENSIONS,
    SAMPLE_EVENT_TYPES,
    SAMPLE_MAPPINGS,
    SAMPLE_POLICY,
)
from close_engine.reference.journal.events import Derived, derive
from close_engine.reference.journal.ledger import LedgerState, RunResult, run
from close_engine.reference.journal.posting import JournalConfig, ReferenceData

Row = Mapping[str, Any]


def sample_config(policy: AccountingPolicy = SAMPLE_POLICY) -> JournalConfig:
    return JournalConfig(
        policy=policy,
        chart=SAMPLE_CHART,
        dimensions=SAMPLE_DIMENSIONS,
        mappings={m.mapping_id: m for m in SAMPLE_MAPPINGS},
        event_types={e.event_type: e for e in SAMPLE_EVENT_TYPES},
        definitions=SAMPLE_DEFINITIONS,
    )


def reference_from(
    tables: Mapping[str, Sequence[Row]], closed: Mapping[str, frozenset[str]] | None = None
) -> ReferenceData:
    rates: dict[tuple[Any, str, str], tuple[Decimal, int]] = {}
    for r in tables.get("ref.fx_rates", ()):
        if r["rate_type"] != "daily_spot":
            continue
        key = (r["rate_date"], str(r["from_currency"]), str(r["to_currency"]))
        if key not in rates or int(r["version"]) > rates[key][1]:
            rates[key] = (Decimal(str(r["rate"])), int(r["version"]))
    status: dict[str, set[str]] = {}
    for p in tables.get("ref.periods", ()):
        if p["status"] != "open":
            status.setdefault(str(p["entity_id"]), set()).add(str(p["period"]))
    for entity, periods in (closed or {}).items():
        status.setdefault(entity, set()).update(periods)
    return ReferenceData(
        functional={str(e["entity_id"]): str(e["functional_currency"]) for e in tables.get("ref.entities", ())},
        exponents={str(c["currency"]): int(c["exponent"]) for c in tables.get("ref.currencies", ())},
        rates=rates,
        closed={k: frozenset(v) for k, v in status.items()},
    )


@dataclass(frozen=True)
class Journaled:
    derived: Derived
    result: RunResult


def journal(
    tables: Mapping[str, Sequence[Row]],
    config: JournalConfig,
    *,
    calendars: Mapping[str, BusinessCalendar] | None = None,
    state: LedgerState | None = None,
    ref: ReferenceData | None = None,
) -> Journaled:
    derived = derive(list(config.event_types.values()), tables, calendars)
    result = run(config, derived.events, ref=ref or reference_from(tables), state=state)
    return Journaled(derived, result)
