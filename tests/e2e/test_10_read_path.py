"""The read path end to end (Gate 1b): results, rows and totals come from the lake over Spark Connect,
agree with each other, stay scoped to each user's entities, and Spark Connect cannot write."""

from __future__ import annotations

import httpx
import pytest
from stacklib import BASE, User, compose

pytestmark = pytest.mark.stack

WRITE_ATTEMPT = """
from pyspark.sql import SparkSession
s = SparkSession.builder.remote("sc://spark-connect:15002").create()
try:
    s.range(3).write.format("delta").mode("overwrite").save("s3a://close-lake/local/ops/e2e_must_not_exist")
    print("WRITE-SUCCEEDED")
except Exception as exc:
    print("WRITE-REFUSED", "403" in str(exc) or "AccessDenied" in str(exc) or "Forbidden" in str(exc))
"""


def _all_rows(user: User, run_id: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    params: dict[str, str | int] = {"page_size": 500}
    while True:
        body = user.http.get(f"/api/v1/smoke/runs/{run_id}/rows", params=params).json()
        rows.extend(body["rows"])
        if body["next_cursor"] is None:
            return rows
        params = {"page_size": 500, "cursor": body["next_cursor"]}


def test_a_write_through_spark_connect_is_refused_with_403() -> None:
    out = compose("exec", "-T", "app", "python", "-c", WRITE_ATTEMPT).stdout
    assert "WRITE-REFUSED True" in out, out


def test_readiness_holds_spark_connect_to_the_pinned_runtime() -> None:
    checks = httpx.get(f"{BASE}/readyz", timeout=30).json()["checks"]
    assert checks["spark_connect"] == {"ok": True, "detail": "Spark 4.1.3, pinned settings in effect"}


def test_manifest_totals_and_paged_rows_agree_and_stay_scoped() -> None:
    dave, alice, carol = User("dave"), User("alice"), User("carol")
    run_id = dave.start(rows=3000).json()["run_id"]
    run = dave.wait(run_id, timeout=180)
    assert run["state"] == "succeeded"
    for user, entities in ((dave, {"US01", "EU01"}), (alice, {"US01"})):
        manifest = user.get(run_id)["result"]
        totals = user.http.get(f"/api/v1/smoke/runs/{run_id}/totals").json()["groups"]
        rows = _all_rows(user, run_id)
        assert set(manifest["entities"]) == entities
        assert {g["entity_id"] for g in totals} == entities
        assert {r["entity_id"] for r in rows} == entities
        assert manifest["rows"] == sum(g["rows"] for g in totals) == len(rows)
        assert len({r["row_id"] for r in rows}) == len(rows)  # every row once
        amounts = [int(str(r["amount_minor"])) for r in rows]
        assert amounts == sorted(amounts, reverse=True)
    assert carol.get(run_id)["result"]["entities"] == []
    assert carol.http.get(f"/api/v1/smoke/runs/{run_id}/totals").json()["groups"] == []
    assert _all_rows(carol, run_id) == []


def test_a_tampered_or_borrowed_cursor_is_rejected() -> None:
    dave, bob = User("dave"), User("bob")
    run_id = dave.start(rows=1000).json()["run_id"]
    assert dave.wait(run_id, timeout=180)["state"] == "succeeded"
    cursor = dave.http.get(f"/api/v1/smoke/runs/{run_id}/rows", params={"page_size": 10}).json()["next_cursor"]
    assert cursor
    borrowed = bob.http.get(f"/api/v1/smoke/runs/{run_id}/rows", params={"page_size": 10, "cursor": cursor})
    assert borrowed.status_code == 400
    tampered = dave.http.get(
        f"/api/v1/smoke/runs/{run_id}/rows", params={"page_size": 10, "cursor": cursor[:-4] + "AAAA"}
    )
    assert tampered.status_code == 400
