"""The journaling stand-in on a local lake (Gate 8a).

- **It equals the engine, through Delta.** The generator's ``tiny`` rows (seed 11) are written to a lake and the job
  journals US01. Its Delta lines and rollups equal the reference engine run on the same rows in memory, so
  decimals, dates, maps and nulls survive the round trip.
- **Reruns.** A rerun writes nothing. Changed inputs, a changed snapshot and a disagreeing entity set are refused
  before anything is written.
- **Periods** come from the snapshot: an event dated in a closed, locked or (from Stage 8b) soft-closed period lands
  in the first open one.
- **Crashes.** A run that dies between its line and rollup commits is completed by the retry, each line written once.
- **The backstop.** Rows already in the ledger that this run wouldn't write as they are are refused: from another
  configuration or more landing data without a pin (an earlier run failed), or from a period closed since.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from close_contracts.canonical_json import content_sha256
from close_contracts.config.journal_workflow import Entity, EntitySet
from close_contracts.resolver import PathTableResolver
from close_contracts.samples.journal_workflow import SAMPLE_ENTITIES
from close_contracts.samples.journaling import (
    SAMPLE_CHART,
    SAMPLE_DEFINITIONS,
    SAMPLE_DIMENSIONS,
    SAMPLE_EVENT_TYPES,
    SAMPLE_MAPPINGS,
    SAMPLE_POLICY,
)
from close_contracts.table_schemas import REGISTRY
from close_contracts.tables import Tables
from close_datagen.generate import generate
from close_datagen.job import run as generate_lake
from close_datagen.profiles import PROFILES
from close_datagen.world import CALENDARS
from close_datagen.writer import write
from close_engine.io.fence import Fence
from close_engine.io.tables import read_table
from close_engine.jobs import RESULT_PREFIX, journal_reference
from close_engine.reference.journal.ledger import rollup
from close_engine.reference.journal.runner import journal, reference_from, sample_config
from close_engine.runctx import RunContext

pytestmark = pytest.mark.spark
GENERATOR_RUN = "6" * 32
ENTITY = "US01"


def _ctx(root: Path) -> RunContext:
    return RunContext(
        run_id=GENERATOR_RUN,
        attempt=1,
        lease_resource="datagen:lake",
        fencing_token=1,
        requested_at=datetime(2026, 9, 28, tzinfo=UTC),
        root_uri=root.as_uri(),
        request_id=None,
    )


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("journal-lake")
    outcome = generate_lake(_ctx(root), "tiny", 11)
    assert outcome["ok"], outcome
    return root


def snapshot(periods: dict[str, str] | None = None, entities: EntitySet = SAMPLE_ENTITIES) -> dict[str, Any]:
    """The run's snapshot as the app writes it: active versions by kind, the entity set, and the periods."""

    def versions(models: list[Any], ident: str) -> list[dict[str, Any]]:
        return [{"config_id": getattr(m, ident), "version": 1, "spec": m.model_dump(mode="json")} for m in models]

    return {
        "configuration": {
            "accounting_policy": [{"config_id": "accounting_policy", "version": 1,
                                   "spec": SAMPLE_POLICY.model_dump(mode="json")}],
            "chart_of_accounts": versions([SAMPLE_CHART], "chart_id"),
            "dimension_set": versions([SAMPLE_DIMENSIONS], "dimension_set_id"),
            "account_mapping": versions(list(SAMPLE_MAPPINGS), "mapping_id"),
            "event_type": versions(list(SAMPLE_EVENT_TYPES), "type_id"),
            "auto_journal_definition": versions(list(SAMPLE_DEFINITIONS), "definition_id"),
            "entity_set": versions([entities], "entity_set_id"),
        },
        "periods": periods or {},
        "entity_id": ENTITY,
    }  # fmt: skip


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    content: dict[str, Any],
    *,
    run: str,
    token: int,
    pinned: str = "",
    claimed: str | None = None,
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(journal_reference, "read_snapshot", lambda _h: content)
    code = journal_reference.main(
        [
            "--run-id", run, "--attempt", "1", "--lease-resource", f"ledger:{ENTITY}", "--fencing-token", str(token),
            "--requested-at", "2026-09-30T09:00:00+00:00", "--root-uri", root.as_uri(), "--entity-id", ENTITY,
            "--snapshot-hash", claimed or content_sha256(content), "--pinned-inputs", pinned,
        ]
    )  # fmt: skip
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert len(lines) == 1
    result: dict[str, Any] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return code, result


def _ledger(spark: SparkSession, root: Path, table: Any) -> list[dict[str, Any]]:
    df = read_table(spark, PathTableResolver(lake_root=root.as_uri()), table).where(F.col("entity_id") == ENTITY)
    rows: list[dict[str, Any]] = df.toArrow().to_pylist()
    return rows


