from __future__ import annotations

import pytest

from close_contracts.enums import JobStatus, Schema
from close_contracts.resolver import CatalogTableResolver, PathTableResolver, ResolverError, TableResolver
from close_contracts.tables import LogicalTable, Tables


def test_logical_table_key_and_validation() -> None:
    t = LogicalTable(Schema.MATCH, "items")
    assert t.key == "match.items"
    for bad in ["Items", "1items", "items-x", "", "a" * 64, "it`ems"]:
        with pytest.raises(ValueError, match="invalid table name"):
            LogicalTable(Schema.MATCH, bad)


def test_table_registry() -> None:
    assert Tables.OPS_RUN_MANIFESTS in Tables.all()
    assert len(set(Tables.all())) == len(Tables.all())


def test_path_resolver() -> None:
    r = PathTableResolver(lake_root="s3a://close-lake/local/", landing_tables={"orders": "s3a://landing/orders"})
    assert isinstance(r, TableResolver)
    assert r.location(Tables.OPS_SMOKE_RESULTS) == "s3a://close-lake/local/ops/smoke_results"
    assert r.table(Tables.OPS_SMOKE_RESULTS) == "delta.`s3a://close-lake/local/ops/smoke_results`"
    assert r.landing("orders") == "delta.`s3a://landing/orders`"
    assert r.evidence_uri("runs", "run-1", "config.json") == "s3a://close-lake/local/_evidence/runs/run-1/config.json"
    with pytest.raises(ResolverError, match="not configured"):
        r.landing("payouts")


@pytest.mark.parametrize(
    "root",
    ["close-lake/local", "http://x", "s3a://bucket/`drop", "s3a://bucket/a b", "s3a://bucket/x;DROP TABLE y"],
)
def test_path_resolver_rejects_unsafe_roots(root: str) -> None:
    with pytest.raises(ResolverError):
        PathTableResolver(lake_root=root)


@pytest.mark.parametrize("part", ["..", ".", "a/b", "a`b", "", "x y"])
def test_evidence_parts_are_validated(part: str) -> None:
    r = PathTableResolver(lake_root="file:/tmp/lake")
    with pytest.raises(ResolverError):
        r.evidence_uri(part)


def test_evidence_needs_parts() -> None:
    with pytest.raises(ResolverError):
        PathTableResolver(lake_root="file:/tmp/lake").evidence_uri()


def test_invalid_landing_mappings() -> None:
    with pytest.raises(ResolverError):
        PathTableResolver(lake_root="file:/tmp/lake", landing_tables={"Orders": "file:/tmp/x"})
    with pytest.raises(ResolverError):
        CatalogTableResolver(catalog="close_prod", evidence_root="s3://ev", landing_tables={"orders": "a.b"})


def test_catalog_resolver() -> None:
    r = CatalogTableResolver(
        catalog="close_prod",
        evidence_root="s3://close-evidence/prod",
        landing_tables={"orders": "landing_prod.shop.orders"},
    )
    assert r.table(Tables.OPS_RUN_MANIFESTS) == "`close_prod`.`ops`.`run_manifests`"
    assert r.landing("orders") == "`landing_prod`.`shop`.`orders`"
    assert r.evidence_uri("x") == "s3://close-evidence/prod/x"
    with pytest.raises(ResolverError):
        r.landing("missing")
    for bad in ["Close", "close-prod", "`x`"]:
        with pytest.raises(ResolverError):
            CatalogTableResolver(catalog=bad, evidence_root="s3://ev")


def test_job_status_terminal() -> None:
    assert {s for s in JobStatus if s.is_terminal} == {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
