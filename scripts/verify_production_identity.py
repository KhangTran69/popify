"""Exercise production configuration and managed identity on disposable PostgreSQL.

The HTTPS TestClient models secure cookies, not a real TLS network connection.
Run only on an explicitly disposable database; each run owns a unique schema.
"""
import os
import re
import uuid
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import main
from app.config import settings
from app.db import engine, get_db
from app.schema import migration_paths
from scripts.migrate import migrate
from app.identity import manage_account, managed_login, session_account, revoke_session
from app import identity
from app.models import Approval, Audit, Case, ActionExecution, ExecutionReconciliation, Delivery, Order, utcnow
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event

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


def verify_delivery_batches(sessions):
    """Competing HTTP batch calls must preserve per-delivery transaction boundaries."""
    now = utcnow()
    keys = [f"production-batch-{i}" for i in range(9)]
    with sessions() as db:
        for key in keys:
            db.add(Delivery(webhook_id=key, topic="orders/create", shop_domain="demo.myshopify.com",
                            payload={"id": key, "created_at": now.isoformat(), "updated_at": now.isoformat()},
                            status="received"))
        db.add(Delivery(webhook_id="production-batch-future", topic="orders/create", shop_domain="demo.myshopify.com",
                        payload={}, status="failed", attempts=1, next_attempt_at=now+timedelta(hours=1)))
        db.commit()
    def poll():
        client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
        try:
            response = client.post("/api/n8n/process-next?batch_size=5", headers={"X-N8N-Secret": settings.n8n_webhook_secret})
            assert response.status_code == 200
            return response.json()["outcomes"]["processed"]
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: poll(), range(2)))
    assert sum(counts) == len(keys), counts
    with sessions() as db:
        assert len(list(db.scalars(select(Order).where(Order.external_id.in_(keys))))) == len(keys)
        deliveries = list(db.scalars(select(Delivery).where(Delivery.webhook_id.in_(keys))))
        assert all(d.status == "processed" and d.attempts == 1 for d in deliveries)
        future = db.scalar(select(Delivery).where(Delivery.webhook_id == "production-batch-future"))
        assert future.status == "failed" and future.attempts == 1
    print("PASS: competing bounded batch requests persisted nine orders exactly once at five-item limit and skipped scheduled retry")


def verify_support_batches(sessions):
    keys = [f"production-support-batch-{i}" for i in range(9)]
    with sessions() as db:
        for key in keys:
            db.add(Case(request_key=key, correlation_id=key, message="Synthetic inquiry", status="queued"))
        db.add(Case(request_key="production-support-future", message="Synthetic retry",
                    status="failed", attempts=1, next_attempt_at=utcnow()+timedelta(hours=1)))
        db.commit()
    def poll():
        client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
        try:
            response = client.post("/api/n8n/triage-next?batch_size=5", headers={"X-N8N-Secret": settings.n8n_webhook_secret})
            assert response.status_code == 200
            return response.json()["outcomes"]["needs_review"]
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: poll(), range(2))) == len(keys)
    with sessions() as db:
        cases = list(db.scalars(select(Case).where(Case.request_key.in_(keys))))
        assert len(cases) == len(keys) and all(c.status == "needs_review" and c.attempts == 1 for c in cases)
        for case in cases:
            approvals = list(db.scalars(select(Approval).where(Approval.subject_type == "case", Approval.subject_id == case.id)))
            assert len(approvals) == 1 and approvals[0].status == "pending"
        future = db.scalar(select(Case).where(Case.request_key == "production-support-future"))
        assert future.status == "failed" and future.attempts == 1
    print("PASS: competing support batches created nine pending approvals exactly once at five-item limit and skipped future retry")


