"""FX rates for manual journals (Stage 7c, ADR-0018): the typed key a rate is looked up by, and the rate itself.

Rates are global reference data in ``ref.fx_rates``, not entity data, so they are read through one narrow call
(:mod:`close_platform.query.rates`) and never through query specs, whose entity filter stays as Stage 1b signed it off.

- **A key** names a currency pair, a day and a rate type. Both currencies must be ISO 4217 codes the platform
  knows, and they must differ. A key is the only input a lookup takes, so no caller value reaches Spark except
  as a typed literal.
- **A rate** is the latest version of that key's row, with its source and the Delta table version it was read
  at. Its decimal is exact (``DECIMAL(19,12)``) and travels as a string in JSON, never as a float.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from close_contracts.money import RATE_SCALE, MoneyError, check_rate, exponent

RATE_TYPES: Final = ("daily_spot", "closing")
MAX_KEYS: Final = 500  # distinct keys in one lookup: a full batch, each member with its own date
_QUANTUM: Final = Decimal(1).scaleb(-RATE_SCALE)


class RateKeyError(ValueError):
    """A rate key that can't be looked up: an unknown currency, the same currency twice, or an unknown type."""


@dataclass(frozen=True, order=True)
class RateKey:
    from_currency: str
    to_currency: str
    on: date
    rate_type: str = "daily_spot"

    def __post_init__(self) -> None:
        for code in (self.from_currency, self.to_currency):
            try:
                exponent(code)
            except MoneyError:
                raise RateKeyError(f"{code!r} is not a currency the platform knows") from None
        if self.from_currency == self.to_currency:
            raise RateKeyError(f"a rate converts between two currencies, not {self.from_currency} to itself")
        if isinstance(self.on, datetime) or not isinstance(self.on, date):
            raise RateKeyError("a rate is for a day (a date, not a time)")
        if self.rate_type not in RATE_TYPES:
            raise RateKeyError(f"rate type {self.rate_type!r} is not one of {', '.join(RATE_TYPES)}")

    def label(self) -> str:
        return f"{self.rate_type} {self.from_currency}->{self.to_currency} on {self.on.isoformat()}"


def rate_text(value: Decimal) -> str:
    """A rate's canonical text: exactly 12 decimals, so the same rate always hashes the same."""
    return f"{check_rate(value).quantize(_QUANTUM):f}"


@dataclass(frozen=True)
class Rate:
    key: RateKey
    rate: Decimal
    version: int
    source: str
    table_version: int | None = None  # the Delta version of ref.fx_rates the rate was read at

    def __post_init__(self) -> None:
        check_rate(self.rate)
        if self.version < 1:
            raise ValueError("a rate's version starts at 1")

    def as_json(self) -> dict[str, Any]:
        return {
            "from_currency": self.key.from_currency,
            "to_currency": self.key.to_currency,
            "rate_date": self.key.on.isoformat(),
            "rate_type": self.key.rate_type,
            "rate": rate_text(self.rate),
            "version": self.version,
            "source": self.source,
            "table_version": self.table_version,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Rate:
        key = RateKey(
            str(data["from_currency"]),
            str(data["to_currency"]),
            date.fromisoformat(str(data["rate_date"])),
            str(data["rate_type"]),
        )
        version = data.get("table_version")
        return cls(
            key,
            Decimal(str(data["rate"])),
            int(data["version"]),
            str(data["source"]),
            None if version is None else int(version),
        )


def check_keys(keys: Iterable[RateKey]) -> frozenset[RateKey]:
    found = frozenset(keys)
    if len(found) > MAX_KEYS:
        raise RateKeyError(f"at most {MAX_KEYS} rates in one lookup")
    return found


def latest(
    rows: Iterable[Mapping[str, Any]], keys: Iterable[RateKey], table_version: int | None
) -> dict[RateKey, Rate]:
    """The latest version of each key's row among ``rows`` (``ref.fx_rates`` columns); keys without one are absent."""
    wanted = check_keys(keys)
    best: dict[RateKey, Mapping[str, Any]] = {}
    for row in rows:
        day = row["rate_date"]
        if isinstance(day, datetime):
            day = day.date()
        try:
            key = RateKey(str(row["from_currency"]), str(row["to_currency"]), day, str(row["rate_type"]))
        except RateKeyError:
            continue  # a row the platform can't use (an unknown currency) is no rate
        if key in wanted and (key not in best or int(row["version"]) > int(best[key]["version"])):
            best[key] = row
    return {
        key: Rate(key, Decimal(str(row["rate"])), int(row["version"]), str(row["source"]), table_version)
        for key, row in best.items()
    }
