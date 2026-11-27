"""Exercise production configuration and managed identity on disposable PostgreSQL.

The HTTPS TestClient models secure cookies, not a real TLS network connection.
Run only on an explicitly disposable database; each run owns a unique schema.
"""
import os
import re
import uuid
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import main
from app.config import settings
from app.db import Base, engine, get_db
from app.identity import manage_account
from app.models import Approval, Audit, Case, ActionExecution, ExecutionReconciliation

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


def verify_recovery(sessions, worker, support, support_token, reviewer, reviewer_token):
    """Exercise actual HTTP contracts and PostgreSQL state, all provider outcomes synthetic."""
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    def queue_case(key):
        assert support.post("/support", data={"csrf_token": support_token, "message": "Synthetic order status inquiry", "request_key": key}).status_code == 303
        assert worker.post("/api/n8n/triage-next", headers=headers).json()["status"] == "needs_review"
        with sessions() as db:
            case = db.scalar(select(Case).where(Case.request_key == key))
            return db.scalar(select(Approval.id).where(Approval.subject_type == "case",
                Approval.subject_id == case.id, Approval.proposed_action == "send_reply"))
    def decide(approval_id):
        path = f"/approvals/{approval_id}/decision"
        assert reviewer.post(path, data={"decision": "approved", "csrf_token": reviewer_token}).status_code == 303
    def scan():
        assert worker.post("/api/n8n/delayed-exceptions", headers=headers).status_code == 200
    def execution(db, approval_id):
        return db.scalar(select(ActionExecution).where(ActionExecution.idempotency_key == f"approval:{approval_id}"))
    old_ambiguous, old_failures, old_delay = settings.mock_action_ambiguous, settings.mock_action_failures, settings.retry_base_seconds
    try:
        ambiguous = queue_case("production-uncertain")
        decide(ambiguous)
        settings.mock_action_ambiguous = True
        scan()
        scan()
        with sessions() as db:
            assert db.get(Approval, ambiguous).execution_status == "ambiguous"
            assert execution(db, ambiguous).attempts == 1
        retry_path = f"/approvals/{ambiguous}/retry"
        assert reviewer.post(retry_path, data={"csrf_token": reviewer_token}).status_code == 409
        reconcile_path = f"/approvals/{ambiguous}/reconcile"
        data = {"outcome": "still_unknown", "explanation": "Synthetic evidence remains inconclusive.", "reference": "", "csrf_token": reviewer_token}
        assert support.post(reconcile_path, data={**data, "csrf_token": support_token}).status_code == 403
        assert reviewer.post(reconcile_path, data=data).status_code == 303
        assert reviewer.post(reconcile_path, data=data).status_code == 303
        assert reviewer.post(retry_path, data={"csrf_token": reviewer_token}).status_code == 409
        data.update(outcome="confirmed_not_executed", explanation="Synthetic fixture confirms no external action occurred.", reference="CI-SYNTHETIC-EVIDENCE")
        assert reviewer.post(reconcile_path, data=data).status_code == 303
        assert reviewer.post(f"/approvals/{ambiguous}/refresh", data={"csrf_token": reviewer_token}).status_code == 303
        with sessions() as db:
            original = db.get(Approval, ambiguous)
            replacement = original.replacement_approval_id
            assert replacement and db.get(Approval, replacement).status == "pending"
            assert execution(db, ambiguous).state == "ambiguous" and execution(db, ambiguous).attempts == 1
            rows = list(db.scalars(select(ExecutionReconciliation).where(ExecutionReconciliation.approval_id == ambiguous)))
            assert len(rows) == 2 and all(row.actor == "ci-reviewer" for row in rows)
        settings.mock_action_ambiguous = False
        scan()
        with sessions() as db:
            assert execution(db, replacement) is None
        decide(replacement)
        scan()
        with sessions() as db:
            assert execution(db, replacement).state == "succeeded"
        transient = queue_case("production-transient")
        decide(transient)
        settings.mock_action_failures = 1
        settings.retry_base_seconds = 2
        scan()
        scan()
        with sessions() as db:
            assert execution(db, transient).state == "retryable" and execution(db, transient).attempts == 1
        time.sleep(2.2)
        scan()
        scan()
        with sessions() as db:
            assert execution(db, transient).state == "succeeded" and execution(db, transient).attempts == 2
        started = queue_case("production-started")
        decide(started)
        with sessions() as db:
            approval = db.get(Approval, started)
            db.add(ActionExecution(idempotency_key=f"approval:{started}", proposed_action=approval.proposed_action,
                evidence_hash=approval.evidence_hash, state="started", attempts=1))
            db.commit()
        scan()
        scan()
        with sessions() as db:
            assert execution(db, started).state == "ambiguous" and execution(db, started).attempts == 1
            assert db.get(Approval, started).execution_status == "ambiguous"
        print("PASS: PostgreSQL HTTP recovery: uncertainty blocked, reconciliation idempotent, fresh approval required, due retry only, persisted started attempt fails closed")
    finally:
        settings.mock_action_ambiguous, settings.mock_action_failures, settings.retry_base_seconds = old_ambiguous, old_failures, old_delay


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
            verify_recovery(sessions, admin, support, support_token, reviewer, reviewer_token)
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
