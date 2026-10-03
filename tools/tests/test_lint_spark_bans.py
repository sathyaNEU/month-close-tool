from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

from tools.lint_spark_bans import lint_source

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]


def test_seeded_violations_are_all_caught() -> None:
    source = (FIXTURES / "bad_spark.py").read_text(encoding="utf-8")
    found = lint_source(source, "bad_spark.py")
    counts = Counter(v.rule for v in found)
    assert counts == Counter(
        {
            "SB001": 3,
            "SB002": 1,
            "SB003": 2,
            "SB004": 1,
            "SB005": 1,
            "SB006": 3,
            "SB007": 2,
            "SB008": 1,
            "SB009": 2,
            "SB010": 1,
            "SB011": 2,
        }
    )
    expected_lines = {i + 1 for i, line in enumerate(source.splitlines()) if "# SB0" in line}
    assert {v.line for v in found} == expected_lines


def test_clean_code_and_pragmas_pass() -> None:
    source = (FIXTURES / "good_spark.py").read_text(encoding="utf-8")
    assert lint_source(source, "good_spark.py") == []


def test_spark_sql_is_allowed_only_in_the_ddl_gateway_and_never_with_args() -> None:
    gateway = "engine/src/close_engine/io/sql.py"
    assert lint_source("spark.sql(statement)\n", gateway) == []
    assert [v.rule for v in lint_source("spark.sql(q, args={'x': 1})\n", gateway)] == ["SB010"]
    assert [v.rule for v in lint_source("spark.sql(q, [1])\n", gateway)] == ["SB010"]
    assert [v.rule for v in lint_source("spark.sql(q)\n", "engine/src/close_engine/jobs/x.py")] == ["SB011"]


def test_pragma_only_allows_the_named_rule() -> None:
    source = "x = df.rand() / 2  # spark-ban: allow SB008 test\n"
    assert [v.rule for v in lint_source(source, "t.py")] == ["SB001"]


def test_cli_fails_on_violations_and_passes_on_engine_src() -> None:
    bad = subprocess.run(
        [sys.executable, str(REPO / "tools/lint_spark_bans.py"), str(FIXTURES / "bad_spark.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "SB001" in bad.stdout
    clean = subprocess.run(
        [sys.executable, str(REPO / "tools/lint_spark_bans.py")], capture_output=True, text=True, check=False
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
