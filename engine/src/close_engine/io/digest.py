"""Order-independent content digests for DataFrames and Delta tables.

Delta files are never byte-identical between runs (file names, row-group layout and commit metadata
differ), so the determinism contract is stated over *content*: a digest that depends only on the
multiset of rows, never on partitioning, file layout or row order.

Algorithm:
  1. each row is serialized as canonical JSON of its columns in sorted name order (nulls included,
     map entries sorted by key) and hashed with SHA-256;
  2. row hashes are grouped into 256 buckets by their first byte, sorted within each bucket, and
     each bucket is hashed;
  3. the final digest is SHA-256 over ``bucket:bucket_hash;`` for the buckets in bucket order.

At most 256 rows are collected to the driver, whatever the table size.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import MapType


def _normalized(df: DataFrame, name: str) -> Column:
    col = F.col(f"`{name}`")
    if isinstance(df.schema[name].dataType, MapType):
        return F.map_from_entries(F.array_sort(F.map_entries(col))).alias(name)
    return col.alias(name)


def row_hash_column(df: DataFrame, exclude: Iterable[str] = ()) -> Column:
    """SHA-256 of the canonical JSON of each row (columns in sorted order, nulls kept)."""
    skip = set(exclude)
    names = sorted(c for c in df.columns if c not in skip)
    if not names:
        raise ValueError("cannot digest a DataFrame with no columns")
    as_json = F.to_json(F.struct(*[_normalized(df, n) for n in names]), {"ignoreNullFields": "false"})
    return F.sha2(as_json, 256)


def dataframe_digest(df: DataFrame, exclude: Iterable[str] = ()) -> str:
    """Hex SHA-256 content digest of ``df`` that is independent of row order and partitioning."""
    hashed = df.select(row_hash_column(df, exclude).alias("h"))
    buckets = (
        hashed.withColumn("b", F.conv(F.substring("h", 1, 2), 16, 10).cast("int"))
        .groupBy("b")
        .agg(F.sha2(F.concat_ws("", F.array_sort(F.collect_list("h"))), 256).alias("bh"), F.count("*").alias("n"))
        .orderBy("b")
        .collect()
    )
    digest = hashlib.sha256()
    for row in buckets:
        digest.update(f"{row['b']}:{row['n']}:{row['bh']};".encode())
    return digest.hexdigest()
