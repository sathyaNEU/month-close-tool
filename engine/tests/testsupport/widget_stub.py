"""Test-only stand-in for the Databricks ``dbutils`` object, just enough to run thin notebooks.

Databricks source-format notebooks are plain ``.py`` files. The runtime injects ``dbutils`` and
``spark`` as globals; locally we inject this stub and a real SparkSession, then execute the file with
``runpy``. The stub supports only widgets, because thin notebooks may use nothing else.
"""

from __future__ import annotations

import runpy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pyspark.sql import SparkSession

NOTEBOOK_HEADER = "# Databricks notebook source"


class _Widgets:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self._declared: dict[str, str] = {}

    def text(self, name: str, defaultValue: str = "", label: str | None = None) -> None:  # noqa: N803 (Databricks API)
        self._declared.setdefault(name, defaultValue)

    def dropdown(self, name: str, defaultValue: str, choices: list[str], label: str | None = None) -> None:  # noqa: N803
        self._declared.setdefault(name, defaultValue)

    def get(self, name: str) -> str:
        if name not in self._declared:
            raise KeyError(f"widget {name!r} was read before being declared (Databricks would fail too)")
        return self._values.get(name, self._declared[name])

    def removeAll(self) -> None:  # noqa: N802 (Databricks API)
        self._declared.clear()


class DbutilsStub:
    def __init__(self, widget_values: Mapping[str, str]) -> None:
        self.widgets = _Widgets(widget_values)


def run_notebook(path: Path, widget_values: Mapping[str, str], spark: SparkSession) -> dict[str, Any]:
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    if first_line.strip() != NOTEBOOK_HEADER:
        raise ValueError(f"{path} is not a Databricks source-format notebook")
    return runpy.run_path(
        str(path), init_globals={"dbutils": DbutilsStub(widget_values), "spark": spark}, run_name="__main__"
    )
