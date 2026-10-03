"""The narrow calendar lookup on real local Delta tables (Gate 8b): the entity's calendar code from ``ref.entities``,
that calendar's days in the range from ``ref.calendars`` at one pinned version, and nothing for an entity without a
code or a lake without the tables. The same rows through the reference lookup give the same answer."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from pyspark.sql import SparkSession
from pyspark.sql.types import BooleanType, DateType, IntegerType, StringType, StructField, StructType

from close_contracts.resolver import PathTableResolver
from close_contracts.tables import Tables
from close_platform.query.calendars import DataFrameCalendars, ReferenceCalendars

pytestmark = pytest.mark.spark
ENTITIES = StructType([StructField("entity_id", StringType(), False), StructField("calendar_code", StringType(), True)])
CALENDARS = StructType(
    [
        StructField("calendar_code", StringType(), False),
        StructField("day", DateType(), False),
        StructField("is_business_day", BooleanType(), False),
        StructField("bd_index", IntegerType(), False),
    ]
)
LABOR_DAY = date(2026, 9, 7)


def _days(code: str, first: date, last: date) -> list[tuple[str, date, bool, int]]:
    out, day, index = [], first, 0
    while day <= last:
        business = day.weekday() < 5 and not (code == "US_FED" and day == LABOR_DAY)
        index += business
        out.append((code, day, business, index))
        day += timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    from close_engine.session import LocalSparkOptions, build_local_session

    active = SparkSession.getActiveSession()
    return active or build_local_session(
        LocalSparkOptions(app_name="close-calendar-tests", master="local[2]", driver_memory="1g", shuffle_partitions=2)
    )


def test_an_entity_gets_its_calendar_days_at_one_pinned_version(spark: SparkSession, tmp_path: Path) -> None:
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    entities = [("US01", "US_FED"), ("EU01", "TARGET2"), ("XX01", None)]
    spark.createDataFrame(entities, ENTITIES).write.format("delta").save(resolver.location(Tables.REF_ENTITIES))
    path = resolver.location(Tables.REF_CALENDARS)
    rows = _days("US_FED", date(2026, 8, 1), date(2026, 9, 30)) + _days("TARGET2", date(2026, 8, 1), date(2026, 9, 30))
    spark.createDataFrame(rows[:10], CALENDARS).write.format("delta").save(path)
    spark.createDataFrame(rows[10:], CALENDARS).write.format("delta").mode("append").save(path)  # table version 1
    lookup = DataFrameCalendars(lambda work: work(spark), resolver)
    found = lookup.calendar("US01", date(2026, 9, 1), date(2026, 9, 10))
    assert found is not None
    assert (found.calendar_code, found.version, len(found.days)) == ("US_FED", 1, 10)
    assert (found.days[date(2026, 9, 4)], found.days[date(2026, 9, 5)], found.days[LABOR_DAY]) == (True, False, False)
    eu = lookup.calendar("EU01", date(2026, 9, 1), date(2026, 9, 10))
    assert eu is not None
    assert eu.days[LABOR_DAY]  # TARGET2 works on Labor Day
    assert lookup.calendar("XX01", date(2026, 9, 1), date(2026, 9, 10)) is None  # no calendar code
    assert lookup.calendar("UK01", date(2026, 9, 1), date(2026, 9, 10)) is None  # no row
    beyond = lookup.calendar("US01", date(2026, 9, 25), date(2026, 10, 5))
    assert beyond is not None
    assert max(beyond.days) == date(2026, 9, 30)  # the lake's days end there
    reference = ReferenceCalendars(
        {"US01": "US_FED", "EU01": "TARGET2"},
        {code: {d: b for c, d, b, _ in rows if c == code} for code in ("US_FED", "TARGET2")},
    )
    assert reference.calendar("US01", date(2026, 9, 1), date(2026, 9, 10)).days == found.days  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="not an entity id"):
        lookup.calendar("us01", date(2026, 9, 1), date(2026, 9, 10))
    with pytest.raises(ValueError, match="at most 400 days"):
        lookup.calendar("US01", date(2026, 1, 1), date(2027, 6, 1))


def test_no_tables_means_no_calendar(spark: SparkSession, tmp_path: Path) -> None:
    lookup = DataFrameCalendars(lambda work: work(spark), PathTableResolver(lake_root=tmp_path.as_uri()))
    assert lookup.calendar("US01", date(2026, 9, 1), date(2026, 9, 10)) is None
