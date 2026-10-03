"""A tiny hand-built event type and helpers for provoking each edge case of the journaling engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from close_contracts.config.journaling import AccountMapping, AutoJournalDefinition, EventType
from close_contracts.default_sources import col, fn, s
from close_contracts.samples.journaling import SAMPLE_CHART, SAMPLE_DIMENSIONS, SAMPLE_POLICY
from close_engine.reference.journal.events import Event
from close_engine.reference.journal.posting import JournalConfig, ReferenceData

DAY = date(2026, 7, 1)
TEST_TYPE = EventType.model_validate(
    {
        "type_id": "test_event",
        "event_type": "TEST_EVENT",
        "description": "a test event",
        "amounts": ["a", "b"],
        "attributes": ["key"],
        "identities": [{"name": "a_is_b", "expr": fn("eq", col("amount_a"), col("amount_b"))}],
        "sources": [
            {
                "source_id": "orders",
                "landing_table": "landing.orders",
                "mapping": {
                    "entity_id": col("entity_id"),
                    "currency": col("currency"),
                    "accounting_date": fn("date_of", col("placed_at")),
                },
            }
        ],
    }
)
REF = ReferenceData(
    functional={"US01": "USD", "EU01": "EUR"},
    exponents={"USD": 2, "EUR": 2},
    rates={(DAY, "EUR", "USD"): (Decimal("1.083333333333"), 3)},
    closed={},
)
KEY_MAPPING = AccountMapping.model_validate(
    {
        "mapping_id": "test_key_account",
        "description": "t",
        "keys": ["key"],
        "rows": [{"match": ["known"], "account": "4010"}],
    }
)


def line(line_id: str, side: str, amount: str, account: Any = None, **extra: Any) -> dict[str, Any]:
    return {"line_id": line_id, "side": side, "account": account or {"account": "1010"}, "amount": col(amount), **extra}


def definition(*lines: dict[str, Any]) -> AutoJournalDefinition:
    return AutoJournalDefinition.model_validate(
        {"definition_id": "test_definition", "name": "test", "event_types": ["TEST_EVENT"], "lines": list(lines)}
    )


def config(*definitions: AutoJournalDefinition) -> JournalConfig:
    return JournalConfig(
        policy=SAMPLE_POLICY,
        chart=SAMPLE_CHART,
        dimensions=SAMPLE_DIMENSIONS,
        mappings={KEY_MAPPING.mapping_id: KEY_MAPPING},
        event_types={"TEST_EVENT": TEST_TYPE},
        definitions=definitions,
    )


def event(
    n: int, a: int, b: int, *, key: str = "known", currency: str = "USD", day: date = DAY, **fields: Any
) -> Event:
    values = {
        "entity_id": "US01",
        "currency": currency,
        "accounting_date": day,
        "program": "direct_ship",
        "order_id": None,
        "seller_id": None,
        "amount_a": a,
        "amount_b": b,
        "attr_key": key,
    } | fields
    return Event(f"e{n:03d}", "TEST_EVENT", "orders", "landing.orders", f'["{n}"]', f"h{n}-{a}-{b}", values)


BALANCED = definition(line("dr", "DR", "amount_a"), line("cr", "CR", "amount_b", {"account": "2010"}))
KEYED = definition(
    line("dr", "DR", "amount_a"),
    line(
        "cr",
        "CR",
        "amount_b",
        {"mapping": "test_key_account", "keys": [col("attr_key")]},
        dimensions={"program": col("program")},
    ),
)
__all__ = ["s"]
