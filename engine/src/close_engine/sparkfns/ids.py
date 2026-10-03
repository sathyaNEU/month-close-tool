"""Spark implementation of :func:`close_contracts.ids.hash_id`.

It builds exactly the payload the Python function hashes (namespace, unit separator, length-prefixed
parts, ``-1:`` for NULL), so an ID computed in a Spark job equals the one computed in the API, the
reference matcher, or a test. ``tests/spark/test_ids_spark.py`` checks every shared test vector.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import BooleanType, ByteType, DataType, DateType, IntegerType, LongType, ShortType, StringType

from close_contracts.ids import ID_HEX_LEN, NULL_ENCODING, US, validate_namespace

ALLOWED_ID_TYPES: tuple[type[DataType], ...] = (
    StringType,
    LongType,
    IntegerType,
    ShortType,
    ByteType,
    DateType,
    BooleanType,
)


def _encode(part: Column) -> Column:
    as_text = part.cast(StringType())
    return F.when(as_text.isNull(), F.lit(NULL_ENCODING)).otherwise(
        F.concat(F.length(as_text).cast(StringType()), F.lit(":"), as_text)
    )


def hash_id(ns: str, *parts: Column | str) -> Column:
    """128-bit deterministic ID column: ``H(ns, *parts)`` (parts may be Columns or column names)."""
    validate_namespace(ns)
    cols = [F.col(p) if isinstance(p, str) else p for p in parts]
    prefix = F.lit(ns + US)
    payload = F.concat(prefix, F.concat_ws(US, *[_encode(c) for c in cols])) if cols else prefix
    return F.substring(F.sha2(payload, 256), 1, ID_HEX_LEN)


def check_id_columns(df: DataFrame, *names: str) -> None:
    """Raise TypeError if any named column has a type whose string form differs from Python's."""
    fields = {f.name: f.dataType for f in df.schema.fields}
    for name in names:
        if name not in fields:
            raise KeyError(f"column {name!r} not found")
        if not isinstance(fields[name], ALLOWED_ID_TYPES):
            raise TypeError(
                f"column {name!r} has type {fields[name].simpleString()}, which is not allowed in hash IDs "
                "(allowed: string, integral, date, boolean)"
            )
