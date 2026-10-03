"""Generate the shared test-vector files in libs/contracts/src/close_contracts/test_vectors/.

The vectors pin the behaviour of the Python reference implementations (IDs, FX conversion, business
calendars). Spark tests then assert that the Spark implementations reproduce every vector exactly.

Run with ``--check`` in CI: it fails if the committed files differ from what the current
implementation produces, which catches an accidental behaviour change in either direction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from close_contracts.calendar import BusinessCalendar, Roll
from close_contracts.ids import IdPart, hash_id, id_payload
from close_contracts.money import convert_minor

VECTOR_DIR = Path(__file__).resolve().parents[1] / "libs/contracts/src/close_contracts/test_vectors"


def _part_json(p: IdPart) -> dict[str, Any]:
    if p is None:
        return {"t": "null"}
    if isinstance(p, bool):
        return {"t": "bool", "v": p}
    if isinstance(p, int):
        return {"t": "int", "v": p}
    if isinstance(p, date):
        return {"t": "date", "v": p.isoformat()}
    return {"t": "str", "v": p}


def id_cases() -> list[tuple[str, tuple[IdPart, ...]]]:
    base: list[tuple[str, tuple[IdPart, ...]]] = [
        ("itm", ()),
        ("itm", (None,)),
        ("itm", ("",)),
        ("itm", ("abc",)),
        ("itm", ("a", None, "b")),
        ("itm", ("a", "b", None)),
        ("itm", (None, "a", "b")),
        ("itm", ("a", "", "b")),
        ("itm", ("a\x1fb",)),
        ("itm", ("a", "b")),
        ("itm", ("1:a",)),
        ("itm", ("-1:",)),
        ("itm", ("\x1f",)),
        ("itm", ("\x1f", "\x1f")),
        ("grp", (0,)),
        ("grp", (-1,)),
        ("grp", (9_223_372_036_854_775_807,)),
        ("grp", (-9_223_372_036_854_775_808,)),
        ("grp", (True,)),
        ("grp", (False,)),
        ("grp", (1, True)),
        ("grp", ("1", "true")),
        ("evt", (date(2026, 9, 26),)),
        ("evt", (date(2000, 1, 1),)),
        ("evt", (date(1999, 12, 31), date(2100, 12, 31))),
        ("evt", ("2026-09-26",)),
        ("adj", ("\u00e9",)),
        ("adj", ("e\u0301",)),
        ("adj", ("\u65e5\u672c\u8a9e",)),
        ("adj", ("\U0001f600",)),
        ("adj", ("\U0001f600", "\U0001f600\U0001f600")),
        ("adj", ("a" * 1000,)),
        ("jrn", ("US01", "2026-09", 42, None)),
        ("smk", ("run-0001", 0)),
        ("smk", ("run-0001", 999)),
    ]
    marketplace: list[tuple[str, tuple[IdPart, ...]]] = [
        ("itm", ("ms-psp-payout-bank-us01", "ds-bank-lines", "BAI2|16|475|119480|910211979")),
        ("itm", ("ms-psp-payout-bank-us01", "ds-psp-payouts", "po_1NXkq2Lz0000000000000001")),
        ("itm", ("ms-order-capture-us01", "ds-orders", "ORD-2026-000123")),
        ("itm", ("ms-order-capture-us01", "ds-stripe-bt", "txn_3PqR0000000000000000001")),
        ("itm", ("ms-label-invoice", "ds-fedex", "794612345678", date(2026, 9, 1))),
        ("grp", ("run-7f3a", "psp-bank-10", "itm-a|itm-b")),
        ("grp", ("cmd-0001", "manual", "itm-a|itm-b|itm-c")),
        ("adj", ("grp-0001", "create")),
        ("adj", ("grp-0001", "cancel")),
        ("evt", ("PAYMENT_CAPTURED", "ORD-1", 1)),
        ("evt", ("PAYMENT_CAPTURED", "ORD-1", 2)),
        ("evt", ("SELLER_PAYOUT_RESULT", "pmt-9d2c", 1)),
        ("jrn", ("US01", date(2026, 9, 30), "4000-100", "USD")),
        ("isl", ("itm-a", 1)),
        ("isl", ("itm-a", 2)),
    ]
    return base + marketplace


def money_cases() -> list[tuple[int, str, str, str]]:
    return [
        (5, "USD", "USD", "0.5"),
        (7, "USD", "USD", "0.5"),
        (-5, "USD", "USD", "0.5"),
        (-7, "USD", "USD", "0.5"),
        (1, "USD", "USD", "0.5"),
        (3, "USD", "USD", "0.5"),
        (12345, "USD", "EUR", "0.923456789012"),
        (-12345, "USD", "EUR", "0.923456789012"),
        (12345, "USD", "JPY", "149.123456789012"),
        (150, "USD", "JPY", "1"),
        (250, "USD", "JPY", "1"),
        (100, "JPY", "USD", "0.006700000000"),
        (1, "JPY", "USD", "0.006750000000"),
        (123456, "KWD", "USD", "3.254100000000"),
        (100000, "USD", "KWD", "0.307300000000"),
        (999_999_999_999_999, "USD", "EUR", "0.5"),
        (123_456_789_012_345, "EUR", "USD", "1.087654321098"),
        (1, "GBP", "USD", "9999999.999999999999"),
        (987654321, "GBP", "EUR", "0.000000000001"),
        (500, "EUR", "GBP", "0.855"),
        (-500, "EUR", "GBP", "0.855"),
        (1_000_000, "CLF", "USD", "38.123456789012"),
        (42, "USD", "CLF", "0.026230000000"),
    ]


def calendar_fixture() -> BusinessCalendar:
    return BusinessCalendar.of(
        "TEST_US",
        holidays=[date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19), date(2026, 7, 3), date(2026, 12, 25)],
    )


def calendar_dates() -> list[date]:
    days = [date(2025, 12, 20) + timedelta(days=i) for i in range(20)]
    days += [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5), date(2026, 7, 6)]
    days += [date(2000, 1, 3), date(2000, 1, 4), date(2026, 9, 26), date(2026, 9, 28), date(2100, 12, 31)]
    return days


def build() -> dict[str, Any]:
    ids = [
        {
            "ns": ns,
            "parts": [_part_json(p) for p in parts],
            "payload_utf8_hex": id_payload(ns, *parts).encode("utf-8").hex(),
            "expected": hash_id(ns, *parts),
        }
        for ns, parts in id_cases()
    ]
    money = [
        {
            "amount_minor": a,
            "from": f,
            "to": t,
            "rate": r,
            "expected_minor": convert_minor(a, f, t, Decimal(r)),
        }
        for a, f, t, r in money_cases()
    ]
    cal = calendar_fixture()
    calendar = {
        "code": cal.code,
        "holidays": sorted(d.isoformat() for d in cal.holidays),
        "weekend": sorted(cal.weekend),
        "cases": [
            {
                "date": d.isoformat(),
                "is_business_day": cal.is_business_day(d),
                "bd_index_following": cal.bd_index(d, Roll.FOLLOWING),
                "bd_index_preceding": cal.bd_index(d, Roll.PRECEDING),
            }
            for d in calendar_dates()
        ],
    }
    return {"ids.json": ids, "money.json": money, "calendar.json": calendar}


def render(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed vectors are stale")
    args = parser.parse_args(argv)
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, content in build().items():
        path = VECTOR_DIR / name
        text = render(content)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8")
            cases = len(content) if isinstance(content, list) else len(content["cases"])
            print(f"wrote {path.relative_to(VECTOR_DIR.parents[4])} ({cases} cases)")
    if stale:
        print(f"stale test vectors: {', '.join(stale)} (run: uv run python tools/gen_test_vectors.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