def test_the_stand_in_writes_what_the_engine_books_in_memory(
    spark: SparkSession, generated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result = _run(monkeypatch, capsys, generated, snapshot(), run="a" * 32, token=1)
    assert code == 0, result
    assert "refused" not in result, result
    rows = generate(11, PROFILES["tiny"], GENERATOR_RUN).rows
    expected = journal(rows, sample_config(), calendars=CALENDARS, ref=reference_from(rows))
    lines = [ln for ln in expected.result.lines if ln.entity_id == ENTITY]
    rollups, links = rollup(lines)
    assert (result["lines"], result["lines_written"], result["rollups"], result["rollups_written"]) == (
        len(lines),
        len(lines),
        len(rollups),
        len(rollups),
    )
    written = {r["line_id"]: r for r in _ledger(spark, generated, Tables.LEDGER_SUBLEDGER_LINES)}
    assert set(written) == {ln.line_id for ln in lines}
    for ln in lines:
        got = written[ln.line_id]
        assert (
            got["account"], got["side"], got["amount_minor"], got["currency"], got["functional_amount_minor"],
            got["functional_currency"], got["period"], got["accounting_date"], got["fx_rate"], got["fx_rate_date"],
            got["fx_rate_version"], dict(got["dimensions"]), got["description"], got["is_suspense"],
            got["intended_account"], got["is_prior_period_adjustment"], got["original_period"], got["rollup_line_id"],
        ) == (
            ln.account, ln.side, ln.amount_minor, ln.currency, ln.functional_amount_minor, ln.functional_currency,
            ln.period, ln.accounting_date, ln.fx_rate, ln.fx_rate_date, ln.fx_rate_version, dict(ln.dimensions),
            ln.description, ln.is_suspense, ln.intended_account, ln.is_prior_period_adjustment, ln.original_period,
            links[ln.line_id],
        ), ln.line_id  # fmt: skip
    summed = {r["rollup_line_id"]: r for r in _ledger(spark, generated, Tables.LEDGER_GL_ROLLUP_LINES)}
    assert {
        k: (r["debit_minor"], r["credit_minor"], r["functional_debit_minor"], r["functional_credit_minor"],
            r["line_count"])
        for k, r in summed.items()
    } == {
        r.rollup_line_id: (r.debit_minor, r.credit_minor, r.functional_debit_minor, r.functional_credit_minor,
                           r.line_count)
        for r in rollups
    }  # fmt: skip
    by_rollup: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # the rollups sum to their lines, in Delta too
    for got in written.values():
        by_rollup[got["rollup_line_id"]][0 if got["side"] == "DR" else 1] += got["functional_amount_minor"]
    assert {k: tuple(v) for k, v in by_rollup.items()} == {
        k: (r["functional_debit_minor"], r["functional_credit_minor"]) for k, r in summed.items()
    }


def test_a_rerun_writes_nothing_and_anything_changed_is_refused(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert generate_lake(_ctx(tmp_path), "tiny", 11)["ok"]
    _, first = _run(monkeypatch, capsys, tmp_path, snapshot(), run="b" * 32, token=1)
    pinned = first["inputs_digest"]
    code, again = _run(monkeypatch, capsys, tmp_path, snapshot(), run="c" * 32, token=2, pinned=pinned)
    assert (code, again["lines_written"], again["rollups_written"], again["inputs_digest"]) == (0, 0, 0, pinned)
    # A changed snapshot and a disagreeing entity set are refused before anything is read or written.
    _, tampered = _run(monkeypatch, capsys, tmp_path, snapshot(), run="d" * 32, token=3, claimed="0" * 64)
    assert "doesn't hash" in tampered["refused"]
    euro = EntitySet(
        entity_set_id="sample_entities", entities=(Entity(entity_id="US01", name="US", functional_currency="EUR"),)
    )
    _, disagreeing = _run(monkeypatch, capsys, tmp_path, snapshot(entities=euro), run="e" * 32, token=4)
    assert "disagree on functional currencies" in disagreeing["refused"]
    # More landing data after the ledger was written: the pinned inputs no longer match.
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    orders = read_table(spark, resolver, next(t for t in REGISTRY if t.key == "landing.orders"))
    extra = orders.limit(1).toArrow().to_pylist()[0] | {"order_id": "ZZ-LATE-ARRIVAL"}
    orders_schema = next(s for t, s in REGISTRY.items() if t.key == "landing.orders")
    write(spark, resolver, orders_schema, [extra], metadata="a late arrival", append=True)
    lines_before = len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES))
    code, changed = _run(monkeypatch, capsys, tmp_path, snapshot(), run="f" * 32, token=5, pinned=pinned)
    assert (code, "inputs changed" in changed["refused"]) == (0, True)
    assert len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES)) == lines_before  # nothing written


def test_an_event_in_a_closed_period_lands_in_the_first_open_one(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert generate_lake(_ctx(tmp_path), "tiny", 11)["ok"]
    statuses = {"2026-07": "soft_closed", "2026-08": "locked"}  # a soft close takes no automated entries (8b)
    assert {"soft_closed", "closed", "locked"} == journal_reference.CLOSED
    code, result = _run(monkeypatch, capsys, tmp_path, snapshot(statuses), run="9" * 32, token=1)
    assert code == 0, result
    lines = _ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES)
    assert lines
    assert not [ln for ln in lines if ln["period"] in ("2026-07", "2026-08")]
    moved = [ln for ln in lines if ln["original_period"] in ("2026-07", "2026-08")]
    assert moved
    assert all(ln["is_prior_period_adjustment"] for ln in moved)
    assert {ln["period"] for ln in moved} == {"2026-09"}
    assert result["prior_period_adjustments"] == len(moved)


