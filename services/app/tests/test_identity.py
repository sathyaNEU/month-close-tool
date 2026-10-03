"""Identity from the proxy headers: deny anything a well-behaved proxy would never send (Gate 1a)."""

from __future__ import annotations

import pytest
from apptestkit import who
from fastapi.testclient import TestClient

pytestmark = pytest.mark.pg


def test_no_identity_headers_is_401_problem_json(client: TestClient) -> None:
    r = client.get("/api/v1/whoami", headers={"X-Request-Id": "req-1"})
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["title"] == "Not authenticated"
    assert body["request_id"] == "req-1"
    assert body["instance"] == "/api/v1/whoami"


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("X-Forwarded-User", "1001")], id="missing-email"),
        pytest.param([("X-Forwarded-Email", "alice@example.com")], id="missing-user"),
        pytest.param(
            [("X-Forwarded-User", "1001"), ("X-Forwarded-User", "1002"), ("X-Forwarded-Email", "a@example.com")],
            id="duplicate-user",
        ),
        pytest.param([("X-Forwarded-User", "1001, 1002"), ("X-Forwarded-Email", "a@example.com")], id="comma-joined"),
        pytest.param([("X-Forwarded-User", "1001;1002"), ("X-Forwarded-Email", "a@example.com")], id="semicolon"),
        pytest.param([("X-Forwarded-User", ""), ("X-Forwarded-Email", "a@example.com")], id="empty"),
        pytest.param(
            [("X-Forwarded-User", "1001"), ("X-Forwarded-Email", "a@example.com, b@example.com")], id="email-list"
        ),
        pytest.param(
            [
                ("X-Forwarded-User", "1001"),
                ("X-Forwarded-Email", "a@example.com"),
                ("X-Forwarded-Preferred-Username", "a"),
                ("X-Forwarded-Preferred-Username", "b"),
            ],
            id="duplicate-name",
        ),
    ],
)
def test_malformed_identity_is_rejected(client: TestClient, headers: list[tuple[str, str]]) -> None:
    r = client.get("/api/v1/whoami", headers=headers)
    assert r.status_code == 401, r.text


def test_whoami_returns_the_entities_granted_to_the_stable_user_id(client: TestClient) -> None:
    alice = client.get("/api/v1/whoami", headers=who("alice")).json()
    assert alice["user_id"] == "1001"
    assert alice["entities"] == ["US01"]
    assert alice["display_name"] == "alice"
    assert client.get("/api/v1/whoami", headers=who("dave")).json()["entities"] == ["EU01", "US01"]
    assert client.get("/api/v1/whoami", headers=who("carol")).json()["entities"] == []  # deny by default


def test_entities_follow_the_user_id_not_the_email(client: TestClient) -> None:
    spoof = who("carol") | {"X-Forwarded-Email": "alice@example.com"}
    assert client.get("/api/v1/whoami", headers=spoof).json()["entities"] == []


def test_request_id_is_echoed_only_when_well_formed(client: TestClient) -> None:
    ok = client.get("/api/v1/whoami", headers=who("alice", **{"X-Request-Id": "abc-123"})).json()
    assert ok["request_id"] == "abc-123"
    bad = client.get("/api/v1/whoami", headers=who("alice", **{"X-Request-Id": "abc 123\\n"})).json()
    assert bad["request_id"] is None


def test_health_needs_no_identity(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
