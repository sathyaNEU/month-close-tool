"""The Spark session settings every executor runs under, shared by the engine (which applies and
asserts them on every session it builds or reuses) and the app (whose readiness check holds a Spark
Connect server to them). Changing one changes results, so they live in one place."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

PINNED_SPARK_CONF: Final[Mapping[str, str]] = MappingProxyType(
    {
        # All timestamps are interpreted and rendered in UTC; accounting dates are derived explicitly.
        "spark.sql.session.timeZone": "UTC",
        # ANSI mode: overflow, invalid casts and division by zero raise instead of returning NULL.
        "spark.sql.ansi.enabled": "true",
        # Decimal arithmetic must be exact; results that do not fit raise instead of losing digits.
        "spark.sql.decimalOperations.allowPrecisionLoss": "false",
        # System-of-record schemas change only through numbered migrations, never implicitly.
        "spark.databricks.delta.schema.autoMerge.enabled": "false",
    }
)