def test_a_run_that_dies_between_lines_and_rollups_is_completed_by_the_retry(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert generate_lake(_ctx(tmp_path), "tiny", 11)["ok"]
    real = journal_reference.insert_missing

    def dies_before_the_rollups(spark_: SparkSession, resolver_: Any, schema: Any, *a: Any, **kw: Any) -> None:
        if schema.table == Tables.LEDGER_GL_ROLLUP_LINES:
            raise RuntimeError("the driver died")
        real(spark_, resolver_, schema, *a, **kw)

    monkeypatch.setattr(journal_reference, "insert_missing", dies_before_the_rollups)
    with pytest.raises(RuntimeError, match="the driver died"):
        _run(monkeypatch, capsys, tmp_path, snapshot(), run="7" * 32, token=1)
    lines = len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES))
    assert lines > 0
    assert _ledger(spark, tmp_path, Tables.LEDGER_GL_ROLLUP_LINES) == []
    monkeypatch.setattr(journal_reference, "insert_missing", real)
    code, retry = _run(monkeypatch, capsys, tmp_path, snapshot(), run="8" * 32, token=2)  # no pin: it never succeeded
    assert (code, retry["lines_written"], retry["rollups_written"]) == (0, 0, retry["rollups"])
    assert len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES)) == lines  # written once


def test_rows_an_unpinned_run_would_not_write_as_they_are_are_refused(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backstop for a ledger an earlier run wrote without pinning it (it failed, or isn't settled yet)."""
    assert generate_lake(_ctx(tmp_path), "tiny", 11)["ok"]
    code, first = _run(monkeypatch, capsys, tmp_path, snapshot(), run="1" * 32, token=1)
    assert (code, "refused" in first) == (0, False)
    lines = len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES))
    # Another configuration: the commission line books to another account under the same line ids.
    changed = snapshot()
    for version in changed["configuration"]["auto_journal_definition"]:
        for line in version["spec"]["lines"] if version["config_id"] == "sample_revenue_at_trigger" else []:
            if line["line_id"] == "commission":
                line["account"]["account"] = "4030"
    _, other = _run(monkeypatch, capsys, tmp_path, changed, run="2" * 32, token=2)
    assert "rows this run wouldn't write as they are" in other["refused"], other
    # July closed since: the pin matches, but the engine would now book July's events in August under the same ids.
    july = snapshot({"2026-07": "closed"})
    _, closed = _run(monkeypatch, capsys, tmp_path, july, run="4" * 32, token=3, pinned=first["inputs_digest"])
    assert "rows this run wouldn't write as they are" in closed["refused"], closed
    # More landing data: one more US01 order adds to rollups already written.
    resolver = PathTableResolver(lake_root=tmp_path.as_uri())
    orders_schema = next(s for t, s in REGISTRY.items() if t.key == "landing.orders")
    orders = read_table(spark, resolver, orders_schema.table).where(F.col("entity_id") == ENTITY).orderBy("order_id")
    write(spark, resolver, orders_schema, [orders.limit(1).toArrow().to_pylist()[0] | {"order_id": "ZZ-MORE"}],
          metadata="one more order", append=True)  # fmt: skip
    _, grown = _run(monkeypatch, capsys, tmp_path, snapshot(), run="3" * 32, token=4)
    assert "rows this run wouldn't write as they are" in grown["refused"], grown
    assert len(_ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES)) == lines  # neither wrote anything


def test_a_writer_fenced_between_its_commits_writes_no_rollups(
    spark: SparkSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert generate_lake(_ctx(tmp_path), "tiny", 11)["ok"]
    newer = RunContext(
        run_id="5" * 32,
        attempt=2,
        lease_resource=f"ledger:{ENTITY}",
        fencing_token=2,
        requested_at=datetime(2026, 9, 30, 10, tzinfo=UTC),
        root_uri=tmp_path.as_uri(),
        request_id=None,
    )

    def a_retry_claims_after_the_lines(name: str, *_: Any, **__: Any) -> bool:
        if name == "after_lines_commit":
            Fence(spark, PathTableResolver(lake_root=tmp_path.as_uri()), newer, job="journal_ref").claim()
        return False

    monkeypatch.setattr(journal_reference, "pause_point", a_retry_claims_after_the_lines)
    code, result = _run(monkeypatch, capsys, tmp_path, snapshot(), run="0" * 32, token=1)
    assert (code, result["fenced"], result["fenced_stage"], result["highest_token"]) == (
        journal_reference.EXIT_FENCED,
        True,
        "before_rollups",
        2,
    )
    assert _ledger(spark, tmp_path, Tables.LEDGER_SUBLEDGER_LINES)  # committed before the newer claim
    assert _ledger(spark, tmp_path, Tables.LEDGER_GL_ROLLUP_LINES) == []
