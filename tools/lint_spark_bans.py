"""AST lint that bans Spark APIs which break the engine's determinism or portability contract.

Rules (see docs/plan/ROADMAP.md, "Engineering rules"):

  SB001  non-deterministic functions (monotonically_increasing_id, rand, uuid, now, current_*, ...)
  SB002  dropDuplicates / drop_duplicates (keeps an arbitrary row)
  SB003  order-dependent aggregates (first, last, max_by, min_by, any_value) — use an explicit total order
  SB004  collect_list / collect_set not wrapped directly in array_sort
  SB005  input_file_name (depends on file layout)
  SB006  RDD / JVM / caching APIs (.rdd, sparkContext, _jvm, _jsc, cache, persist, checkpoint)
         — they break on Spark Connect, serverless and standard access mode; use scratch tables
  SB007  floating-point types (DoubleType, FloatType, cast to double/float) — money is BIGINT minor units
  SB008  true division `/` — Spark returns DOUBLE for integer division; use `div` or DECIMAL math
  SB009  SQL strings built in engine code (F.expr, selectExpr, expr) — the rule compiler builds Columns
  SB010  spark.sql(..., args=...) — OSS Delta 4.1-4.4 drops SQL parameter markers (delta-io/delta#7640)
  SB011  spark.sql outside the SQL gateways (close_engine/io/sql.py for DDL, close_platform/query/history.py for
         DESCRIBE HISTORY) — use the DataFrame / DeltaTable API

A line can opt out with a justified pragma:  ``# spark-ban: allow SB008 <reason>``
``engine/src``, the generator (``datagen/src``, whose ``large`` profile runs on Databricks) and the read path
(``close_platform/query``) are linted by default; tests and fixtures are exempt.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = [
    REPO / "engine" / "src",
    REPO / "datagen" / "src",
    REPO / "libs" / "platform" / "src" / "close_platform" / "query",
]

NONDETERMINISTIC = {
    "monotonically_increasing_id",
    "rand",
    "randn",
    "random",
    "uuid",
    "shuffle",
    "now",
    "current_timestamp",
    "current_date",
    "localtimestamp",
    "current_timezone",
    "spark_partition_id",
}
ORDER_DEPENDENT_AGGS = {"first", "last", "first_value", "last_value", "max_by", "min_by", "any_value"}
COLLECTORS = {"collect_list", "collect_set", "array_agg"}
JVM_ATTRS = {"rdd", "sparkContext", "_jvm", "_jsc", "_jdf", "_sc"}
CACHING_CALLS = {"cache", "persist", "checkpoint", "localCheckpoint"}
FLOAT_TYPES = {"DoubleType", "FloatType"}
FLOAT_CAST_NAMES = {"double", "float", "real"}
SQL_STRING_CALLS = {"expr", "selectExpr"}
SQL_GATEWAY_MODULES = ("close_engine/io/sql.py", "close_platform/query/history.py")

PRAGMA_RE = re.compile(r"#\s*spark-ban:\s*allow\s+((?:SB\d{3}[\s,]*)+)")


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self._parents: dict[ast.AST, ast.AST] = {}

    def add(self, node: ast.AST, rule: str, message: str) -> None:
        self.violations.append(Violation(self.path, getattr(node, "lineno", 0), rule, message))

    def visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self._parents[child] = node
        super().visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name in NONDETERMINISTIC:
            self.add(node, "SB001", f"non-deterministic function `{name}`")
        elif name in {"dropDuplicates", "drop_duplicates"}:
            self.add(node, "SB002", f"`{name}` keeps an arbitrary row; rank with total_order() instead")
        elif name in ORDER_DEPENDENT_AGGS:
            self.add(node, "SB003", f"order-dependent aggregate `{name}`; use an explicit total order")
        elif name in COLLECTORS:
            parent = self._parents.get(node)
            wrapped = isinstance(parent, ast.Call) and _call_name(parent) == "array_sort"
            if not wrapped:
                self.add(node, "SB004", f"`{name}` must be wrapped directly in array_sort(...)")
        elif name == "input_file_name":
            self.add(node, "SB005", "`input_file_name` depends on physical file layout")
        elif name in CACHING_CALLS and isinstance(node.func, ast.Attribute):
            self.add(
                node, "SB006", f"`.{name}()` is not portable (Spark Connect / serverless); materialize a scratch table"
            )
        elif name in FLOAT_TYPES:
            self.add(node, "SB007", f"`{name}` — money and ranks must not be floating point")
        elif name == "cast" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.lower() in FLOAT_CAST_NAMES:
                self.add(node, "SB007", f"cast to `{arg.value}` — use DECIMAL or BIGINT")
        elif name in SQL_STRING_CALLS:
            self.add(node, "SB009", f"`{name}` builds SQL from strings; build Columns instead")
        if name == "sql" and isinstance(node.func, ast.Attribute):
            if len(node.args) > 1 or any(kw.arg == "args" for kw in node.keywords):
                self.add(node, "SB010", "SQL parameter binding is broken with OSS Delta (delta-io/delta#7640)")
            if not self.path.replace("\\", "/").endswith(SQL_GATEWAY_MODULES):
                gateways = ", ".join(SQL_GATEWAY_MODULES)
                self.add(node, "SB011", f"`spark.sql` is only allowed in the SQL gateways {gateways}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in JVM_ATTRS:
            self.add(node, "SB006", f"`.{node.attr}` is not available on Spark Connect / serverless")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            self.add(node, "SB008", "true division `/` yields DOUBLE in Spark; use `div` or DECIMAL arithmetic")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.op, ast.Div):
            self.add(node, "SB008", "true division `/=`; use `div` or DECIMAL arithmetic")
        self.generic_visit(node)


def _allowed_rules(source_lines: list[str], line: int) -> set[str]:
    if 1 <= line <= len(source_lines):
        m = PRAGMA_RE.search(source_lines[line - 1])
        if m:
            return set(re.findall(r"SB\d{3}", m.group(1)))
    return set()


def lint_source(source: str, path: str) -> list[Violation]:
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path)
    visitor.visit(tree)
    lines = source.splitlines()
    return sorted(v for v in visitor.violations if v.rule not in _allowed_rules(lines, v.line))


def iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            yield from sorted(f for f in p.rglob("*.py") if "tests" not in f.parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ban non-deterministic / non-portable Spark APIs.")
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories (default: engine/src)")
    args = parser.parse_args(argv)
    paths = args.paths or DEFAULT_PATHS
    violations: list[Violation] = []
    files = list(iter_python_files(paths))
    for f in files:
        rel = str(f.resolve().relative_to(REPO)) if f.resolve().is_relative_to(REPO) else str(f)
        violations.extend(lint_source(f.read_text(encoding="utf-8"), rel))
    for v in violations:
        print(v.render())
    print(f"spark-ban lint: {len(files)} files, {len(violations)} violation(s)", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
