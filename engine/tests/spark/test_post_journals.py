"""The posting job (Gate 7b) on a local lake: each line is written once however often a run is retried, the ERP
feed follows the ledger and a rerun finishes it, a stale writer is fenced before it writes (and, from Stage 8a,
before its feed commit), and the ledger tables hold the columns the read path serves."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from close_contracts.queryable import JOURNAL_LINES, JOURNAL_POSTINGS, QueryableTable
from close_contracts.resolver import PathTableResolver
from close_contracts.table_schemas import TableSchema
from close_contracts.tables import Tables
from close_engine.io.fence import Fence
from close_engine.io.migrations import load, tables_in
from close_engine.io.sql import run_ddl
from close_engine.io.tables import delta_table, read_table
from close_engine.jobs import RESULT_PREFIX, post_journals
from close_engine.runctx import RunContext

pytestmark = pytest.mark.spark
ENTITY = "US01"


def _lake(spark: SparkSession, root: Path) -> PathTableResolver:
    """The ledger tables as V007 created them and V008 evolved them (Stage 7c's rate columns)."""
    resolver = PathTableResolver(lake_root=root.as_uri())
    for migration in (m for m in load() if m.name in ("journal_posting", "journal_fx")):
        for statement in migration.statements:
            run_ddl(spark, statement, resolver, **tables_in(statement))
    return resolver


def _journal(number: int, amount: int) -> list[dict[str, Any]]:
    """Two lines of one approved journal, as the posting job reads them from ``posting_queue``."""
    common = {
        "journal_id": uuid.UUID(int=number),
        "entity_id": ENTITY,
        "number": number,
        "revision": 1,
        "content_hash": f"{number:064x}",
        "template_id": "sample_standard_journal",
        "period": "2026-09",
        "accounting_date": date(2026, 9, 10),
        "currency": "USD",
        "journal_description": f"Carrier accrual {number}",
        "source": "manual",
        "preparer": "1001",
        "approved_at": datetime(2026, 9, 29, 12, tzinfo=UTC),
        "approvals": "1:1002",
    }
    return [
        {**common, "line_no": 1, "account": "5020", "side": "DR", "amount_minor": amount,
         "dimensions": {"channel": "web"}, "line_description": "Freight expense"},
        {**common, "line_no": 2, "account": "2410", "side": "CR", "amount_minor": amount,
         "dimensions": None, "line_description": "Accrued freight"},
    ]  # fmt: skip


def _ids(*numbers: int) -> list[str]:
    return sorted(str(uuid.UUID(int=n)) for n in numbers)


def _post(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    queue: list[dict[str, Any]],
    *,
    run: str,
    token: int,
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(
        post_journals, "read_queue", lambda _run, entity: [q for q in queue if q["entity_id"] == entity]
    )
    code = post_journals.main(
        [
            "--run-id", run, "--attempt", "1", "--lease-resource", f"ledger:{ENTITY}", "--fencing-token", str(token),
            "--requested-at", "2026-09-30T09:00:00+00:00", "--root-uri", root.as_uri(), "--entity-id", ENTITY,
        ]
    )  # fmt: skip
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert len(lines) == 1
    result: dict[str, Any] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return code, result


def _rows(spark: SparkSession, resolver: PathTableResolver, table: Any) -> list[dict[str, Any]]:
    return [r.asDict() for r in read_table(spark, resolver, table).collect()]


def test_each_line_is_written_once_however_often_it_is_posted(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = _lake(spark, tmp_path)
    queue = _journal(1, 12_345) + _journal(2, 700)
    code, first = _post(monkeypatch, capsys, tmp_path, queue, run="a" * 32, token=1)
    assert code == 0
    assert (first["written"], first["already_present"], first["lines"], first["fenced"]) == (_ids(1, 2), [], 4, False)
    meta = json.loads(delta_table(spark, resolver, Tables.LEDGER_JOURNAL_LINES).history(1).collect()[0]["userMetadata"])
    assert (meta["run_id"], meta["fencing_token"]) == ("a" * 32, 1)
    # A later run over the same journals plus a new one writes only the new one.
    code, second = _post(monkeypatch, capsys, tmp_path, [*queue, *_journal(3, 50)], run="b" * 32, token=2)
    assert code == 0
    assert (second["written"], second["already_present"], second["lines"]) == (_ids(3), _ids(1, 2), 6)
    lines = _rows(spark, resolver, Tables.LEDGER_JOURNAL_LINES)
    assert len(lines) == len({(ln["journal_id"], ln["line_no"]) for ln in lines}) == 6
    assert sum(ln["debit_minor"] for ln in lines) == sum(ln["credit_minor"] for ln in lines) == 12_345 + 700 + 50
    assert {ln["run_id"] for ln in lines if ln["journal_id"] == _ids(1)[0]} == {"a" * 32}  # never rewritten
    assert next(ln for ln in lines if ln["account"] == "2410")["dimensions"] == {}
    feed = _rows(spark, resolver, Tables.LEDGER_JOURNAL_POSTINGS)
    assert sorted(f["journal_id"] for f in feed) == _ids(1, 2, 3)
    payload = json.loads(next(f["payload"] for f in feed if f["journal_id"] == _ids(1)[0]))
    assert [(ln["account"], ln["side"], ln["amount_minor"]) for ln in payload["lines"]] == [
        ("5020", "DR", 12_345),
        ("2410", "CR", 12_345),
    ]
    assert payload["content_hash"] == f"{1:064x}"
    for table in (JOURNAL_LINES, JOURNAL_POSTINGS):  # what the read path serves is there, typed as it expects
        _serves(spark, resolver, table)


def _serves(spark: SparkSession, resolver: PathTableResolver, table: QueryableTable) -> None:
    frame = read_table(spark, resolver, table.table)
    actual = {f.name: f.dataType.simpleString() for f in frame.schema.fields}
    assert [(c.name, actual.get(c.name)) for c in table.columns] == [(c.name, c.type) for c in table.columns]
    assert table.entity_column in actual
    assert frame.count() > 0
    assert frame.groupBy(*table.unique_key).count().where(F.col("count") > 1).count() == 0


def test_a_crash_between_the_two_commits_is_finished_by_the_rerun(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = _lake(spark, tmp_path)
    real = post_journals.insert_missing

    def dies_before_the_feed(spark_: SparkSession, resolver_: Any, schema: TableSchema, *a: Any, **kw: Any) -> None:
        if schema.table == Tables.LEDGER_JOURNAL_POSTINGS:
            raise RuntimeError("the driver died")
        real(spark_, resolver_, schema, *a, **kw)

    monkeypatch.setattr(post_journals, "insert_missing", dies_before_the_feed)
    with pytest.raises(RuntimeError, match="the driver died"):
        _post(monkeypatch, capsys, tmp_path, _journal(1, 900), run="c" * 32, token=1)
    assert (
        len(_rows(spark, resolver, Tables.LEDGER_JOURNAL_LINES)),
        _rows(spark, resolver, Tables.LEDGER_JOURNAL_POSTINGS),
    ) == (2, [])
    monkeypatch.setattr(post_journals, "insert_missing", real)
    code, retry = _post(monkeypatch, capsys, tmp_path, _journal(1, 900), run="d" * 32, token=2)
    assert (code, retry["written"], retry["already_present"]) == (0, [], _ids(1))
    assert len(_rows(spark, resolver, Tables.LEDGER_JOURNAL_LINES)) == 2
    assert [f["run_id"] for f in _rows(spark, resolver, Tables.LEDGER_JOURNAL_POSTINGS)] == ["d" * 32]


def test_a_stale_writer_is_fenced_before_it_writes(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = _lake(spark, tmp_path)
    assert _post(monkeypatch, capsys, tmp_path, _journal(1, 100), run="e" * 32, token=5)[0] == 0
    code, stale = _post(monkeypatch, capsys, tmp_path, _journal(2, 200), run="f" * 32, token=4)
    assert (code, stale["fenced"], stale["fenced_stage"], stale["highest_token"]) == (
        post_journals.EXIT_FENCED,
        True,
        "before_commit",
        5,
    )
    written = read_table(spark, resolver, Tables.LEDGER_JOURNAL_LINES).select("journal_id").distinct().collect()
    assert [r["journal_id"] for r in written] == _ids(1)


def test_a_writer_fenced_between_its_commits_writes_no_feed(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = _lake(spark, tmp_path)
    newer = RunContext(
        run_id="9" * 32,
        attempt=2,
        lease_resource=f"ledger:{ENTITY}",
        fencing_token=2,
        requested_at=datetime(2026, 9, 30, 10, tzinfo=UTC),
        root_uri=tmp_path.as_uri(),
        request_id=None,
    )

    def a_retry_claims_after_the_lines(name: str, *_: Any, **__: Any) -> bool:
        if name == "after_lines_commit":
            Fence(spark, resolver, newer, job="post_journals").claim()
        return False

    monkeypatch.setattr(post_journals, "pause_point", a_retry_claims_after_the_lines)
    code, result = _post(monkeypatch, capsys, tmp_path, _journal(1, 100), run="8" * 32, token=1)
    assert (code, result["fenced"], result["fenced_stage"], result["highest_token"]) == (
        post_journals.EXIT_FENCED,
        True,
        "before_feed",
        2,
    )
    assert len(_rows(spark, resolver, Tables.LEDGER_JOURNAL_LINES)) == 2  # committed before the newer claim
    assert _rows(spark, resolver, Tables.LEDGER_JOURNAL_POSTINGS) == []


def _euro_journal(number: int) -> list[dict[str, Any]]:
    """A EUR journal in US01 as the Stage 7c view gives it: each line with its functional amount and rate, and the
    rounding line as one more line."""
    rate = {
        "currency": "EUR",
        "functional_currency": "USD",
        "fx_rate": Decimal("1.084213000000"),
        "fx_rate_date": date(2026, 9, 10),
        "fx_rate_type": "daily_spot",
        "fx_rate_version": 1,
    }
    lines = [
        (1, "5020", "DR", 1_001, 1_085, "entry"),
        (2, "5020", "DR", 1_001, 1_085, "entry"),
        (3, "2410", "CR", 2_002, 2_171, "entry"),
        (4, "7010", "DR", 0, 1, "fx_rounding"),
    ]
    base = _journal(number, 1)[0]
    return [
        {**base, **rate, "line_no": n, "account": a, "side": side, "amount_minor": m, "functional_amount_minor": f,
         "line_kind": kind, "dimensions": None, "line_description": "FX rounding" if kind == "fx_rounding" else "Line"}
        for n, a, side, m, f, kind in lines
    ]  # fmt: skip


def test_a_foreign_currency_journal_posts_functional_amounts_its_rate_and_a_rounding_line(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = _lake(spark, tmp_path)
    code, result = _post(monkeypatch, capsys, tmp_path, _euro_journal(7), run="7" * 32, token=1)
    assert (code, result["lines"]) == (0, 4)
    lines = sorted(_rows(spark, resolver, Tables.LEDGER_JOURNAL_LINES), key=lambda ln: ln["line_no"])
    assert [
        (ln["line_kind"], ln["amount_minor"], ln["currency"], ln["functional_amount_minor"], ln["debit_minor"],
         ln["credit_minor"], ln["fx_rate"], ln["fx_rate_version"])
        for ln in lines
    ] == [
        ("entry", 1_001, "EUR", 1_085, 1_085, 0, Decimal("1.084213000000"), 1),
        ("entry", 1_001, "EUR", 1_085, 1_085, 0, Decimal("1.084213000000"), 1),
        ("entry", 2_002, "EUR", 2_171, 0, 2_171, Decimal("1.084213000000"), 1),
        ("fx_rounding", 0, "EUR", 1, 1, 0, Decimal("1.084213000000"), 1),
    ]  # fmt: skip
    assert sum(ln["debit_minor"] for ln in lines) == sum(ln["credit_minor"] for ln in lines) == 2_171
    feed = _rows(spark, resolver, Tables.LEDGER_JOURNAL_POSTINGS)[0]
    payload = json.loads(feed["payload"])
    assert (payload["currency"], payload["functional_currency"], payload["rate"]["rate"]) == (
        "EUR",
        "USD",
        "1.084213000000",
    )
    assert [ln["kind"] for ln in payload["lines"]] == ["entry", "entry", "entry", "fx_rounding"]
    for table in (JOURNAL_LINES, JOURNAL_POSTINGS):  # the evolved table still serves the read path
        _serves(spark, resolver, table)
