"""Identity through devproxy: every forged spelling loses to the proxy's identity (Gate 1a)."""

from __future__ import annotations

import json
import socket
import subprocess
import uuid

import httpx
import pytest
from stacklib import BASE, User, compose

pytestmark = pytest.mark.stack

FORGED = [
    pytest.param({"X-Forwarded-User": "1004"}, id="plain"),
    pytest.param({"x-forwarded-user": "1004", "X-FORWARDED-EMAIL": "dave@example.com"}, id="case"),
    pytest.param({"X_Forwarded_User": "1004", "x_forwarded_email": "dave@example.com"}, id="underscore"),
    pytest.param({"X-Forwarded_User": "1004"}, id="mixed-separators"),
    pytest.param({"X-Forwarded-User": "1004, 1001"}, id="comma-joined"),
    pytest.param({"Forwarded": "for=10.0.0.1;user=1004"}, id="forwarded"),
    pytest.param({"X-Forwarded-Access-Token": "forged-token", "X-Real-Ip": "10.9.9.9"}, id="token-and-ip"),
    pytest.param({"X-Forwarded-Preferred-Username": "dave"}, id="display-name"),
]


def test_only_devproxy_is_published() -> None:
    out = compose("ps", "--format", "json").stdout
    services = [json.loads(line) for line in out.splitlines() if line.strip()]
    published = {
        s["Service"]: [(p["URL"], p["PublishedPort"]) for p in s.get("Publishers") or [] if p.get("PublishedPort")]
        for s in services
    }
    assert {name for name, ports in published.items() if ports} == {"devproxy"}
    assert {url for url, _ in published["devproxy"]} == {"127.0.0.1"}


def test_anonymous_requests_are_401_even_with_forged_identity() -> None:
    r = httpx.get(f"{BASE}/api/v1/whoami", headers={"X-Forwarded-User": "1004", "X-Forwarded-Email": "d@example.com"})
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("forged", FORGED)
def test_forged_headers_never_beat_the_proxy_identity(forged: dict[str, str]) -> None:
    me = User("alice").whoami(forged).json()
    assert (me["user_id"], me["email"], me["entities"]) == ("1001", "alice@example.com", ["US01"])


def test_duplicate_forged_headers_are_stripped() -> None:
    alice = User("alice")
    headers = [("X-Forwarded-User", "1004"), ("X-Forwarded-User", "1002")]
    me = alice.http.get("/api/v1/whoami", headers=headers).json()
    assert me["user_id"] == "1001"


def test_http_1_0_gets_the_proxy_identity() -> None:
    with socket.create_connection(("127.0.0.1", 8088), timeout=30) as sock:
        sock.sendall(
            b"GET /api/v1/whoami HTTP/1.0\r\nCookie: close_dev_user=bob\r\n"
            b"X-Forwarded-User: 1004\r\nx_forwarded_user: 1004\r\n\r\n"
        )
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200")
    me = json.loads(body)
    assert (me["user_id"], me["entities"]) == ("1002", ["EU01"])


def test_h2c_gets_the_proxy_identity() -> None:
    proc = subprocess.run(
        [
            "curl", "-s", "--http2-prior-knowledge", "-w", "\n%{http_version}",
            "-H", "Cookie: close_dev_user=carol", "-H", "x-forwarded-user: 1004",
            f"{BASE}/api/v1/whoami",
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    body, version = proc.stdout.rsplit("\n", 1)
    assert version == "2"
    me = json.loads(body)
    assert (me["user_id"], me["entities"]) == ("1003", [])


def test_the_proxy_sets_the_request_id() -> None:
    me = User("dave").whoami({"X-Request-Id": "forged-id"}).json()
    assert me["request_id"] != "forged-id"
    uuid.UUID(me["request_id"])


def test_unknown_dev_users_are_not_signed_in() -> None:
    client = httpx.Client(base_url=BASE, cookies={"close_dev_user": "mallory"})
    assert client.get("/__dev/login/mallory").status_code == 404
    assert client.get("/api/v1/whoami", headers={"X-Forwarded-User": "1001"}).status_code == 401
