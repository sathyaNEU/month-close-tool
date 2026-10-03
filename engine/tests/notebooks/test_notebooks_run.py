"""Every Databricks notebook in engine/notebooks runs locally as a script (thin-wrapper contract)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pyspark.sql import SparkSession
from testsupport.widget_stub import NOTEBOOK_HEADER, run_notebook

from close_engine.io.migrations import load
from close_engine.jobs import RESULT_PREFIX

pytestmark = pytest.mark.spark

NOTEBOOK_DIR = Path(__file__).resolve().parents[2] / "notebooks"


def _smoke_widgets(tmp: Path) -> dict[str, str]:
    return {"run_id": "nb-smoke-1", "rows": "500", "root_uri": tmp.as_uri()}


def _smoke_result(result: dict[str, Any]) -> None:
    assert result["rows"] == 500
    assert len(result["digest"]) == 64


def _ddl_widgets(tmp: Path) -> dict[str, str]:
    return {"run_id": "nb-ddl-1", "root_uri": tmp.as_uri()}


def _ddl_result(result: dict[str, Any]) -> None:
    assert result["ok"] is True
    assert result["applied"] == list(range(1, len(load()) + 1))


# Widget values and result checks per notebook. A new notebook without an entry here fails the suite on purpose.
WIDGETS: dict[str, Callable[[Path], dict[str, str]]] = {"smoke.py": _smoke_widgets, "ddl_apply.py": _ddl_widgets}
CHECKS: dict[str, Callable[[dict[str, Any]], None]] = {"smoke.py": _smoke_result, "ddl_apply.py": _ddl_result}


def test_every_notebook_has_widget_values() -> None:
    notebooks = sorted(p.name for p in NOTEBOOK_DIR.glob("*.py"))
    assert notebooks, "no notebooks found"
    assert sorted(WIDGETS) == notebooks
    assert sorted(CHECKS) == notebooks


@pytest.mark.parametrize("name", sorted(WIDGETS))
def test_notebook_runs_as_script(
    name: str, spark: SparkSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = NOTEBOOK_DIR / name
    assert path.read_text(encoding="utf-8").startswith(NOTEBOOK_HEADER)
    run_notebook(path, WIDGETS[name](tmp_path), spark)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert len(lines) == 1
    CHECKS[name](json.loads(lines[0][len(RESULT_PREFIX) :]))


def test_undeclared_widget_read_fails(spark: SparkSession, tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(NOTEBOOK_HEADER + "\nvalue = dbutils.widgets.get('missing')\n", encoding="utf-8")
    with pytest.raises(KeyError, match="before being declared"):
        run_notebook(bad, {}, spark)
