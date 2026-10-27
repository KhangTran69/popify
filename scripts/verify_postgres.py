"""Exercise concurrent behavior in an isolated, temporary PostgreSQL schema."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
import hashlib
import hmac
import json
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from app import main
from app.config import settings
from app.db import Base, engine, get_db
from app.models import Order, Delivery, Case, Approval, ActionExecution, ExecutionReconciliation, utcnow
from app.services import (
    process_delivery,
    create_approval,
    decide_approval,
    execute_approved,
    approval_freshness_differences,
    queue_support,
    SupportRequestKeyConflict,
    refresh_approval,
    queue_approval_retry,
    reconcile_approval_execution,
)
import app.services as services


def run():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Run inside the Compose API container")
    schema = "verify_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = create_engine(
        settings.database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    sessions = sessionmaker(isolated, expire_on_commit=False)
    old_secret = settings.shopify_webhook_secret
    try:
        Base.metadata.create_all(isolated)

        def session_override():
            with sessions() as db:
                yield db

        main.app.dependency_overrides[get_db] = session_override
        settings.shopify_webhook_secret = "isolated-test-secret"
        raw = json.dumps(
            {
                "id": "concurrent",
                "name": "#CONCURRENT",
                "updated_at": utcnow().isoformat(),
            }
        ).encode()
        headers = {
            "X-Shopify-Hmac-SHA256": base64.b64encode(
                hmac.new(
                    settings.shopify_webhook_secret.encode(), raw, hashlib.sha256
                ).digest()
            ).decode(),
            "X-Shopify-Webhook-Id": "same-delivery",
            "X-Shopify-Shop-Domain": settings.shopify_store_domain
            or "demo.myshopify.com",
        }

        def receive(_):
            with TestClient(main.app) as client:
                return client.post(
                    "/webhooks/shopify/orders", content=raw, headers=headers
                ).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(receive, range(6)))
        assert statuses.count(202) == 1 and statuses.count(200) == 5, statuses
        with sessions() as db:
            assert db.scalar(select(func.count(Delivery.id))) == 1
            delivery_id = db.scalar(select(Delivery.id))

        def process(_):
            with sessions() as db:
                return process_delivery(db, delivery_id)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(process, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(Order.id))) == 1
            now = utcnow()
            ids = []
            for i in range(6):
                d = Delivery(
                    webhook_id=f"ordered-{i}",
                    topic="orders/updated",
                    shop_domain="demo.myshopify.com",
                    payload={
                        "id": "first-insert-race",
                        "name": "#RACE",
                        "updated_at": (now + timedelta(seconds=i)).isoformat(),
                        "total_price": str(i),
                    },
                )
                db.add(d)
                db.flush()
                ids.append(d.id)
            db.commit()

        def process_id(delivery):
            with sessions() as db:
                return process_delivery(db, delivery)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(process_id, ids[::-1]))
        with sessions() as db:
            row = db.scalar(
                select(Order).where(Order.external_id == "first-insert-race")
            )
            assert row.total == "5"

        def submit_support(message):
            with sessions() as db:
                try:
                    case = queue_support(
                        db,
                        message,
                        "#CONFLICT",
                        "conflict@example.test",
                        "same-support-key",
                    )
                    return ("queued", case.id)
                except SupportRequestKeyConflict:
                    return ("conflict", None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            support_results = list(
                pool.map(submit_support, ["Where is my order?", "Refund this order."])
            )
        assert sorted(result[0] for result in support_results) == [
            "conflict",
            "queued",
        ], support_results
        with sessions() as db:
            assert db.scalar(
                select(func.count(Case.id)).where(
                    Case.request_key == "same-support-key"
                )
            ) == 1
            # Approval decisions validate current order state and delay eligibility.
            delayed_order = db.get(Order, row.id)
            delayed_order.created_at = utcnow() - timedelta(hours=settings.order_delay_hours + 1)
            delayed_order.fulfillment_status = "unfulfilled"
            db.flush()
            a = create_approval(
                db,
                "exception",
                "order",
                delayed_order.id,
                "review_delayed_order",
                {
                    "order_number": delayed_order.order_number,
                    "order_id": delayed_order.id,
                    "hours_threshold": settings.order_delay_hours,
                    "created_at": delayed_order.created_at.isoformat(),
                    "order_fulfillment_status": delayed_order.fulfillment_status,
                    "order_financial_status": delayed_order.financial_status,
                    "order_total": f"{delayed_order.currency} {delayed_order.total}",
                    "order_source_updated_at": delayed_order.source_updated_at.isoformat(),
                },
            )
            differences = approval_freshness_differences(db, a)
            assert not differences, f"PostgreSQL approval fixture is stale: {differences}"
            db.commit()
            aid = a.id

        def approve(_):
            with sessions() as db:
                decide_approval(db, aid, "approved", "test-operator")

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(approve, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(ActionExecution.id))) == 0

        # Conflicting operator clicks serialize on the subject lifecycle; only
        # the first committed decision is authoritative.
        with sessions() as db:
            conflict_order = Order(
                shop_domain="demo.myshopify.com", external_id="decision-conflict",
                order_number="#DECISION-RACE", email="race@example.test",
                currency="USD", total="20.00", fulfillment_status="unfulfilled",
                financial_status="paid",
                created_at=utcnow() - timedelta(hours=settings.order_delay_hours + 2),
                source_updated_at=utcnow(), payload={},
            )
            db.add(conflict_order)
            db.flush()
            conflict = create_approval(
                db, "exception", "order", conflict_order.id, "review_delayed_order",
                {
                    "order_number": conflict_order.order_number, "order_id": conflict_order.id,
                    "hours_threshold": settings.order_delay_hours,
                    "created_at": conflict_order.created_at.isoformat(),
                    "order_fulfillment_status": conflict_order.fulfillment_status,
                    "order_financial_status": conflict_order.financial_status,
                    "order_total": "USD 20.00",
                    "order_source_updated_at": conflict_order.source_updated_at.isoformat(),
                },
            )
            db.commit()
            conflict_id = conflict.id

        def conflicting_decision(decision):
            with sessions() as db:
                return decide_approval(db, conflict_id, decision, decision).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            decision_results = list(pool.map(conflicting_decision, ["approved", "rejected"]))
        assert len(set(decision_results)) == 1, decision_results
        with sessions() as db:
            decided = db.get(Approval, conflict_id)
            assert decided.status in ("approved", "rejected") and decided.actor == decided.status

        # A stale approval can be refreshed while an already selected worker
        # candidate runs. Both operations acquire the subject lock first.
        with sessions() as db:
            case = Case(
                message="Please check my order", category="order_status", confidence=82,
                draft="Reviewed support draft", status="needs_review",
                correlation_id="refresh-execute-race",
            )
            db.add(case)
            db.flush()
            facts = {
                "order_number": None, "order_id": None, "customer_email": None,
                "case_order_ref": None, "order_email": None,
                "customer_message": case.message,
                "policy": settings.store_policies[case.category],
                "order_fulfillment_status": None, "order_financial_status": None,
                "order_total": None, "order_source_updated_at": None,
                "category": case.category, "confidence": case.confidence,
                "draft": case.draft, "missing_info": "Verified order reference",
                "case_status": case.status, "correlation_id": case.correlation_id,
            }
            stale = create_approval(db, "support", "case", case.id, "send_reply", facts)
            stale.status = "approved"
            stale.execution_status = "evidence_changed"
            case.draft = "Current source draft"
            db.commit()
            stale_id = stale.id

        def refresh_stale(_):
            with sessions() as db:
                return refresh_approval(db, stale_id, "test-operator").id

        def execute_stale(_):
            with sessions() as db:
                approval = db.get(Approval, stale_id)
                execute_approved(db, approval)
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            refresh_future = pool.submit(refresh_stale, 0)
            execute_future = pool.submit(execute_stale, 0)
            replacement_id = refresh_future.result(timeout=10)
            execute_future.result(timeout=10)
        with sessions() as db:
            assert db.get(Approval, stale_id).replacement_approval_id == replacement_id
            assert db.scalar(select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{stale_id}"
            )) is None

        # Competing manual requeue and worker execution must produce at most one
        # additional persisted attempt, with the existing backoff still applied.
        prior_failures = settings.mock_action_failures
        prior_ambiguous = settings.mock_action_ambiguous
        settings.mock_action_failures = 0
        settings.mock_action_ambiguous = False
        with sessions() as db:
            case = Case(
                message="Please check my order", category="order_status", confidence=82,
                draft="Retryable support draft", status="needs_review",
                missing_info="Verified order reference",
                correlation_id="retry-execute-race",
            )
            db.add(case)
            db.flush()
            facts = {
                "order_number": None, "order_id": None, "customer_email": None,
                "case_order_ref": None, "order_email": None,
                "customer_message": case.message,
                "policy": settings.store_policies[case.category],
                "order_fulfillment_status": None, "order_financial_status": None,
                "order_total": None, "order_source_updated_at": None,
                "category": case.category, "confidence": case.confidence,
                "draft": case.draft, "missing_info": "Verified order reference",
                "case_status": case.status, "correlation_id": case.correlation_id,
            }
            retryable = create_approval(db, "support", "case", case.id, "send_reply", facts)
            retryable.status = "approved"
            retryable.execution_status = "retryable"
            retryable.attempts = 1
            retryable.next_attempt_at = utcnow() - timedelta(seconds=1)
            db.add(ActionExecution(
                idempotency_key=f"approval:{retryable.id}",
                proposed_action=retryable.proposed_action,
                evidence_hash=retryable.evidence_hash,
                state="retryable", attempts=1,
            ))
            db.commit()
            retry_id = retryable.id

        def requeue_and_run(_):
            with sessions() as db:
                try:
                    queue_approval_retry(db, retry_id, "test-operator")
                except ValueError:
                    db.rollback()  # execution won the lifecycle lock

        def execute_retry(_):
            with sessions() as db:
                execute_approved(db, db.get(Approval, retry_id))
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(requeue_and_run, 0), pool.submit(execute_retry, 0)]
            for future in futures:
                future.result(timeout=10)
        settings.mock_action_failures = prior_failures
        settings.mock_action_ambiguous = prior_ambiguous
        with sessions() as db:
            retry_execution = db.scalar(select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{retry_id}"
            ))
            assert retry_execution.state == "succeeded" and retry_execution.attempts == 2, {
                "execution_state": retry_execution.state,
                "execution_attempts": retry_execution.attempts,
                "approval_state": db.get(Approval, retry_id).execution_status,
                "approval_attempts": db.get(Approval, retry_id).attempts,
                "retry_deadline": db.get(Approval, retry_id).next_attempt_at,
                "freshness_differences": approval_freshness_differences(db, db.get(Approval, retry_id)),
            }

        # Hold the execution transaction immediately after it reads its locked
        # evidence. A concurrent order update must wait until the mock action
        # commits, then the next attempt must see that update as stale.
        freshness_checked = Event()
        release_execution = Event()
        source_update_started = Event()
        source_update_finished = Event()
        original_freshness = services.approval_freshness_differences

        def pause_after_freshness(db, approval):
            result = original_freshness(db, approval)
            if approval.id == aid:
                freshness_checked.set()
                if not release_execution.wait(10):
                    raise TimeoutError("concurrency verifier timed out waiting to release execution")
            return result

        services.approval_freshness_differences = pause_after_freshness

        def execute_once():
            with sessions() as db:
                execute_approved(db, db.get(Approval, aid))
                db.commit()

        def update_order():
            source_update_started.set()
            with sessions() as db:
                locked_order = db.scalar(
                    select(Order).where(Order.id == row.id).with_for_update()
                )
                locked_order.fulfillment_status = "fulfilled"
                locked_order.source_updated_at = utcnow() + timedelta(seconds=20)
                db.commit()
            source_update_finished.set()

        execution_thread = ThreadPoolExecutor(max_workers=2)
        execution_future = execution_thread.submit(execute_once)
        try:
            assert freshness_checked.wait(5), "execution did not reach freshness validation"
            update_future = execution_thread.submit(update_order)
            assert source_update_started.wait(2)
            assert not source_update_finished.wait(0.2), "source update bypassed the evidence lock"
            release_execution.set()
            execution_future.result(timeout=5)
            update_future.result(timeout=5)
        finally:
            release_execution.set()
            services.approval_freshness_differences = original_freshness
            execution_thread.shutdown(wait=True)
        assert source_update_finished.is_set()

        # Competing refresh clicks on one stale approval return one revision.
        with sessions() as db:
            case = Case(
                message="Where is my order?", category="order_status", confidence=80,
                draft="Old snapshot draft", status="needs_review",
                correlation_id="concurrent-refresh",
            )
            db.add(case)
            db.flush()
            refresh_source = case
            refresh_approval_row = create_approval(
                db,
                "support",
                "case",
                case.id,
                "send_reply",
                {
                    "customer_message": case.message,
                    "customer_email": None,
                    "case_order_ref": None,
                    "order_number": None,
                    "order_id": None,
                    "order_email": None,
                    "policy": settings.store_policies[case.category],
                    "order_fulfillment_status": None,
                    "order_financial_status": None,
                    "order_total": None,
                    "order_source_updated_at": None,
                    "category": case.category,
                    "confidence": case.confidence,
                    "draft": case.draft,
                    "missing_info": "Verified order reference",
                    "case_status": case.status,
                    "correlation_id": case.correlation_id,
                },
            )
            case.draft = "Current response draft"
            db.commit()
            refresh_id = refresh_approval_row.id

        def refresh_once(_):
            with sessions() as db:
                return refresh_approval(db, refresh_id, "test-operator").id

        with ThreadPoolExecutor(max_workers=6) as pool:
            replacement_ids = list(pool.map(refresh_once, range(6)))
        assert len(set(replacement_ids)) == 1, replacement_ids
        with sessions() as db:
            assert db.scalar(
                select(func.count(Approval.id)).where(
                    Approval.subject_id == refresh_source.id,
                    Approval.subject_type == "case",
                )
            ) == 2

        def execute(_):
            with sessions() as db:
                execute_approved(db, db.get(Approval, aid))
                db.commit()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(execute, range(6)))
        with sessions() as db:
            assert db.scalar(select(func.count(ActionExecution.id)).where(
                ActionExecution.idempotency_key == f"approval:{aid}"
            )) == 1
            assert db.scalar(select(ActionExecution.attempts).where(
                ActionExecution.idempotency_key == f"approval:{aid}"
            )) == 1
        # Concurrent duplicate assertions are idempotent; conflicting claims
        # serialize and leave exactly one immutable operator record.
        def ambiguous_case(correlation):
            with sessions() as db:
                case = Case(
                    message="Synthetic ambiguous outcome", category="order_status",
                    confidence=80, draft="Reviewed draft", status="needs_review",
                    missing_info="Verified order reference",
                    correlation_id=correlation,
                )
                db.add(case)
                db.flush()
                approval = create_approval(
                    db, "support", "case", case.id, "send_reply",
                    {
                        "order_number": None, "order_id": None, "customer_email": None,
                        "case_order_ref": None, "order_email": None,
                        "customer_message": case.message, "category": case.category,
                        "confidence": case.confidence, "draft": case.draft,
                        "policy": settings.store_policies[case.category],
                        "order_fulfillment_status": None, "order_financial_status": None,
                        "order_total": None, "order_source_updated_at": None,
                        "missing_info": "Verified order reference",
                        "case_status": case.status, "correlation_id": case.correlation_id,
                    },
                )
                decide_approval(db, approval.id, "approved", "test-operator")
                execute_approved(db, approval, mock_outcome="ambiguous")
                db.commit()
                return approval.id

        def reconciliation_request(approval_id, outcome, explanation, reference):
            with sessions() as db:
                try:
                    result = reconcile_approval_execution(
                        db, approval_id, outcome, explanation, reference,
                        "test-operator", synthetic=True,
                    )
                    return ("ok", result.outcome)
                except ValueError:
                    db.rollback()
                    return ("conflict", None)

        identical_id = ambiguous_case("concurrent-identical-reconciliation")
        identical = (
            identical_id, "confirmed_not_executed",
            "Concurrent review confirms the synthetic action did not execute.",
            "SYNTH-CONCURRENT-IDENTICAL",
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: reconciliation_request(*identical), range(6)))
        assert outcomes == [("ok", "confirmed_not_executed")] * 6, outcomes
        with sessions() as db:
            original = db.scalar(select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{identical_id}"
            ))
            assert original.state == "ambiguous" and original.attempts == 1
            assert db.scalar(select(func.count(ExecutionReconciliation.id)).where(
                ExecutionReconciliation.approval_id == identical_id
            )) == 1

        conflicting_id = ambiguous_case("concurrent-conflicting-reconciliation")
        def conflicting_request(outcome):
            return reconciliation_request(
                conflicting_id, outcome,
                "Concurrent review records one supported operator conclusion.",
                f"SYNTH-CONCURRENT-{conflicting_id}",
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(conflicting_request,
                                     ["confirmed_completed", "confirmed_not_executed"]))
        assert all(result[0] == "ok" for result in outcomes), outcomes
        assert "conflicting_evidence" in [result[1] for result in outcomes], outcomes
        with sessions() as db:
            rows = db.scalars(select(ExecutionReconciliation).where(
                ExecutionReconciliation.approval_id == conflicting_id
            ).order_by(ExecutionReconciliation.revision)).all()
            assert len(rows) == 2 and rows[1].outcome == "conflicting_evidence", rows
            assert rows[1].previous_reconciliation_id == rows[0].id
            disputed = db.get(Approval, conflicting_id)
            assert disputed.execution_status == "disputed"
            resolved = reconcile_approval_execution(
                db, conflicting_id, "confirmed_not_executed",
                "A later locked review confirms the action did not execute.",
                f"SYNTH-CONCURRENT-RESOLVED-{conflicting_id}", "test-operator-2", synthetic=True,
            )
            assert resolved.revision == 3 and resolved.outcome == "confirmed_not_executed"
            assert resolved.previous_reconciliation_id == rows[1].id
            original = db.scalar(select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{conflicting_id}"
            ))
            assert original.state == "ambiguous" and original.attempts == 1
            assert disputed.execution_status == "reconciled_not_executed"

        blocked_id = ambiguous_case("concurrent-blocker-release")
        with sessions() as db:
            blocker = db.get(Approval, blocked_id)
            queued_approval = create_approval(
                db, blocker.kind, blocker.subject_type, blocker.subject_id,
                blocker.proposed_action, dict(blocker.supporting_facts), revision=2,
            )
            decide_approval(db, queued_approval.id, "approved", "test-operator")
            execute_approved(db, queued_approval)
            db.commit()
            queued_id = queued_approval.id
            assert queued_approval.execution_status == "blocked_prior_outcome"

        def resolve_blocker():
            with sessions() as db:
                return reconcile_approval_execution(
                    db, blocked_id, "confirmed_not_executed",
                    "The isolated ledger confirms the earlier mock did not execute.",
                    f"SYNTH-RELEASE-{blocked_id}", "test-operator", synthetic=True,
                ).outcome

        def release_new_approval():
            with sessions() as db:
                try:
                    return queue_approval_retry(db, queued_id, "test-operator").execution_status
                except ValueError:
                    db.rollback()
                    return "still_blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            release_results = list(pool.map(lambda fn: fn(), (resolve_blocker, release_new_approval)))
        assert release_results[0] == "confirmed_not_executed", release_results
        with sessions() as db:
            queued_approval = db.get(Approval, queued_id)
            if queued_approval.execution_status == "blocked_prior_outcome":
                queue_approval_retry(db, queued_id, "test-operator")
            assert queued_approval.execution_status == "queued"
            execute_approved(db, queued_approval)
            db.commit()
            assert queued_approval.execution_status == "succeeded"
            original = db.scalar(select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{blocked_id}"
            ))
            assert original.state == "ambiguous" and original.attempts == 1

        print(
            "PASS: concurrent webhook duplicates, duplicate workers, first-insert/out-of-order races, conflicting support request-key reuse, repeated and conflicting approval decisions, source freshness locking, refresh/execution and retry/execution races, concurrent replacement requests, immutable reconciliation follow-ups, concurrent blocker resolution/requeue, and repeated execution."
        )
    finally:
        settings.shopify_webhook_secret = old_secret
        main.app.dependency_overrides.pop(get_db, None)
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


if __name__ == "__main__":
    run()
