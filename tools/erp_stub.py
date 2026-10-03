"""The stub ERP (Stage 7b, ADR-0017): reads the ledger feed and writes document numbers back.

It plays the part an ERP plays in production, through the same API a real integration would use:

1. Sign in through devproxy as ``erp`` (user 1006, an ``integration_client`` in US01).
2. Page through ``GET /api/v1/gl/postings`` for the entity: one row per posted journal, with its canonical JSON.
3. "Book" each journal whose lines balance (in its own currency, and in the functional one with any rounding line),
   numbering it ``ERP-<entity>-<journal number>``, so a rerun assigns the same numbers and the write-back treats
   them as no-ops. An unbalanced payload is refused and reported.
4. Write the numbers back with ``POST /api/v1/posting/status``, at most 500 per request.

    uv run python tools/erp_stub.py --entity US01      # prints a JSON summary; exit 1 if anything was refused
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

BASE = "http://127.0.0.1:8088"
CHUNK = 500


def feed(client: httpx.Client, entity_id: str) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    while True:
        params = {"entity_id": entity_id, "page_size": "500"} | ({"cursor": cursor} if cursor else {})
        r = client.get("/api/v1/gl/postings", params=params)
        r.raise_for_status()
        page = r.json()
        yield from page["postings"]
        cursor = page["next_cursor"]
        if not cursor:
            return


def document_id(posting: dict[str, Any]) -> str:
    return f"ERP-{posting['entity_id']}-{int(posting['number']):06d}"


def balanced(posting: dict[str, Any]) -> bool:
    """The journal balances in its own currency, and in the functional one with its rounding line (Stage 7c);
    a feed row from before 7c has no functional amounts, and they are its amounts."""
    lines = json.loads(posting["payload"])["lines"]
    for field in ("amount_minor", "functional_amount_minor"):
        amounts = [(ln["side"], int(ln.get(field, ln["amount_minor"]))) for ln in lines]
        debits = sum(a for side, a in amounts if side == "DR")
        credits = sum(a for side, a in amounts if side == "CR")
        if not debits == credits > 0:
            return False
    return True


def trial_balance_csv(balances: dict[str, Any], erp_only: dict[str, int], decimals: int) -> str:
    """The stub ERP's trial balance for a period (Stage 8a): the platform's closing balances, which is what the ERP
    booked from its feeds (the stub's premise), plus entries made only in the ERP, in minor units by account.

    The extract's contract: ``account,balance``, the balance in major units, debit positive. Zero balances are left
    out."""
    totals = {str(a["account"]): int(a["closing_minor"]) for a in balances["accounts"]}
    for account, minor in erp_only.items():
        totals[account] = totals.get(account, 0) + minor
    rows = []
    for account, minor in sorted(totals.items()):
        if minor:
            sign, units = ("-" if minor < 0 else ""), abs(minor)
            whole, part = divmod(units, 10**decimals)
            rows.append(f"{account},{sign}{whole}" + (f".{part:0{decimals}d}" if decimals else ""))
    return "account,balance\n" + "".join(f"{r}\n" for r in rows)


def upload_trial_balance(client: httpx.Client, entity_id: str, period: str, csv_text: str) -> dict[str, Any]:
    r = client.post(
        "/api/v1/erp/trial-balances",
        params={"entity_id": entity_id, "period": period},
        content=csv_text.encode(),
        headers={"Content-Type": "text/csv", "Idempotency-Key": f"erp-{uuid.uuid4()}"},
    )
    r.raise_for_status()
    found: dict[str, Any] = r.json()
    return found


def run(client: httpx.Client, entity_id: str) -> dict[str, Any]:
    postings = list(feed(client, entity_id))
    booked = [p for p in postings if balanced(p)]
    refused = [p["journal_id"] for p in postings if not balanced(p)]
    confirmed: list[dict[str, Any]] = []
    for start in range(0, len(booked), CHUNK):
        entries = [
            {"journal_id": p["journal_id"], "document_id": document_id(p)} for p in booked[start : start + CHUNK]
        ]
        r = client.post(
            "/api/v1/posting/status", json={"entries": entries}, headers={"Idempotency-Key": f"erp-{uuid.uuid4()}"}
        )
        r.raise_for_status()
        confirmed += r.json()["confirmed"]
    return {"entity_id": entity_id, "read": len(postings), "confirmed": confirmed, "refused": refused}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entity", default="US01")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args(argv)
    with httpx.Client(base_url=args.base, timeout=60) as client:
        client.get("/__dev/login/erp").raise_for_status()
        summary = run(client, args.entity)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
