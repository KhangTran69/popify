"""Regression tests for rejected requests and safe failure responses."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import main
from app.config import settings
from app.db import get_db
from app.integrations import verify_shopify
from app.security import (
    FORM_API_BODY_LIMIT, WEBHOOK_BODY_LIMIT, RequestBodyLimitMiddleware,
    secure_equal, valid_integration_secret,
)


def test_unicode_comparisons_and_empty_integration_secret(monkeypatch):
    assert secure_equal("café", "café")
    assert not secure_equal("café", "cafe")
    assert not secure_equal("\ud800", "bad")
    assert not valid_integration_secret(None, "")
    assert not valid_integration_secret("", "")
    assert not valid_integration_secret("é", "secret")
    monkeypatch.setattr(settings, "shopify_webhook_secret", "test-secret")
    assert not verify_shopify(b"{}", "é")


@pytest.fixture
def boundary_client(monkeypatch):
    monkeypatch.setattr(settings, "preview_mode", False)
    monkeypatch.setattr(main, "_LOGIN_ATTEMPTS", {})
    # Rejected requests should never need database operations.
    def no_database():
        yield object()
    monkeypatch.setitem(main.app.dependency_overrides, get_db, no_database)
    return TestClient(main.app, follow_redirects=False)


def token(client):
    return re.search(r'name="csrf_token" value="([^"]+)', client.get("/login").text).group(1)


def test_unicode_login_and_csrf_are_controlled_errors(boundary_client):
    client = boundary_client
    assert client.post("/login", data={
        "username": "未知", "password": "秘密", "csrf_token": token(client),
    }).status_code == 401
    assert client.post("/login", data={
        "username": settings.operator_username, "password": settings.operator_password,
        "csrf_token": "é",
    }).status_code == 403
    assert client.post("/login", data={
        "username": settings.operator_username, "password": settings.operator_password,
        "csrf_token": token(client),
    }).status_code == 303


def test_empty_worker_secret_does_not_authenticate(boundary_client, monkeypatch):
    monkeypatch.setattr(settings, "n8n_webhook_secret", "")
    assert boundary_client.post("/api/n8n/process-next").status_code == 401
    assert boundary_client.post("/api/support", json={"message": "test"}).status_code == 401


def test_oversize_requests_fail_before_parsing(boundary_client):
    for path, limit in [
        ("/login", FORM_API_BODY_LIMIT),
        ("/api/support", FORM_API_BODY_LIMIT),
        ("/webhooks/shopify/orders", WEBHOOK_BODY_LIMIT),
    ]:
        response = boundary_client.post(path, content=b"x" * (limit + 1))
        assert response.status_code == 413
        assert response.headers["x-correlation-id"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path,limit", [
    ("/api/support", FORM_API_BODY_LIMIT),
    ("/webhooks/shopify/orders", WEBHOOK_BODY_LIMIT),
])
@pytest.mark.parametrize("overflow", [False, True])
def test_streamed_body_limit_and_exact_replay(path, limit, overflow):
    first = b"a" * (limit - 1)
    last = b"bc" if overflow else b"b"
    received = []
    responses = []
    messages = iter([
        {"type": "http.request", "body": first, "more_body": True},
        {"type": "http.request", "body": last, "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    async def downstream(scope, receive, send):
        received.append((await receive())["body"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {"type": "http", "method": "POST", "path": path, "headers": []}
    asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))
    if overflow:
        assert responses[0]["status"] == 413
        assert not received
    else:
        assert responses[0]["status"] == 204
        assert received == [first + last]


def test_database_failure_has_safe_response(monkeypatch, boundary_client):
    def unavailable():
        raise OperationalError("private SQL", {"secret": "customer data"}, Exception("private connection"))
    monkeypatch.setitem(main.app.dependency_overrides, get_db, unavailable)
    response = boundary_client.post("/api/n8n/process-next", headers={
        "X-N8N-Secret": settings.n8n_webhook_secret,
    })
    assert response.status_code == 503
    assert "customer data" not in response.text
    assert "private SQL" not in response.text
    assert response.headers["x-correlation-id"]


def test_stale_operator_session_is_rejected(boundary_client, monkeypatch):
    client = boundary_client
    assert client.post("/login", data={
        "username": settings.operator_username, "password": settings.operator_password,
        "csrf_token": token(client),
    }).status_code == 303
    monkeypatch.setattr(settings, "operator_username", "replacement-operator")
    assert client.get("/support").status_code == 401


def test_body_timeout_and_capacity_rejection(monkeypatch):
    import app.security as security
    responses = []
    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"x", "more_body": False}
    async def send(message):
        responses.append(message)
    async def downstream(scope, receive, send):
        raise AssertionError("Rejected request reached downstream")
    scope = {"type": "http", "method": "POST", "path": "/login", "headers": []}
    middleware = RequestBodyLimitMiddleware(downstream)
    monkeypatch.setattr(security, "BODY_TIMEOUT_SECONDS", 0.001)
    asyncio.run(middleware(scope, receive, send))
    assert responses[0]["status"] == 408
    responses.clear()
    for _ in range(security.MAX_CONCURRENT_MUTATIONS):
        assert middleware._slots.acquire(blocking=False)
    try:
        asyncio.run(middleware(scope, receive, send))
        assert responses[0]["status"] == 503
        assert (b"retry-after", b"1") in responses[0]["headers"]
    finally:
        for _ in range(security.MAX_CONCURRENT_MUTATIONS):
            middleware._slots.release()


def test_login_capacity_preserves_other_mutations():
    from app.security import MAX_CONCURRENT_LOGINS
    responses = []
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    async def send(message):
        responses.append(message)
    async def downstream(scope, receive, send):
        assert (await receive())["body"] == b"{}"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    middleware = RequestBodyLimitMiddleware(downstream)
    for _ in range(MAX_CONCURRENT_LOGINS):
        assert middleware._login_slots.acquire(blocking=False)
    try:
        scope = {"type": "http", "method": "POST", "path": "/login", "headers": []}
        asyncio.run(middleware(scope, receive, send))
        assert responses[0]["status"] == 503
        responses.clear()
        asyncio.run(middleware({**scope, "path": "/webhooks/shopify/orders"}, receive, send))
        assert responses[0]["status"] == 204
    finally:
        for _ in range(MAX_CONCURRENT_LOGINS):
            middleware._login_slots.release()


def test_invalid_support_text_rejected_before_database(boundary_client, monkeypatch):
    monkeypatch.setattr(settings, "n8n_webhook_secret", "synthetic-secret")
    response = boundary_client.post("/api/support", json={"message": "invalid\x00text"}, headers={"X-N8N-Secret": "synthetic-secret"})
    assert response.status_code == 422


def test_production_unexpected_failure_is_sanitized(boundary_client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "managed_identity", True)
    monkeypatch.setenv("POPIFY_ENV", "production")
    def broken():
        raise RuntimeError("private synthetic payload must never appear")
    monkeypatch.setitem(main.app.dependency_overrides, get_db, broken)
    response = boundary_client.get("/api/ops/status")
    assert response.status_code == 500
    assert response.headers["x-correlation-id"] and response.headers["cache-control"] == "no-store"
    assert "private synthetic payload" not in response.text
    assert "private synthetic payload" not in caplog.text
    assert "unexpected_request_failure" in caplog.text



@pytest.mark.parametrize("headers", [
    {"Origin": "https://sibling.example.test"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_managed_cross_origin_forms_fail_even_with_valid_token(boundary_client, monkeypatch, headers):
    monkeypatch.setattr(settings, "managed_identity", True)
    monkeypatch.setattr(settings, "operator_origin", "https://console.example.test")
    response = boundary_client.post("/login", headers=headers, data={
        "username": "synthetic-admin", "password": "synthetic-password", "csrf_token": token(boundary_client)})
    assert response.status_code == 403


def test_operator_origin_configuration():
    from app.config import Settings
    from pydantic import ValidationError
    assert Settings(_env_file=None, operator_origin="https://localhost:8444").operator_origin == "https://localhost:8444"
    assert Settings(_env_file=None, operator_origin="https://EXAMPLE.test:443").operator_origin == "https://example.test"
    for value in ("http://example.test", "https://example.test/path", "https://user@example.test",
                  "https://example.test:99999", "https://example.test?query=1"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, operator_origin=value)
