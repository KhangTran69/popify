"""Managed identity tests use isolated data; the existing demo remains unchanged."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.config import settings
from app.db import Base, get_db
from app.identity import manage_account, session_account, managed_login
from app.models import Audit, OperatorAccount, OperatorSession, utcnow

PASSWORD = "synthetic-test-password"


@pytest.fixture
def identity_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    def get_session():
        with sessions() as db:
            yield db
    monkeypatch.setattr(settings, "managed_identity", True)
    monkeypatch.setattr(settings, "preview_mode", False)
    monkeypatch.setitem(main.app.dependency_overrides, get_db, get_session)
    with sessions() as db:
        for role in ("admin", "support", "reviewer", "auditor"):
            manage_account(db, "test-" + role, "create", role=role, password=PASSWORD)
    yield sessions
    engine.dispose()


def csrf(client):
    return re.search(r'name="csrf_token" value="([^"]+)', client.get("/login").text).group(1)


def login(client, role):
    response = client.post("/login", data={"username": "test-" + role,
        "password": PASSWORD, "csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    return csrf(client)


@pytest.mark.parametrize("role,path,data", [
    ("auditor", "/support", {"message": "test", "request_key": "no"}),
    ("support", "/approvals/1/decision", {"decision": "approved"}),
    ("reviewer", "/support", {"message": "test", "request_key": "no"}),
    ("auditor", "/deliveries/1/retry", {}),
    ("support", "/demo/start", {}),
    ("auditor", "/approvals/1/reconcile", {"outcome": "still_unknown", "explanation": "test"}),
])
def test_roles_denied_before_mutation(identity_db, role, path, data):
    client = TestClient(main.app, follow_redirects=False)
    token = login(client, role)
    assert client.post(path, data={**data, "csrf_token": token}).status_code == 403


def test_support_attribution_and_logout_replay(identity_db):
    client = TestClient(main.app, follow_redirects=False)
    token = login(client, "support")
    cookie = client.cookies.get("session")
    result = client.post("/support", data={"message": "Synthetic inquiry", "request_key": "attribution",
                                          "csrf_token": token})
    assert result.status_code == 303
    with identity_db() as db:
        event = db.scalar(select(Audit).where(Audit.event == "inquiry_queued"))
        assert event.actor == "test-support"
        account = db.scalar(select(OperatorAccount).where(OperatorAccount.username == "test-support"))
        assert PASSWORD not in account.password_hash
    assert client.post("/logout", data={"csrf_token": token}).status_code == 303
    replay = TestClient(main.app, follow_redirects=False)
    replay.cookies.set("session", cookie)
    assert replay.get("/support").status_code == 401


def test_account_change_revokes_and_expired_session_is_denied(identity_db):
    client = TestClient(main.app, follow_redirects=False)
    login(client, "reviewer")
    with identity_db() as db:
        manage_account(db, "test-reviewer", "role", role="auditor")
    assert client.get("/").status_code == 401
    login(client, "reviewer")
    with identity_db() as db:
        for session in db.scalars(select(OperatorSession)):
            session.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.get("/").status_code == 401


def test_shared_login_budget_and_revocation(identity_db):
    with identity_db() as db:
        for _ in range(5):
            account, token, wait = managed_login(db, "test-admin", "wrong", "synthetic-address")
            assert not account and not token and not wait
    with identity_db() as db:
        assert managed_login(db, "test-admin", PASSWORD, "another-address")[2] > 0
        account, token, wait = managed_login(db, "test-support", PASSWORD, "another-address")
        assert account and token and not wait
        assert session_account(db, token).username == "test-support"
        manage_account(db, "test-support", "disable")
        assert session_account(db, token) is None


def test_authenticated_operations_and_role_ui(identity_db):
    client = TestClient(main.app, follow_redirects=False)
    login(client, "auditor")
    response = client.get("/api/ops/status")
    assert response.status_code == 200
    assert response.json()["queues"]["orders"]["queued"] == 0
    assert all(not worker["recent_poll"] for worker in response.json()["workers"])
    page = client.get("/")
    assert page.status_code == 200
    assert 'action="/api/demo/order"' not in page.text
    assert 'href="/support"' not in page.text
    assert client.get("/ready").status_code == 200
    with identity_db() as db:
        manage_account(db, "test-admin", "disable")
    assert client.get("/ready").status_code == 503


def test_admin_account_controls_and_audit(identity_db):
    auditor = TestClient(main.app, follow_redirects=False)
    auditor_token = login(auditor, "auditor")
    assert auditor.get("/operators").status_code == 403
    assert auditor.post("/operators/test-support", data={"action": "revoke", "csrf_token": auditor_token}).status_code == 403
    admin = TestClient(main.app, follow_redirects=False)
    token = login(admin, "admin")
    page = admin.get("/operators")
    assert page.status_code == 200 and "test-support" in page.text
    assert "pbkdf2_sha256" not in page.text
    support = TestClient(main.app, follow_redirects=False)
    login(support, "support")
    assert admin.post("/operators/test-support", data={"action": "role", "role": "reviewer",
        "csrf_token": "invalid"}).status_code == 403
    assert admin.post("/operators/test-support", data={"action": "role", "role": "reviewer",
        "csrf_token": token}).status_code == 303
    assert support.get("/").status_code == 401
    assert admin.post("/operators/test-admin", data={"action": "disable", "csrf_token": token}).status_code == 409
    with identity_db() as db:
        account = db.scalar(select(OperatorAccount).where(OperatorAccount.username == "test-support"))
        assert account.role == "reviewer"
        event = db.scalar(select(Audit).where(Audit.entity_type == "operator",
                    Audit.entity_id == account.id, Audit.event == "operator_account_role"))
        assert event.actor == "test-admin"


def test_reauthentication_revokes_presented_cookie(identity_db):
    client = TestClient(main.app, follow_redirects=False)
    login(client, "support")
    old = client.cookies.get("session")
    login(client, "reviewer")
    replay = TestClient(main.app, follow_redirects=False)
    replay.cookies.set("session", old)
    assert replay.get("/").status_code == 401
    assert client.get("/operations").status_code == 200


def test_admin_mutation_rechecks_revoked_session(identity_db):
    with identity_db() as db:
        _, token, _ = managed_login(db, "test-admin", PASSWORD, "synthetic-admin")
        manage_account(db, "test-admin", "revoke")
        with pytest.raises(PermissionError):
            manage_account(db, "test-support", "disable", actor="test-admin",
                           protect_last_admin=True, administrator_token=token)
        db.rollback()
        account = db.scalar(select(OperatorAccount).where(OperatorAccount.username == "test-support"))
        assert account.active


def test_managed_cross_site_form_denied_even_with_valid_token(identity_db):
    from app.models import Case
    client = TestClient(main.app, follow_redirects=False)
    token = login(client, "support")
    response = client.post("/support", headers={"Sec-Fetch-Site": "cross-site"},
                           data={"csrf_token": token, "message": "Synthetic",
                                 "request_key": "cross-site"})
    assert response.status_code == 403
    with identity_db() as db:
        assert db.scalar(select(Case).where(Case.request_key == "cross-site")) is None


def test_session_lookup_refreshes_role_and_revocation(identity_db):
    with identity_db() as db:
        _, token, wait = managed_login(db, "test-reviewer", PASSWORD, "synthetic-freshness")
        assert not wait and session_account(db, token).role == "reviewer"
        with identity_db() as other:
            manage_account(other, "test-reviewer", "role", role="auditor")
        assert session_account(db, token) is None


def test_configurable_session_lifetime_and_cap(identity_db, monkeypatch):
    from app.identity import _digest
    monkeypatch.setattr(settings, "operator_session_minutes", 30)
    monkeypatch.setattr(settings, "operator_max_sessions", 2)
    tokens = []
    with identity_db() as db:
        for _ in range(3):
            _, token, wait = managed_login(db, "test-support", PASSWORD, "synthetic-policy")
            assert token and not wait
            tokens.append(token)
        assert session_account(db, tokens[0]) is None
        for token in tokens[1:]:
            assert session_account(db, token)
            expiry = db.get(OperatorSession, _digest(token)).expires_at
            assert 29 * 60 <= (expiry.replace(tzinfo=utcnow().tzinfo) - utcnow()).total_seconds() <= 30 * 60
        assert len(list(db.scalars(select(OperatorSession)))) == 2
    monkeypatch.setattr(settings, "operator_max_sessions", 1)
    with identity_db() as db:
        _, latest, wait = managed_login(db, "test-support", PASSWORD, "synthetic-policy")
        assert latest and not wait and session_account(db, latest)
        assert all(session_account(db, old) is None for old in tokens)
        assert len(list(db.scalars(select(OperatorSession)))) == 1


@pytest.mark.parametrize("headers", [
    [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
    [("Origin", "null")],
    [("Origin", "https://one.example.test"), ("Origin", "https://two.example.test")],
    [("Sec-Fetch-Site", "unknown")],
])
def test_ambiguous_cross_origin_headers_denied(identity_db, headers):
    from app.models import Case
    client = TestClient(main.app, follow_redirects=False)
    token = login(client, "support")
    response = client.post("/support", headers=headers,
        data={"csrf_token": token, "message": "Synthetic", "request_key": "ambiguous-origin"})
    assert response.status_code == 403
    with identity_db() as db:
        assert db.scalar(select(Case).where(Case.request_key == "ambiguous-origin")) is None
