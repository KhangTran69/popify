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
