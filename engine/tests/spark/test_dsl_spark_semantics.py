"""The DSL's Python semantics against real Spark (Java regex, ANSI mode).

Every pattern the regex subset accepts must extract and match exactly as Java does, on inputs chosen to
hit the known differences (line terminators, non-ASCII digits and letters, case), and the Spark
behaviours the interpreter copies (trim, substring, split_part, two-digit years, decimal to string) are
checked here rather than assumed. Stage 3's compiler differential test builds on the same corpus.
"""

from __future__ import annotations

from datetime import date

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DecimalType, StringType, StructField, StructType
from regexcorpus import ACCEPTED, INPUTS

from close_contracts.dsl.interpret import _regex_extract
from close_contracts.dsl.regex import validate

pytestmark = pytest.mark.spark


def test_every_accepted_pattern_behaves_the_same_in_java_and_python(spark: SparkSession) -> None:
    df = spark.createDataFrame([(i, s) for i, s in enumerate(INPUTS)], "i int, s string")
    columns, expected_by_column = [], []
    for p, pattern in enumerate(ACCEPTED):
        safe = validate(pattern)
        compiled = safe.compile()
        columns.append(F.col("s").rlike(pattern).alias(f"m{p}"))
        expected_by_column.append((f"m{p}", [compiled.search(s) is not None for s in INPUTS]))
        for g in range(safe.groups + 1):
            columns.append(F.regexp_extract("s", pattern, g).alias(f"x{p}_{g}"))
            expected_by_column.append((f"x{p}_{g}", [_regex_extract(s, pattern, g) for s in INPUTS]))
    rows = sorted(df.select("i", *columns).collect(), key=lambda r: r["i"])
    mismatches = [
        (name, INPUTS[i], row[name], want[i])
        for name, want in expected_by_column
        for i, row in enumerate(rows)
        if row[name] != want[i]
    ]
    assert not mismatches, mismatches[:10]


def test_spark_behaviours_the_interpreter_copies(spark: SparkSession) -> None:
    row = (
        spark.range(1)
        .select(
            F.trim(F.lit("\ta b\t ")).alias("trim_tabs"),
            F.trim(F.lit("  a b  ")).alias("trim_spaces"),
            F.substring(F.lit("abcdef"), 0, 2).alias("sub_zero"),
            F.substring(F.lit("abcdef"), -2, 5).alias("sub_negative"),
            F.regexp_extract(F.lit("no match"), r"TX(\d{8})XT", 1).alias("no_match"),
            F.split_part(F.lit("a,b"), F.lit(","), F.lit(5)).alias("split_past_end"),
            F.to_date(F.lit("990131"), "yyMMdd").alias("two_digit_year"),
            F.lit("1.5").cast(DecimalType(38, 12)).cast("string").alias("decimal_string"),
            F.concat(F.lit("a"), F.lit(None).cast("string")).alias("concat_null"),
            F.lit(True).cast("string").alias("bool_string"),
        )
        .collect()[0]
    )
    assert row["trim_tabs"] == "\ta b\t"  # spaces only
    assert row["trim_spaces"] == "a b"
    assert row["sub_zero"] == "ab"
    assert row["sub_negative"] == "ef"
    assert row["no_match"] == ""
    assert row["split_past_end"] == ""
    assert row["two_digit_year"] == date(2099, 1, 31)
    assert row["decimal_string"] == "1.500000000000"
    assert row["concat_null"] is None
    assert row["bool_string"] == "true"


def test_the_interpreter_trim_matches_spark_on_mixed_whitespace(spark: SparkSession) -> None:
    from close_contracts.dsl import DslType, evaluate, parse, typecheck

    samples = [" a ", "\ta\t", " \t a \t ", "a", "  ", "\u00a0a\u00a0"]
    schema = StructType([StructField("s", StringType())])
    rows = spark.createDataFrame([(s,) for s in samples], schema).select(F.trim("s").alias("t")).collect()
    ir = typecheck(parse({"fn": "trim", "args": [{"col": "s"}]}), {"s": DslType.STRING})
    assert [r["t"] for r in rows] == [evaluate(ir, {"s": s}) for s in samples]