def verify_worker_lock_contention(sessions):
    """A lock timeout must preserve previously committed work and retry budgets."""
    now = utcnow()
    first_key, blocked_key = "ci-lock-first", "ci-lock-blocked"
    with sessions() as db:
        for key, external in ((first_key, first_key), (blocked_key, "production-batch-0")):
            db.add(Delivery(webhook_id=key, topic="orders/create", shop_domain="demo.myshopify.com",
                            payload={"id": external, "created_at": now.isoformat(), "updated_at": now.isoformat()},
                            status="received"))
        db.commit()
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as client:
        with sessions() as blocker:
            blocker.scalar(select(Order).where(Order.external_id == "production-batch-0").with_for_update())
            response = client.post("/api/n8n/process-next?batch_size=5", headers=headers)
            assert response.status_code == 503 and response.headers["Retry-After"] == "5"
            with sessions() as db:
                first = db.scalar(select(Delivery).where(Delivery.webhook_id == first_key))
                blocked = db.scalar(select(Delivery).where(Delivery.webhook_id == blocked_key))
                assert first.status == "processed" and first.attempts == 1
                assert blocked.status == "received" and blocked.attempts == 0
            blocker.rollback()
        assert client.post("/api/n8n/process-next?batch_size=5", headers=headers).json()["outcomes"]["processed"] == 1
        with sessions() as db:
            blocked = db.scalar(select(Delivery).where(Delivery.webhook_id == blocked_key))
            assert blocked.status == "processed" and blocked.attempts == 1
            db.add(Case(request_key="ci-lock-support", message="Synthetic inquiry", status="queued"))
            db.commit()
        with sessions() as blocker:
            blocker.execute(text("LOCK TABLE approvals IN SHARE MODE"))
            response = client.post("/api/n8n/triage-next?batch_size=5", headers=headers)
            assert response.status_code == 503
            with sessions() as db:
                case = db.scalar(select(Case).where(Case.request_key == "ci-lock-support"))
                assert case.status == "queued" and case.attempts == 0
                assert not list(db.scalars(select(Approval).where(Approval.subject_type == "case", Approval.subject_id == case.id)))
            blocker.rollback()
        assert client.post("/api/n8n/triage-next?batch_size=5", headers=headers).json()["outcomes"]["needs_review"] == 1
        with sessions() as db:
            case = db.scalar(select(Case).where(Case.request_key == "ci-lock-support"))
            assert case.status == "needs_review" and case.attempts == 1
            approvals = list(db.scalars(select(Approval).where(Approval.subject_type == "case", Approval.subject_id == case.id)))
            assert len(approvals) == 1 and approvals[0].status == "pending"
    print("PASS: PostgreSQL worker lock timeout returns retryable 503, preserves committed batch items and retry budgets, then drains exactly once")


def verify_delayed_scan_contention(sessions):
    keys = [f"ci-delayed-scan-{i}" for i in range(8)]
    locked_key = keys[-1]
    with sessions() as db:
        for key in keys:
            db.add(Order(external_id=key, order_number=key, created_at=utcnow()-timedelta(hours=settings.order_delay_hours+1)))
        db.commit()
    def scan():
        with sessions() as db:
            from app.workers import scan_delayed_orders
            return scan_delayed_orders(db)
    with sessions() as blocker:
        locked = blocker.scalar(select(Order).where(Order.external_id == locked_key).with_for_update())
        locked.fulfillment_status = "fulfilled"
        blocker.flush()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: scan(), range(2)))
        assert sum(result[1] for result in results) == len(keys)-1, results
        blocker.commit()
    assert scan()[1] == 0
    with sessions() as db:
        orders = list(db.scalars(select(Order).where(Order.external_id.in_(keys))))
        for order in orders:
            approvals = list(db.scalars(select(Approval).where(
                Approval.subject_type == "order", Approval.subject_id == order.id,
                Approval.proposed_action == "review_delayed_order")))
            assert len(approvals) == (0 if order.external_id == locked_key else 1)
            assert all(a.status == "pending" for a in approvals)
    print("PASS: competing delayed scans report only their own inserts and skip an order being fulfilled")


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


