"""Exercise production configuration and managed identity on disposable PostgreSQL.

The HTTPS TestClient models secure cookies, not a real TLS network connection.
Run only on an explicitly disposable database; each run owns a unique schema.
"""
import os
import re
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import main
from app.config import settings
from app.db import Base, engine, get_db
from app.identity import manage_account
from app.models import Approval, Audit, Case, ActionExecution

PASSWORD = "synthetic-production-test-password"


def token(client):
    return re.search(r'name="csrf_token" value="([^"]+)', client.get("/login").text).group(1)


def sign_in(client, username):
    response = client.post("/login", data={"username": username, "password": PASSWORD,
                                           "csrf_token": token(client)}, follow_redirects=False)
    assert response.status_code == 303, response.status_code
    assert "secure" in response.headers["set-cookie"].lower()
    assert "httponly" in response.headers["set-cookie"].lower()
    return token(client)


def run():
    if os.environ.get("POPIFY_CONFIRM_DISPOSABLE") != "1" or not settings.managed_identity or engine.dialect.name != "postgresql" or os.environ.get("POPIFY_ENV") != "production":
        raise RuntimeError("Requires explicit disposable PostgreSQL and production managed-identity configuration.")
    schema = "production_identity_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = create_engine(settings.database_url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = sessionmaker(isolated, expire_on_commit=False)
    def override():
        with sessions() as db:
            yield db
    main.app.dependency_overrides[get_db] = override
    try:
        Base.metadata.create_all(isolated)
        with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as admin:
            assert admin.get("/ready").status_code == 503
            with sessions() as db:
                for role in ("admin", "support", "reviewer", "auditor"):
                    manage_account(db, "ci-" + role, "create", role=role, password=PASSWORD)
            assert admin.get("/ready").status_code == 200
            admin_token = sign_in(admin, "ci-admin")
            operators = admin.get("/operators")
            assert operators.status_code == 200
            assert "script-src 'self'" in operators.headers["Content-Security-Policy"]
            assert "unsafe-inline" not in operators.headers["Content-Security-Policy"]
            assert admin.get("/docs").status_code == 404
            assert admin.get("/demo").status_code == 404
            assert admin.post("/api/demo/order", data={"csrf_token": admin_token}).status_code == 404
            support = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
            support_token = sign_in(support, "ci-support")
            response = support.post("/support", data={"csrf_token": support_token, "message": "Where is my synthetic order?", "request_key": "production-identity-test"})
            assert response.status_code == 303
            worker = admin.post("/api/n8n/triage-next", headers={"X-N8N-Secret": settings.n8n_webhook_secret})
            assert worker.status_code == 200 and worker.json()["status"] == "needs_review"
            with sessions() as db:
                case = db.scalar(select(Case).where(Case.request_key == "production-identity-test"))
                approval = db.scalar(select(Approval).where(Approval.subject_id == case.id, Approval.proposed_action == "send_reply"))
                approval_id = approval.id
                event = db.scalar(select(Audit).where(Audit.entity_type == "case", Audit.entity_id == case.id, Audit.event == "inquiry_queued"))
                assert event.actor == "ci-support"
            decision_url = f"/approvals/{approval_id}/decision"
            assert support.post(decision_url, data={"csrf_token": support_token, "decision": "approved"}).status_code == 403
            reviewer = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
            reviewer_token = sign_in(reviewer, "ci-reviewer")
            assert reviewer.post(decision_url, data={"csrf_token": reviewer_token, "decision": "approved"}).status_code == 303
            action_worker = admin.post("/api/n8n/delayed-exceptions", headers={"X-N8N-Secret": settings.n8n_webhook_secret})
            assert action_worker.status_code == 200
            with sessions() as db:
                approval = db.get(Approval, approval_id)
                execution = db.scalar(select(ActionExecution).where(ActionExecution.idempotency_key == f"approval:{approval_id}"))
                assert approval.actor == "ci-reviewer" and approval.status == "approved"
                assert execution and execution.state == "succeeded"
            cookie = reviewer.cookies.get("session")
            assert admin.post("/operators/ci-reviewer", data={"action": "revoke", "csrf_token": admin_token}).status_code == 303
            replay = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
            replay.cookies.set("session", cookie)
            assert replay.get("/").status_code == 401
        print("PASS: production configuration, secure cookie attributes, PostgreSQL-backed roles, attributable triage and approval, mock execution and session revocation")
    finally:
        main.app.dependency_overrides.pop(get_db, None)
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


if __name__ == "__main__":
    run()
