"""Unexpected failures: a 500 problem that keeps the request id and leaks nothing (Gate 1a)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from close_app.context import AppContext
from close_app.logs import RequestIdFilter
from close_app.main import create_app

pytestmark = pytest.mark.pg


def test_unhandled_errors_are_500_problems_with_the_request_id(
    ctx: AppContext, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(context=ctx)

    def boom() -> None:
        raise RuntimeError("secret internals")

    app.add_api_route("/boom", boom)
    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level(logging.INFO), TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/boom", headers={"X-Request-Id": "req-500"})
    assert r.status_code == 500
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["request_id"] == "req-500"
    assert "secret" not in r.text
    error = next(rec for rec in caplog.records if rec.getMessage() == "unhandled error")
    assert error.request_id == "req-500"  # type: ignore[attr-defined]
    assert error.exc_info is not None
    access = next(rec for rec in caplog.records if rec.getMessage() == "request")
    assert access.status == 500  # type: ignore[attr-defined]
    assert access.request_id == "req-500"  # type: ignore[attr-defined]