def verify_mutation_revocation_order(sessions):
    with sessions() as db:
        manage_account(db, "ci-race-reviewer", "create", role="reviewer", password=PASSWORD)
        _, token, wait = managed_login(db, "ci-race-reviewer", PASSWORD, "synthetic-race")
        assert token and not wait
    attempting = Event()
    finished = Event()
    def revoke():
        with sessions() as db:
            attempting.set()
            manage_account(db, "ci-race-reviewer", "role", role="auditor")
        finished.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sessions() as db:
            assert session_account(db, token, lock_mutation=True).role == "reviewer"
            future = pool.submit(revoke)
            assert attempting.wait(5)
            assert not finished.wait(.3), "Revocation did not wait for in-flight authorized transaction"
            db.commit()
        future.result(timeout=10)
    with sessions() as db:
        assert session_account(db, token, lock_mutation=True) is None
    print("PASS: revocation serialized with authorized mutation; stale session denied after commit")


def verify_admin_logout_order(sessions):
    """An administrator's logout must serialize with account administration."""
    with sessions() as db:
        manage_account(db, "ci-race-admin", "create", role="admin", password=PASSWORD)
        manage_account(db, "ci-race-target", "create", role="support", password=PASSWORD)
        _, admin_token, wait = managed_login(db, "ci-race-admin", PASSWORD, "synthetic-admin-race")
        assert admin_token and not wait

    checked, release, logout_attempted = Event(), Event(), Event()
    original_lookup = identity.session_account

    def pause_after_revalidation(db, token, **kwargs):
        account = original_lookup(db, token, **kwargs)
        checked.set()
        if not release.wait(10):
            raise RuntimeError("Administrator race synchronization timed out")
        return account

    def change_role():
        with sessions() as db:
            manage_account(db, "ci-race-target", "role", role="reviewer",
                           actor="ci-race-admin", protect_last_admin=True,
                           administrator_token=admin_token)

    def sign_out():
        with sessions() as db:
            account = original_lookup(db, admin_token)
            assert account is not None
            logout_attempted.set()
            revoke_session(db, admin_token, account)

    identity.session_account = pause_after_revalidation
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            change = pool.submit(change_role)
            try:
                assert checked.wait(5)
                logout = pool.submit(sign_out)
                assert logout_attempted.wait(5)
                # The administrator session remains locked through the mutation.
                try:
                    logout.result(timeout=.5)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Logout committed before the authorized account mutation")
            finally:
                release.set()
            change.result(timeout=10)
            logout.result(timeout=10)
    finally:
        identity.session_account = original_lookup

    with sessions() as db:
        assert original_lookup(db, admin_token) is None
        from app.models import OperatorAccount
        target = db.scalar(select(OperatorAccount).where(OperatorAccount.username == "ci-race-target"))
        assert target.role == "reviewer"
        try:
            manage_account(db, "ci-race-target", "disable", actor="ci-race-admin",
                           protect_last_admin=True, administrator_token=admin_token)
        except PermissionError:
            db.rollback()
        else:
            raise AssertionError("Logged-out administrator was allowed to mutate an account")
        assert db.scalar(select(OperatorAccount.active).where(OperatorAccount.username == "ci-race-target"))
    print("PASS: administrator logout serialized with account mutation; logged-out administrator denied")


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
        migrate(isolated)
        with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as admin:
            assert admin.get("/ready").status_code == 503
            with sessions() as db:
                for role in ("admin", "support", "reviewer", "auditor"):
                    manage_account(db, "ci-" + role, "create", role=role, password=PASSWORD)
            assert admin.get("/ready").status_code == 200
            latest = migration_paths()[-1].name
            with sessions() as db:
                db.execute(text("DELETE FROM schema_migrations WHERE version=:v"), {"v":latest})
                db.commit()
            assert admin.get("/ready").status_code == 503
            with sessions() as db:
                db.execute(text("INSERT INTO schema_migrations(version) VALUES (:v)"), {"v":latest})
                db.commit()
            assert admin.get("/ready").status_code == 200
            print("PASS: managed readiness rejects missing migration ledger entry on actual migrated schema")
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
            assert support.post("/support", headers={"Sec-Fetch-Site": "cross-site"}, data={"csrf_token": support_token, "message": "Synthetic blocked", "request_key": "cross-site"}).status_code == 403
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
            verify_mutation_revocation_order(sessions)
            verify_admin_logout_order(sessions)
            verify_delivery_batches(sessions)
            verify_support_batches(sessions)
            verify_worker_lock_contention(sessions)
            verify_delayed_scan_contention(sessions)
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
