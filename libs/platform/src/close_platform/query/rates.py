"""The narrow FX rate lookup (Stage 7c, ADR-0018): typed keys in, rates out, from ``ref.fx_rates`` only.

It is deliberately not a query spec. ``ref.fx_rates`` is global reference data, not entity data, so the query
engine's entity filter (Stage 1b) stays exactly as signed off for the entity-scoped tables it serves, and no
caller can reach this table through a spec.

- **Typed input.** A call takes :class:`close_contracts.fx.RateKey` values only: ISO currencies the platform
  knows, a date, and a rate type from a closed list. At most 500 keys go in one call.
- **One fixed query.** The keys become literal conditions (``F.lit``, never SQL text) on the four key columns, read
  at one pinned table version, which each rate records. The latest version of each key's row wins.
- **Through the read path's session pool** (concurrency limit, tags, timeouts), like every Spark Connect read.
  The app calls it before opening a database transaction, never inside one.
"""

from __future__ import annotations

import functools
import operator
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any, Final, Protocol

from close_contracts.fx import Rate, RateKey, check_keys, latest
from close_contracts.resolver import PathTableResolver, TableResolver
from close_contracts.tables import Tables
from close_platform.query.history import MissingTableError, latest_version

COLUMNS: Final = ("rate_date", "from_currency", "to_currency", "rate_type", "rate", "version", "source")
SessionRunner = Callable[[Callable[[Any], Any]], Any]  # as the query engine's; pyspark is imported only when used


class RateLookup(Protocol):
    def rates(self, keys: Iterable[RateKey]) -> dict[RateKey, Rate]:
        """The latest rate for each key that has one; keys without a rate are absent."""
        ...


class DataFrameRates:
    """Rates over Spark Connect (the app) or a local session (tests)."""

    def __init__(self, run: SessionRunner, resolver: TableResolver) -> None:
        self._run = run
        self._resolver = resolver

    def rates(self, keys: Iterable[RateKey]) -> dict[RateKey, Rate]:
        wanted = check_keys(keys)
        if not wanted:
            return {}

        def work(session: Any) -> tuple[int | None, list[dict[str, Any]]]:
            from pyspark.sql import functions as F

            try:
                version = latest_version(session, self._resolver, Tables.REF_FX_RATES)
            except MissingTableError:
                return None, []
            reader = session.read.format("delta").option("versionAsOf", version)
            if isinstance(self._resolver, PathTableResolver):
                df = reader.load(self._resolver.location(Tables.REF_FX_RATES))
            else:
                df = reader.table(self._resolver.table(Tables.REF_FX_RATES))
            days: dict[tuple[str, str, str], set[date]] = defaultdict(set)
            for key in wanted:
                days[(key.from_currency, key.to_currency, key.rate_type)].add(key.on)
            conditions = [
                (F.col("from_currency") == F.lit(src))
                & (F.col("to_currency") == F.lit(dst))
                & (F.col("rate_type") == F.lit(kind))
                & F.col("rate_date").isin(sorted(on))
                for (src, dst, kind), on in sorted(days.items())
            ]
            rows: list[dict[str, Any]] = (
                df.where(functools.reduce(operator.or_, conditions)).select(*COLUMNS).toArrow().to_pylist()
            )
            return version, rows

        version, rows = self._run(work)
        return latest(rows, wanted, version)


class ReferenceRates:
    """In-memory rates for the app's tests. Every ``load`` is a new table version, as in Delta."""

    def __init__(self, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.rows: list[Mapping[str, Any]] = list(rows)
        self.version = 0
        self.calls: list[frozenset[RateKey]] = []

    def load(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.rows += list(rows)
        self.version += 1

    def rates(self, keys: Iterable[RateKey]) -> dict[RateKey, Rate]:
        wanted = check_keys(keys)
        self.calls.append(wanted)
        return latest(self.rows, wanted, self.version)
