import pytest
import base64, hashlib, hmac, json
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.db import Base, get_db
from app.config import settings
from app.models import (
    Order, Delivery, Approval, ActionExecution, ExecutionReconciliation, Case, utcnow,
)
from app.services import (
    process_delivery,
    create_approval as service_create_approval,
    decide_approval,
    verify_shopify,
    create_support_case,
    execute_approved,
    as_utc,
    SupportRequestKeyConflict,
    refresh_approval,
    ApprovalEvidenceStale,
    approval_freshness_differences,
    approval_freshness_comparison,
    queue_approval_retry,
    reconcile_approval_execution,
)

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)


def create_approval(db, kind, subject_type, subject_id, action, facts, revision=None):
    """Give legacy service tests coherent source rows for freshness validation."""
    if subject_type == "order" and action == "review_delayed_order" and not db.get(Order, subject_id):
        order = Order(
            id=subject_id, shop_domain="test.myshopify.com", external_id=f"test-{subject_id}",
            order_number=f"#{subject_id}", email="test@example.test", currency="USD",
            total="12.00", fulfillment_status="unfulfilled", financial_status="paid",
            created_at=utcnow() - timedelta(hours=72), source_updated_at=utcnow(), payload={},
        )
        db.add(order)
        db.flush()
    if subject_type == "case" and not db.get(Case, subject_id):
        order = Order(
            shop_domain="test.myshopify.com", external_id=f"case-order-{subject_id}",
            order_number=f"#CASE-{subject_id}", email="test@example.test", currency="USD",
            total="12.00", fulfillment_status="unfulfilled", financial_status="paid",
            created_at=utcnow() - timedelta(hours=72), source_updated_at=utcnow(), payload={},
        )
        db.add(order)
        db.flush()
        category = facts.get("category", "other")
        db.add(Case(
            id=subject_id, order_id=order.id, order_ref=order.order_number,
            customer_email=order.email, message=facts.get("customer_message", "Synthetic inquiry"),
            category=category, confidence=facts.get("confidence", 80),
            draft=facts.get("draft", "Synthetic reviewed draft"), status="needs_review",
            missing_info=facts.get("missing_info", ""),
        ))
        db.flush()
    source_order = None
    if subject_type == "order":
        source_order = db.get(Order, subject_id)
    elif subject_type == "case":
        case = db.get(Case, subject_id)
        source_order = db.get(Order, case.order_id) if case and case.order_id else None
    if source_order:
        source_order.shop_domain = source_order.shop_domain or "test.myshopify.com"
        source_order.external_id = source_order.external_id or f"test-{source_order.id}"
        source_order.order_number = source_order.order_number or f"#TEST-{source_order.id}"
        source_order.email = source_order.email or "test@example.test"
        source_order.currency = source_order.currency or "USD"
        source_order.total = source_order.total or "12.00"
        source_order.fulfillment_status = source_order.fulfillment_status or "unfulfilled"
        source_order.financial_status = source_order.financial_status or "paid"
        source_order.created_at = source_order.created_at or (utcnow() - timedelta(hours=72))
        if action == "review_delayed_order" and source_order.created_at > utcnow() - timedelta(hours=settings.order_delay_hours + 1):
            source_order.created_at = utcnow() - timedelta(hours=settings.order_delay_hours + 2)
        source_order.source_updated_at = source_order.source_updated_at or utcnow()
        source_order.payload = source_order.payload or {}
        db.flush()
        completed = dict(facts)
        completed.setdefault("order_number", source_order.order_number)
        completed.setdefault("order_id", source_order.id)
        completed.setdefault("order_fulfillment_status", source_order.fulfillment_status)
        completed.setdefault("order_financial_status", source_order.financial_status)
        completed.setdefault("order_total", f"{source_order.currency} {source_order.total}")
        completed.setdefault("order_source_updated_at", source_order.source_updated_at.isoformat())
        completed.setdefault("created_at", source_order.created_at.isoformat())
        completed.setdefault("hours_threshold", settings.order_delay_hours)
        if subject_type == "case":
            case = db.get(Case, subject_id)
            completed.setdefault("customer_email", case.customer_email)
            completed.setdefault("case_order_ref", case.order_ref)
            completed.setdefault("order_email", source_order.email if case.order_id else None)
            completed.setdefault("customer_message", case.message)
            completed.setdefault("policy", settings.store_policies[case.category])
            completed.setdefault("category", case.category)
            completed.setdefault("confidence", case.confidence)
            completed.setdefault("draft", case.draft)
            completed.setdefault("missing_info", case.missing_info or "")
            completed.setdefault("case_status", case.status)
        facts = completed
    return service_create_approval(db, kind, subject_type, subject_id, action, facts, revision)


def override_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


main.app.dependency_overrides[get_db] = override_db
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    client.cookies.clear()
    yield


def run_due(db, approval):
    approval.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.flush()
    execute_approved(db, approval)
    db.commit()


def sig(raw, secret="test-secret"):
    return base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode()


def test_signed_webhook_duplicate_and_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "shopify_webhook_secret", "test-secret")
    raw = json.dumps(
        {
            "id": 1,
            "name": "#1",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ).encode()
    headers = {
        "X-Shopify-Hmac-SHA256": sig(raw),
        "X-Shopify-Webhook-Id": "delivery-1",
        "X-Shopify-Shop-Domain": "demo.myshopify.com",
    }
    r = client.post("/webhooks/shopify/orders", content=raw, headers=headers)
    assert r.status_code == 202
    r = client.post("/webhooks/shopify/orders", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert (
        client.post(
            "/webhooks/shopify/orders",
            content=raw,
            headers={"X-Shopify-Hmac-SHA256": "wrong"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/webhooks/shopify/orders",
            content=raw,
            headers={
                "X-Shopify-Hmac-SHA256": sig(raw),
                "X-Shopify-Shop-Domain": "demo.myshopify.com",
            },
        ).status_code
        == 400
    )
    with TestingSession() as db:
        delivery = db.scalar(
            select(Delivery).where(Delivery.webhook_id == "delivery-1")
        )
        assert delivery.status == "received"
        assert db.scalar(select(Order).where(Order.external_id == "1")) is None
        process_delivery(db, delivery.id)
        process_delivery(db, delivery.id)
        assert db.scalar(select(Order).where(Order.external_id == "1")) is not None
        assert len(db.scalars(select(Order).where(Order.external_id == "1")).all()) == 1
        assert verify_shopify(raw, sig(raw, "test-secret"))


def test_isolated_preview_blocks_workflow_and_webhook_routes(monkeypatch):
    monkeypatch.setattr(main.settings, "preview_mode", True)
    for path in ("/api/n8n/order-intake", "/webhooks/shopify/orders"):
        response = client.post(path)
        assert response.status_code == 409
        assert "disabled in the isolated preview" in response.json()["detail"]


def test_delayed_order_scan_is_bounded_and_continues_without_starvation():
    with TestingSession() as db:
        for i in range(105):
            db.add(Order(
                shop_domain="scan.test", external_id=f"scan-{i}", order_number=f"#SCAN-{i}",
                email="scan@example.test", currency="USD", total="1.00",
                fulfillment_status="unfulfilled", financial_status="paid",
                created_at=utcnow() - timedelta(days=3), source_updated_at=utcnow(), payload={},
            ))
        db.commit()
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    first = client.post("/api/n8n/delayed-exceptions", headers=headers).json()
    assert first["open_delayed_orders"] == 100 and first["delayed_scan_has_more"] is True
    second = client.post("/api/n8n/delayed-exceptions", headers=headers).json()
    assert second["open_delayed_orders"] == 5 and second["delayed_scan_has_more"] is False
    with TestingSession() as db:
        assert db.scalar(
            select(func.count()).select_from(Approval).where(
                Approval.proposed_action == "review_delayed_order"
            )
        ) == 105


def test_preview_settings_ignore_dotenv_and_integration_environment(monkeypatch):
    import os
    from app.config import load_settings

    monkeypatch.setenv("POPIFY_PREVIEW_MODE", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/popify-isolated.db")
    monkeypatch.setenv("LLM_MODE", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "must-not-be-used")
    monkeypatch.setenv("N8N_BASE_URL", "http://127.0.0.1:5678")
    preview = load_settings()
    assert preview.preview_mode is True
    assert preview.llm_mode == "mock" and preview.openai_api_key == ""
    assert preview.shopify_admin_token == "" and preview.n8n_base_url == ""
    assert preview.database_url == os.environ["DATABASE_URL"]


def test_out_of_order_event_does_not_overwrite():
    with TestingSession() as db:
        old = {
            "id": 2,
            "name": "#2",
            "total_price": "12",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        new = {
            "id": 2,
            "name": "#2",
            "total_price": "99",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        d1 = Delivery(
            webhook_id="old",
            topic="orders/updated",
            shop_domain="demo.myshopify.com",
            payload=old,
        )
        db.add(d1)
        db.commit()
        db.refresh(d1)
        process_delivery(db, d1.id)
        d2 = Delivery(
            webhook_id="new",
            topic="orders/updated",
            shop_domain="demo.myshopify.com",
            payload=new,
        )
        db.add(d2)
        db.commit()
        db.refresh(d2)
        process_delivery(db, d2.id)
        d3 = Delivery(
            webhook_id="late",
            topic="orders/updated",
            shop_domain="demo.myshopify.com",
            payload=old,
        )
        db.add(d3)
        db.commit()
        db.refresh(d3)
        process_delivery(db, d3.id)
        order = db.scalar(select(Order).where(Order.external_id == "2"))
        assert order.total == "99"


def test_support_draft_uses_known_facts_and_policy():
    with TestingSession() as db:
        db.add(
            Order(
                shop_domain="demo.myshopify.com",
                external_id="3",
                order_number="#3",
                email="x@example.test",
                currency="USD",
                total="50.00",
                fulfillment_status="unfulfilled",
                source_updated_at=utcnow(),
                created_at=utcnow(),
                payload={"demo": True},
            )
        )
        db.commit()
        case = create_support_case(db, "Where is my order?", "#3", "x@example.test")
        assert case.category == "order_status" and "$" not in case.draft
        assert "unfulfilled" in case.draft and case.status == "needs_review"
        assert (
            db.scalar(
                select(Approval).where(
                    Approval.subject_type == "case",
                    Approval.subject_id == case.id,
                    Approval.proposed_action == "send_reply",
                )
            )
            is not None
        )


def test_approval_idempotence_and_retry():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 99, "send_reply", {"draft": "hello"}
        )
        first = decide_approval(db, approval.id, "approved", "operator")
        assert first.execution_status == "queued"
        assert db.scalar(select(ActionExecution)) is None
        run_due(db, first)
        assert first.execution_status == "succeeded"
        again = decide_approval(db, approval.id, "approved", "operator")
        assert again.execution_status == "succeeded"
        assert len(db.scalars(select(ActionExecution)).all()) == 1


def test_operator_authentication_required():
    assert client.get("/", follow_redirects=False).status_code == 401


def test_transient_action_failure_and_safe_retry(monkeypatch):
    monkeypatch.setattr(settings, "mock_action_failures", 1)
    with TestingSession() as db:
        approval = create_approval(
            db,
            "exception",
            "order",
            123,
            "review_delayed_order",
            {"order_number": "#123"},
        )
        decide_approval(db, approval.id, "approved", "operator")
        run_due(db, approval)
        assert approval.execution_status == "retryable" and approval.attempts == 1
        execute_approved(db, approval)
        db.commit()
        assert approval.attempts == 1  # Backoff must prevent an early retry.
        run_due(db, approval)
        assert approval.execution_status == "succeeded" and approval.attempts == 2
        rows = db.scalars(
            select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{approval.id}"
            )
        ).all()
        assert len(rows) == 1 and rows[0].attempts == 2


def test_unapproved_action_cannot_execute():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 777, "send_reply", {"draft": "hello"}
        )
        assert (
            approval.status == "pending" and approval.execution_status == "not_started"
        )
        assert (
            db.scalar(
                select(ActionExecution).where(
                    ActionExecution.idempotency_key == f"approval:{approval.id}"
                )
            )
            is None
        )


def test_support_order_match_requires_reference_and_email():
    with TestingSession() as db:
        db.add(
            Order(
                shop_domain="demo.myshopify.com",
                external_id="8",
                order_number="#8",
                email="right@example.test",
                currency="USD",
                total="12",
                fulfillment_status="fulfilled",
                source_updated_at=utcnow(),
                created_at=utcnow(),
                payload={},
            )
        )
        db.commit()
        case = create_support_case(db, "Where is my order?", "#8", "wrong@example.test")
        assert case.order_id is None
        assert case.missing_info == "Verified order reference"


def test_exhausted_failure_is_visible_and_manual_retry_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "mock_action_failures", 3)
    monkeypatch.setattr(settings, "max_action_attempts", 3)
    with TestingSession() as db:
        approval = create_approval(
            db,
            "exception",
            "order",
            456,
            "review_delayed_order",
            {"order_number": "#456"},
        )
        decide_approval(db, approval.id, "approved", "operator")
        for _ in range(3):
            run_due(db, approval)
        assert approval.execution_status == "exhausted" and approval.attempts == 3
        from app.services import execute_approved

        execute_approved(db, approval)
        db.commit()
        assert approval.attempts == 3  # Exhaustion requires explicit operator requeue.
        approval.execution_status = "queued"
        run_due(db, approval)
        assert approval.execution_status == "succeeded" and approval.attempts == 4
        assert (
            len(
                db.scalars(
                    select(ActionExecution).where(
                        ActionExecution.idempotency_key == f"approval:{approval.id}"
                    )
                ).all()
            )
            == 1
        )


def test_ambiguous_action_outcome_blocks_blind_retry(monkeypatch):
    monkeypatch.setattr(settings, "mock_action_ambiguous", True)
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 987, "send_reply", {"draft": "hello"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        run_due(db, approval)
        assert approval.execution_status == "ambiguous" and approval.attempts == 1
        from app.services import execute_approved

        execute_approved(db, approval)
        db.commit()
        assert approval.execution_status == "ambiguous" and approval.attempts == 1


def make_ambiguous_approval(subject_id):
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", subject_id, "send_reply",
            {"draft": "Reviewed synthetic reply", "category": "order_status", "confidence": 80},
        )
        decide_approval(db, approval.id, "approved", "operator")
        execute_approved(db, approval, mock_outcome="ambiguous")
        db.commit()
        return approval.id


def test_reconciliation_records_operator_assertion_without_rewriting_ambiguous_execution():
    token = sign_in()
    approval_id = make_ambiguous_approval(2101)
    form = {
        "csrf_token": token,
        "outcome": "confirmed_completed",
        "explanation": "Operator checked the synthetic completion ledger.",
        "reference": "SYNTH-LEDGER-2101",
    }
    response = client.post(f"/approvals/{approval_id}/reconcile", data=form, follow_redirects=False)
    assert response.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        reconciliation = db.scalar(select(ExecutionReconciliation).where(
            ExecutionReconciliation.approval_id == approval_id
        ))
        assert approval.execution_status == "reconciled_completed"
        assert execution.state == "ambiguous" and execution.attempts == 1
        assert reconciliation.actor == "operator"
        assert reconciliation.reference == "SYNTH-LEDGER-2101"
        assert reconciliation.outcome == "confirmed_completed"
    detail = client.get(f"/approvals/{approval_id}")
    assert "Current conclusion · completed" in detail.text
    assert "no provider was queried" in detail.text
    assert "Execution outcome reconciled" in detail.text

    # Repeating the same submission is idempotent; a different assertion cannot overwrite it.
    duplicate = client.post(f"/approvals/{approval_id}/reconcile", data=form, follow_redirects=False)
    assert duplicate.status_code == 303
    conflict = client.post(f"/approvals/{approval_id}/reconcile", data={
        **form, "outcome": "confirmed_not_executed", "reference": "SYNTH-NO-EXEC-2101",
    }, follow_redirects=False)
    assert conflict.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        rows = db.scalars(select(ExecutionReconciliation).where(
            ExecutionReconciliation.approval_id == approval_id
        ).order_by(ExecutionReconciliation.revision)).all()
        assert len(rows) == 2 and rows[1].outcome == "conflicting_evidence"
        assert rows[1].asserted_outcome == "confirmed_not_executed"
        assert approval.execution_status == "disputed"


def test_confirmed_nonexecution_requires_fresh_pending_approval_and_never_retries_old_revision():
    approval_id = make_ambiguous_approval(2102)
    with TestingSession() as db:
        original = db.get(Approval, approval_id)
        reconcile_approval_execution(
            db, approval_id, "confirmed_not_executed",
            "Synthetic review confirms no mock action was recorded.",
            "SYNTH-REVIEW-2102", "operator", synthetic=True,
        )
        assert original.execution_status == "reconciled_not_executed"
        original_execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert original_execution.state == "ambiguous"
        replacement = refresh_approval(db, approval_id, "operator")
        replacement_id = replacement.id
        assert replacement.status == "pending" and replacement.execution_status == "not_started"
        execute_approved(db, original)
        db.commit()
        assert original.execution_status == "reconciled_not_executed"
        assert original_execution.state == "ambiguous"
    with TestingSession() as db:
        replacement = decide_approval(db, replacement_id, "approved", "operator")
        execute_approved(db, replacement)
        db.commit()
        assert replacement.execution_status == "succeeded"
        original_execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert original_execution.state == "ambiguous" and original_execution.attempts == 1
        rows, _, _, _ = __import__("app.recovery", fromlist=["recovery_page"]).recovery_page(db)
        assert all(row["record_id"] != approval_id for row in rows)


def test_unknown_reconciliation_keeps_approval_uncertain_and_blocks_recovery_actions():
    token = sign_in()
    approval_id = make_ambiguous_approval(2103)
    response = client.post(f"/approvals/{approval_id}/reconcile", data={
        "csrf_token": token,
        "outcome": "still_unknown",
        "explanation": "Available synthetic information cannot confirm either outcome.",
        "reference": "",
    }, follow_redirects=False)
    assert response.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.execution_status == "ambiguous"
        rows, _, _, _ = __import__("app.recovery", fromlist=["recovery_page"]).recovery_page(db, category="uncertain")
        assert any(row["record_id"] == approval_id for row in rows)
        with pytest.raises(ValueError, match="Only a pending, stale"):
            refresh_approval(db, approval_id, "operator")
    retry_response = client.post(f"/approvals/{approval_id}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert retry_response.status_code == 409
    detail = client.get(f"/approvals/{approval_id}")
    assert "Current conclusion · still unknown" in detail.text
    assert "retries and replacement approvals remain blocked" in detail.text


def test_preview_scenario_page_shows_correlated_journey_and_reconciliation_form(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval = create_preview_scenario(db, "ambiguous", actor="operator")
        approval_id = approval.id
        correlation_id = approval.supporting_facts["correlation_id"]
    page = client.get(f"/demo/scenarios/{approval_id}")
    assert page.status_code == 200
    assert f"{correlation_id}" in page.text
    assert f"/orders/" in page.text and f"/cases/" in page.text
    assert "Journey history" in page.text
    assert "Append operator reconciliation" in page.text
    assert f"from_scenario={approval_id}" in page.text
    assert "Customer inquiry: Synthetic ambiguous support request" in page.text
    assert "automation saved an order status reply draft. Preview does not run n8n." in page.text
    assert "Approved · send reply by operator" in page.text
    assert "Mock execution: ambiguous after 1 attempt." in page.text
    result = client.post(
        f"/approvals/{approval_id}/reconcile?from_scenario={approval_id}",
        data={"csrf_token": token, "scenario_id": approval_id,
              "outcome": "still_unknown",
              "explanation": "Synthetic evidence leaves the outcome unknown.",
              "reference": ""},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert result.headers["location"] == f"/demo/scenarios/{approval_id}"
    refreshed = client.get(result.headers["location"])
    assert "Outcome remains unknown" in refreshed.text
    assert "Append operator reconciliation" in refreshed.text
    assert "no provider was queried" in refreshed.text

    # A confirmed non-execution returns to the same scenario and offers only
    # a new pending decision, never a retry of the ambiguous execution.
    with TestingSession() as db:
        confirmed = create_preview_scenario(db, "ambiguous", actor="operator")
        confirmed_id = confirmed.id
    reconciled = client.post(
        f"/approvals/{confirmed_id}/reconcile?from_scenario={confirmed_id}",
        data={"csrf_token": token, "scenario_id": confirmed_id,
              "outcome": "confirmed_not_executed",
              "explanation": "Synthetic review confirms no mock execution was saved.",
              "reference": "SYNTH-FRESH-APPROVAL"},
        follow_redirects=False,
    )
    assert reconciled.headers["location"] == f"/demo/scenarios/{confirmed_id}"
    assert "Create a fresh pending approval" in client.get(reconciled.headers["location"]).text
    replacement = client.post(
        f"/approvals/{confirmed_id}/refresh?from_scenario={confirmed_id}",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert replacement.status_code == 303
    assert replacement.headers["location"].startswith("/demo/scenarios/")
    replacement_page = client.get(replacement.headers["location"])
    assert "Review the evidence and approve or reject the pending decision." in replacement_page.text


def test_stale_preview_approval_submission_after_rejection_is_idempotent(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval = start_preview_scenario(db, "pending", "operator")
        approval_id = approval.id

    rejected = client.post(
        f"/approvals/{approval_id}/decision",
        data={"csrf_token": token, "decision": "rejected"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    replayed = client.post(
        f"/approvals/{approval_id}/decision",
        data={"csrf_token": token, "decision": "approved"},
        follow_redirects=False,
    )
    assert replayed.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.status == "rejected"
        assert db.scalar(select(ActionExecution)) is None


def test_preview_scenario_approval_revision_pager_labels_match_direction(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    sign_in()
    with TestingSession() as db:
        previous = start_preview_scenario(db, "pending", "operator")
        original_id = previous.id
        for revision in range(2, 22):
            previous.status = "superseded"
            replacement = service_create_approval(
                db,
                previous.kind,
                previous.subject_type,
                previous.subject_id,
                previous.proposed_action,
                previous.supporting_facts,
                revision=revision,
            )
            previous.replacement_approval_id = replacement.id
            previous = replacement
        db.commit()

    first_page = client.get(f"/demo/scenarios/{original_id}?revision_page=1")
    older_page = client.get(f"/demo/scenarios/{original_id}?revision_page=2")
    assert 'href="?revision_page=2&amp;reconciliation_page=1&amp;audit_page=1">Older revisions</a>' in first_page.text
    assert 'href="?revision_page=1&amp;reconciliation_page=1&amp;audit_page=1">Newer revisions</a>' in older_page.text
    assert f"Current approval #{previous.id}" in older_page.text


def test_execution_is_bound_to_reviewed_action_and_evidence():
    with TestingSession() as db:
        approval = create_approval(
            db,
            "support",
            "case",
            44,
            "send_reply",
            {"draft": "Reviewed draft", "policy": settings.store_policies["other"]},
        )
        decide_approval(db, approval.id, "approved", "operator")
        reviewed_hash = approval.evidence_hash
        run_due(db, approval)
        execution = db.scalar(select(ActionExecution))
        assert execution.proposed_action == "send_reply"
        assert execution.evidence_hash == reviewed_hash
        assert approval.execution_status == "succeeded"


def test_terminal_execution_contract_mismatch_is_not_reported_as_success():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 441, "send_reply", {"draft": "Reviewed"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        run_due(db, approval)
        execution = db.scalar(select(ActionExecution))
        attempts = execution.attempts
        execution.proposed_action = "different_action"
        db.commit()
        execute_approved(db, approval)
        db.commit()
        assert approval.execution_status == "evidence_changed"
        assert execution.attempts == attempts
        with pytest.raises(ValueError, match="inspect its outcome"):
            refresh_approval(db, approval.id, "operator")


def test_changed_evidence_blocks_execution_before_mock_action():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 45, "send_reply", {"draft": "Reviewed draft"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        approval.supporting_facts = {"draft": "Changed after approval"}
        db.commit()
        run_due(db, approval)
        assert approval.execution_status == "evidence_changed"
        assert "changed after the operator decision" in approval.failure_detail
        assert db.scalar(select(ActionExecution)) is None


def test_changed_evidence_is_reported_as_a_worker_failure():
    from app.models import Audit

    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 450, "send_reply", {"draft": "Reviewed draft"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        approval.supporting_facts = {"draft": "Changed after approval"}
        db.commit()
    response = client.post(
        "/api/n8n/delayed-exceptions",
        headers={"X-N8N-Secret": settings.n8n_webhook_secret},
    )
    assert response.status_code == 200
    assert response.json()["action_outcomes"]["evidence_changed"] == 1
    with TestingSession() as db:
        assert db.scalar(
            select(Audit).where(
                Audit.entity_type == "workflow",
                Audit.entity_id == 3,
                Audit.event == "work_failed",
            )
        )


def test_changed_pending_evidence_requires_fresh_approval():
    token = sign_in()
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 46, "send_reply", {"draft": "Original"}
        )
        approval.supporting_facts = {"draft": "Changed"}
        db.commit()
        approval_id = approval.id
    response = client.post(
        f"/approvals/{approval_id}/decision",
        data={"csrf_token": token, "decision": "approved"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.status == "pending"
        assert approval.execution_status == "not_started"


def test_order_financial_state_change_invalidates_support_approval():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 462, "send_reply", {"draft": "Reviewed"}
        )
        case = db.get(Case, 462)
        db.get(Order, case.order_id).financial_status = "refunded"
        db.flush()
        assert any(
            "Order financial status changed" in message
            for message in approval_freshness_differences(db, approval)
        )
        with pytest.raises(ApprovalEvidenceStale):
            decide_approval(db, approval.id, "approved", "operator")


def test_stale_pending_approval_can_be_replaced_without_rewriting_history():
    with TestingSession() as db:
        original = create_approval(
            db, "support", "case", 460, "send_reply", {"draft": "Original reviewed draft"}
        )
        original_hash = original.evidence_hash
        original_facts = dict(original.supporting_facts)
        case = db.get(Case, 460)
        case.draft = "Current grounded response"
        db.flush()
        assert original.evidence_hash == original_hash
        assert original.evidence_hash == original_hash  # Hash is intact; source is stale.
        assert "Draft changed since review." in approval_freshness_differences(db, original)
        replacement = refresh_approval(db, original.id, "operator")
        assert replacement.id != original.id
        assert replacement.revision == 2 and replacement.status == "pending"
        assert original.status == "superseded"
        assert original.replacement_approval_id == replacement.id
        assert original.supporting_facts == original_facts
        assert original.evidence_hash == original_hash
        assert replacement.supporting_facts["draft"] == case.draft
        assert refresh_approval(db, original.id, "operator").id == replacement.id


def test_replacement_revision_increments_when_reviewed_action_changes():
    with TestingSession() as db:
        original = create_approval(
            db,
            "exception",
            "case",
            469,
            "review_refund_request",
            {"category": "refund", "policy": settings.store_policies["refund"]},
        )
        case = db.get(Case, 469)
        case.category = "cancellation"
        case.draft = "Updated cancellation response"
        db.flush()
        replacement = refresh_approval(db, original.id, "operator")
        assert replacement.proposed_action == "review_cancellation_request"
        assert replacement.revision == 2
        assert original.replacement_approval_id == replacement.id


def test_approved_stale_evidence_is_blocked_and_preserves_attempt_history(monkeypatch):
    monkeypatch.setattr(settings, "mock_action_failures", 1)
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 461, "send_reply", {"draft": "Reviewed"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        decision_at = approval.decision_at
        actor = approval.actor
        run_due(db, approval)
        assert approval.execution_status == "retryable" and approval.attempts == 1
        db.get(Case, 461).message = "A materially different current request"
        db.commit()
        run_due(db, approval)
        assert approval.execution_status == "evidence_changed"
        prior_execution = db.scalar(select(ActionExecution))
        assert prior_execution and prior_execution.attempts == 1
        replacement = refresh_approval(db, approval.id, "operator")
        assert replacement.status == "pending" and replacement.revision == 2
        assert approval.status == "approved" and as_utc(approval.decision_at) == as_utc(decision_at)
        assert approval.actor == actor and approval.attempts == 1
        assert db.get(ActionExecution, prior_execution.id).attempts == 1


def test_legacy_approval_can_be_replaced_without_rewriting_its_decision():
    with TestingSession() as db:
        order = Order(
            shop_domain="legacy.test", external_id="legacy-approval-order",
            order_number="#LEGACY", email="legacy@example.test", currency="USD",
            total="20.00", fulfillment_status="unfulfilled", financial_status="paid",
            created_at=utcnow() - timedelta(days=2), source_updated_at=utcnow(), payload={},
        )
        db.add(order); db.flush()
        case = Case(
            order_id=order.id, order_ref=order.order_number, customer_email=order.email,
            message="Legacy historical request", category="order_status", confidence=80,
            draft="Legacy historical draft", status="needs_review",
        )
        db.add(case); db.flush()
        legacy = Approval(
            kind="support", subject_type="case", subject_id=case.id,
            proposed_action="send_reply", supporting_facts={"draft":"Legacy historical draft"},
            evidence_hash=None, status="approved", actor="legacy-operator",
            decision_at=utcnow(), execution_status="evidence_changed", attempts=2,
        )
        db.add(legacy); db.flush()
        previous_decision_at = legacy.decision_at
        previous_facts = dict(legacy.supporting_facts)
        replacement = refresh_approval(db, legacy.id, "operator")
        assert replacement.revision == 2 and replacement.status == "pending"
        assert replacement.evidence_hash
        assert legacy.status == "approved" and legacy.actor == "legacy-operator"
        assert as_utc(legacy.decision_at) == as_utc(previous_decision_at)
        assert legacy.attempts == 2 and legacy.supporting_facts == previous_facts


def test_approval_freshness_detects_subsecond_source_timestamp_change():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 466, "send_reply", {"draft": "Reviewed"}
        )
        case = db.get(Case, 466)
        order = db.get(Order, case.order_id)
        order.source_updated_at = order.source_updated_at + timedelta(microseconds=1)
        db.flush()
        assert any(
            "Order source updated at changed" in item
            for item in approval_freshness_differences(db, approval)
        )


def test_replaced_stale_revision_leaves_actionable_recovery_queue_and_counts():
    sign_in()
    with TestingSession() as db:
        original = create_approval(
            db, "support", "case", 467, "send_reply", {"draft": "Old draft"}
        )
        original.status = "approved"
        original.execution_status = "evidence_changed"
        db.get(Case, 467).draft = "Updated grounded draft"
        db.flush()
        replacement = refresh_approval(db, original.id, "operator")
        original_id = original.id
        replacement_id = replacement.id
    stale_queue = client.get("/queues/failures?status=stale")
    dashboard = client.get("/")
    assert stale_queue.status_code == 200
    assert f"#{original_id}" not in stale_queue.text
    assert f"#{replacement_id}" not in stale_queue.text
    assert "Stale evidence 0" in dashboard.text
    record = client.get(f"/approvals/{replacement_id}")
    assert f"approval #{original_id}" in record.text
    assert "Approval revision history" in record.text


def test_action_recovery_filters_separate_scheduled_stale_and_uncertain_states():
    sign_in()
    ids = {}
    with TestingSession() as db:
        for offset, state in enumerate(("retryable", "evidence_changed", "ambiguous", "exhausted")):
            approval = create_approval(
                db,
                "exception",
                "order",
                480 + offset,
                "review_delayed_order",
                {},
            )
            approval.status = "approved"
            approval.execution_status = state
            approval.next_attempt_at = (
                utcnow() + timedelta(minutes=4) if state == "retryable" else None
            )
            ids[state] = approval.id
        db.commit()
    for filter_name, expected in (
        ("scheduled", "retryable"),
        ("stale", "evidence_changed"),
        ("uncertain", "ambiguous"),
        ("exhausted", "exhausted"),
    ):
        response = client.get(f"/queues/failures?status={filter_name}")
        assert response.status_code == 200
        assert f"#{ids[expected]} · exception" in response.text
        assert all(f"#{record_id} · exception" not in response.text for state, record_id in ids.items() if state != expected)


def test_preview_approval_persists_local_mock_result_without_workflow(monkeypatch):
    token = sign_in()
    monkeypatch.setattr(settings, "preview_mode", True)
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 468, "send_reply", {"draft": "Preview draft"}
        )
        approval_id = approval.id
        db.commit()
    response = client.post(
        f"/approvals/{approval_id}/decision",
        data={"csrf_token": token, "decision": "approved"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        execution = db.scalar(
            select(ActionExecution).where(
                ActionExecution.idempotency_key == f"approval:{approval_id}"
            )
        )
        assert approval.status == "approved"
        assert approval.execution_status == "succeeded"
        assert execution and execution.state == "succeeded" and execution.attempts == 1
    detail = client.get(response.headers["location"])
    assert "Mock action completed" in detail.text
    monkeypatch.setattr(settings, "preview_mode", False)


def test_unregistered_action_contract_is_blocked():
    with TestingSession() as db:
        approval = create_approval(
            db, "support", "case", 47, "delete_order", {"reason": "invalid"}
        )
        decide_approval(db, approval.id, "approved", "operator")
        run_due(db, approval)
        assert approval.execution_status == "invalid_action"
        assert db.scalar(select(ActionExecution)) is None


def test_unauthorized_operator_cannot_approve():
    assert (
        client.post(
            "/approvals/1/decision",
            data={"decision": "approved", "csrf_token": "x"},
            follow_redirects=False,
        ).status_code
        == 401
    )


def test_configured_openai_response_uses_structured_triage_and_is_still_approval_gated(
    monkeypatch,
):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "category": "refund",
                                        "confidence": 72,
                                        "draft": "Thanks for reaching out.",
                                        "missing_info": "",
                                    }
                                ),
                            }
                        ],
                    }
                ]
            }

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(settings, "llm_mode", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "mock_action_ambiguous", False)
    monkeypatch.setattr("app.integrations.httpx.post", fake_post)
    with TestingSession() as db:
        db.add(
            Order(
                shop_domain="demo.myshopify.com",
                external_id="9",
                order_number="#9",
                email="buyer@example.test",
                currency="USD",
                total="20.00",
                fulfillment_status="unfulfilled",
                source_updated_at=utcnow(),
                created_at=utcnow(),
                payload={},
            )
        )
        db.commit()
        case = create_support_case(db, "Please refund me", "#9", "buyer@example.test")
        assert calls and calls[0][0] == "https://api.openai.com/v1/responses"
        assert (
            case.category == "refund"
            and "Refund requests are reviewed by an operator" in case.draft
            and "order" in case.draft.lower()
        )
        assert case.order_id is not None
        approvals = db.scalars(
            select(Approval).where(
                Approval.subject_type == "case", Approval.subject_id == case.id
            )
        ).all()
        assert {a.proposed_action for a in approvals} == {
            "send_reply",
            "review_refund_request",
        }
        assert all(
            a.status == "pending" and a.execution_status == "not_started"
            for a in approvals
        )


def test_exhausted_delivery_visible_and_manual_retry(monkeypatch):
    monkeypatch.setattr(settings, "max_delivery_attempts", 3)
    with TestingSession() as db:
        delivery = Delivery(
            webhook_id="poison",
            topic="orders/create",
            shop_domain="demo.myshopify.com",
            payload={"name": "missing id"},
            status="received",
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        for _ in range(3):
            delivery.next_attempt_at = None
            db.commit()
            result = process_delivery(db, delivery.id)
        assert result["status"] == "failed"
        assert delivery.status == "exhausted" and delivery.attempts == 3
        delivery.payload = {
            "id": "recovered-1",
            "name": "#RECOVERED",
            "updated_at": "2026-10-01T00:00:00Z",
        }
        delivery.status = "received"
        delivery.next_attempt_at = None
        db.commit()
        result = process_delivery(db, delivery.id)
        assert (
            result["status"] == "processed"
            and delivery.status == "processed"
            and delivery.attempts == 4
        )
        assert (
            db.scalar(select(Order).where(Order.external_id == "recovered-1"))
            is not None
        )


def test_delivery_retry_requires_operator():
    assert (
        client.post(
            "/deliveries/1/retry", data={"csrf_token": "x"}, follow_redirects=False
        ).status_code
        == 401
    )


def test_authenticated_dashboard_and_failed_delivery_detail_render():
    import re

    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
    signed = client.post(
        "/login",
        data={
            "username": settings.operator_username,
            "password": settings.operator_password,
            "csrf_token": token,
        },
        follow_redirects=True,
    )
    assert signed.status_code == 200 and "Commerce operations" in signed.text
    with TestingSession() as db:
        delivery = Delivery(
            webhook_id="visible-failure",
            topic="orders/create",
            shop_domain="demo.myshopify.com",
            payload={},
            status="exhausted",
            attempts=3,
            error="ValueError",
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        delivery_id = delivery.id
    dashboard = client.get("/")
    detail = client.get(f"/deliveries/{delivery_id}")
    assert (
        "Receipt to persistence" in dashboard.text
        and "visible-failure" in client.get("/queues/deliveries?status=exhausted").text
    )
    assert detail.status_code == 200 and "Retry delivery" in detail.text


def sign_in():
    import re

    token = re.search(
        r'name="csrf_token" value="([^"]+)', client.get("/login").text
    ).group(1)
    result = client.post(
        "/login",
        data={
            "username": settings.operator_username,
            "password": settings.operator_password,
            "csrf_token": token,
        },
    )
    assert result.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)', result.text).group(1)


def test_executor_rejects_unapproved_and_rejected_records():
    for state in ("pending", "rejected"):
        with TestingSession() as db:
            a = create_approval(
                db, "support", "case", 1 if state == "pending" else 2, "send_reply", {}
            )
            a.status = state
            db.commit()
            with pytest.raises(PermissionError):
                execute_approved(db, a)
            assert db.scalar(select(ActionExecution)) is None


def test_form_queues_idempotently_and_n8n_triages():
    token = sign_in()
    fields = {
        "csrf_token": token,
        "request_key": "same-inquiry",
        "message": "Where is my order?",
        "order_ref": "unknown",
        "email": "x@example.test",
    }
    first = client.post("/support", data=fields, follow_redirects=False)
    assert first.status_code == 303
    again = client.post("/support", data=fields, follow_redirects=False)
    assert first.headers["location"] == again.headers["location"]
    conflicting = client.post(
        "/support",
        data={**fields, "message": "Please refund my order."},
        follow_redirects=False,
    )
    assert conflicting.status_code == 409
    with TestingSession() as db:
        from app.models import Case

        assert len(db.scalars(select(Case)).all()) == 1
        assert db.scalar(select(Case)).status == "queued"
        assert db.scalar(select(Approval)) is None
    assert client.post("/api/n8n/triage-next").status_code == 401
    response = client.post(
        "/api/n8n/triage-next", headers={"X-N8N-Secret": settings.n8n_webhook_secret}
    )
    assert response.json()["status"] == "needs_review"
    assert "Verified order reference" in client.get(first.headers["location"]).text


def test_guided_demo_requires_auth_and_csrf_and_runs_n8n(monkeypatch):
    import re
    from types import SimpleNamespace

    assert client.get("/demo", follow_redirects=False).status_code == 401
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)', login_page.text).group(1)
    assert client.post("/demo/start", data={"csrf_token": token}).status_code == 401

    token = sign_in()
    assert client.post("/demo/start", data={"csrf_token": "wrong"}).status_code == 403
    started = client.post("/demo/start", data={"csrf_token": token}, follow_redirects=False)
    assert started.status_code == 303
    run_url = started.headers["location"]
    intro = client.get("/demo", follow_redirects=False)
    assert intro.status_code == 200 and "Start guided demo" in intro.text
    another = client.post("/demo/start", data={"csrf_token": token}, follow_redirects=False)
    assert another.status_code == 303 and another.headers["location"] != run_url
    page = client.get(run_url)
    assert page.status_code == 200
    assert "Queued for n8n" in page.text
    assert "No real customer actions" in page.text
    delivery_id = int(run_url.rsplit("/", 1)[1])
    with TestingSession() as db:
        delivery = db.get(Delivery, delivery_id)
        assert delivery is not None and delivery.status == "received"
        correlation_id = delivery.webhook_id

    calls = []

    def fake_post(url, timeout):
        calls.append((url, timeout))
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(main.httpx, "post", fake_post)
    result = client.post(
        f"{run_url}/run/order", data={"csrf_token": token}, follow_redirects=False
    )
    assert result.status_code == 303
    assert calls == [(f"{settings.n8n_base_url}/webhook/commerceops/orders/process", 15)]
    with TestingSession() as db:
        from app.models import Audit

        event = db.scalar(
            select(Audit).where(Audit.event == "guided_demo_triggered").order_by(Audit.id.desc())
        )
        assert event.correlation_id == correlation_id
    assert client.post(
        f"{run_url}/run/order", data={"csrf_token": "wrong"}
    ).status_code == 403


def test_support_request_key_conflict_in_service():
    from app.services import queue_support

    with TestingSession() as db:
        first = queue_support(
            db, "Where is my order?", "#X", "a@example.test", "request-1"
        )
        again = queue_support(
            db, "Where is my order?", "#X", "A@example.test", "request-1"
        )
        assert first.id == again.id
        with pytest.raises(SupportRequestKeyConflict):
            queue_support(
                db, "Refund me", "#X", "a@example.test", "request-1"
            )


def test_configuration_rejects_invalid_mock_and_retry_limits():
    from pydantic import ValidationError
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(mock_action_failures=11)
    with pytest.raises(ValidationError):
        Settings(order_delay_hours=0)


def test_csrf_rejects_approval_and_retry_and_demo_mutation():
    sign_in()
    for endpoint, data in [
        ("/approvals/1/decision", {"decision": "approved"}),
        ("/approvals/1/retry", {}),
        ("/api/demo/order", {}),
    ]:
        assert (
            client.post(endpoint, data={**data, "csrf_token": "wrong"}).status_code
            == 403
        )


def test_llm_cannot_erase_required_missing_information(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(
        "app.services.generate_support_content",
        lambda *args: ("damaged_item", 30, "Safe draft", ""),
    )
    with TestingSession() as db:
        case = create_support_case(db, "broken item", "unknown", "x@example.test")
        assert all(
            flag in case.missing_info
            for flag in [
                "Verified order reference",
                "Photo or item details",
                "Uncertain classification",
            ]
        )


def test_search_filter_pagination_and_unknown_page():
    sign_in()
    with TestingSession() as db:
        for i in range(15):
            db.add(
                Order(
                    external_id=f"page-{i}",
                    order_number=f"#PAGE-{i}",
                    email="buyer@example.test",
                    fulfillment_status="fulfilled",
                    payload={},
                )
            )
        db.commit()
    first = client.get("/queues/orders?q=PAGE&status=fulfilled")
    second = client.get("/queues/orders?q=PAGE&status=fulfilled&page=2")
    assert first.status_code == second.status_code == 200
    assert "Page 1 of 2" in first.text and "Page 2 of 2" in second.text
    assert "No matching records" in client.get("/queues/orders?q=absent").text
    assert client.get("/queues/orders?page=0").status_code == 422
    assert client.get("/queues/unknown").status_code == 404


def test_queue_context_survives_detail_navigation_and_approval_facts_are_grouped():
    sign_in()
    with TestingSession() as db:
        order = Order(
            external_id="context-1", order_number="#CONTEXT-1", payload={}
        )
        db.add(order)
        db.flush()
        approval = create_approval(
            db,
            "support",
            "order",
            order.id,
            "send_reply",
            {
                "order_number": order.order_number,
                "category": "order_status",
                "confidence": 80,
                "draft": "A grounded draft.",
                "missing_info": "Verified order reference",
            },
        )
        db.commit()
        order_id, approval_id = order.id, approval.id

    queue = client.get("/queues/orders?q=CONTEXT&status=unfulfilled&page=1")
    assert f"/orders/{order_id}?from_queue=orders&amp;q=CONTEXT&amp;status=unfulfilled&amp;page=1" in queue.text
    detail = client.get(
        f"/approvals/{approval_id}?from_queue=approvals&q=send_reply&status=pending&page=2"
    )
    assert 'href="/queues/approvals?q=send_reply&amp;status=pending&amp;page=2"' in detail.text
    assert "Verified order context" in detail.text
    assert "Triage assessment" in detail.text
    assert "Draft response" in detail.text and "A grounded draft." in detail.text
    assert "Information gaps" in detail.text


def test_approval_snapshots_request_policy_and_delay_evidence():
    sign_in()
    with TestingSession() as db:
        order = Order(
            external_id="snapshot-1", order_number="#SNAPSHOT-1",
            email="buyer@example.test", fulfillment_status="unfulfilled",
            created_at=utcnow() - timedelta(hours=72), payload={},
        )
        db.add(order)
        db.commit()
        support = create_support_case(
            db, "Where is my order?", order.order_number, order.email
        )
        approval = db.scalar(select(Approval).where(
            Approval.subject_type == "case", Approval.subject_id == support.id,
            Approval.proposed_action == "send_reply",
        ))
        assert approval.supporting_facts["customer_message"] == "Where is my order?"
        assert approval.supporting_facts["policy"] == settings.store_policies["order_status"]
        support_id = approval.id
        order_id = order.id
    support_page = client.get(f"/approvals/{support_id}").text
    assert "Captured when this approval was created" in support_page
    assert "Where is my order?" in support_page
    assert settings.store_policies["order_status"] in support_page

    response = client.post(
        "/api/n8n/delayed-exceptions",
        headers={"X-N8N-Secret": settings.n8n_webhook_secret},
    )
    assert response.status_code == 200
    with TestingSession() as db:
        delayed = db.scalar(select(Approval).where(
            Approval.subject_type == "order", Approval.subject_id == order_id,
            Approval.proposed_action == "review_delayed_order",
        ))
        assert delayed.supporting_facts["hours_threshold"] == settings.order_delay_hours
        delay_id = delayed.id
    delay_page = client.get(f"/approvals/{delay_id}").text
    assert "Applicable delay rule" in delay_page
    assert f"{settings.order_delay_hours} hours" in delay_page
    assert "Related support draft" not in delay_page


def test_navigation_survives_decision_retry_and_related_links():
    token = sign_in()
    with TestingSession() as db:
        order = Order(external_id="nav-1", order_number="#NAV-1", payload={})
        db.add(order)
        db.flush()
        approval = create_approval(
            db, "exception", "order", order.id, "review_delayed_order",
            {"order_number": order.order_number, "hours_threshold": 48},
        )
        db.commit()
        approval_id, order_id = approval.id, order.id
    context = "?from_queue=approvals&q=review&status=pending&page=1"
    detail = client.get(f"/approvals/{approval_id}{context}").text
    assert f"/orders/{order_id}?from_queue=approvals&amp;q=review&amp;status=pending&amp;page=1" in detail
    response = client.post(
        f"/approvals/{approval_id}/decision{context}",
        data={"csrf_token": token, "decision": "approved"}, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/approvals/{approval_id}{context}"
    assert 'href="/queues/approvals?q=review&amp;status=pending&amp;page=1"' in client.get(response.headers["location"]).text

    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        approval.execution_status = "exhausted"
        approval.next_attempt_at = None
        db.commit()
    failure_context = "?from_queue=failures&q=review&status=exhausted&page=1"
    failure_page = client.get(f"/approvals/{approval_id}{failure_context}").text
    assert "Eligible on the next worker run after requeue" in failure_page
    retried = client.post(
        f"/approvals/{approval_id}/retry{failure_context}",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert retried.headers["location"] == f"/approvals/{approval_id}{failure_context}"
    changed_context = "?from_queue=failures&q=review&status=evidence_changed&page=1"
    changed_page = client.get(f"/approvals/{approval_id}{changed_context}").text
    assert 'href="/queues/failures?q=review&amp;status=evidence_changed&amp;page=1"' in changed_page
    invalid = client.get(f"/approvals/{approval_id}?from_queue=//evil.test&status=exhausted&page=-1").text
    assert 'href="/queues/approvals"' in invalid


def test_guided_rejection_stops_outcome_and_links_back_to_run():
    from app.models import Case

    token = sign_in()
    started = client.post(
        "/demo/start", data={"csrf_token": token}, follow_redirects=False
    )
    delivery_id = int(started.headers["location"].rsplit("/", 1)[1])
    with TestingSession() as db:
        delivery = db.get(Delivery, delivery_id)
        delivery.status = "processed"
        order = Order(
            shop_domain=delivery.shop_domain,
            external_id=str(delivery.payload["id"]),
            order_number=delivery.payload["name"], payload={},
        )
        db.add(order)
        db.flush()
        create_approval(
            db, "exception", "order", order.id, "review_delayed_order",
            {"order_number": order.order_number},
        )
        case = Case(
            message="Where is my order?", status="needs_review",
            request_key=f"guided-support:{delivery_id}", order_id=order.id,
        )
        db.add(case)
        db.flush()
        reply = create_approval(
            db, "support", "case", case.id, "send_reply",
            {"order_number": order.order_number,
             "correlation_id": f"guided-support:{delivery_id}"},
        )
        db.commit()
        reply_id = reply.id
    rejected = client.post(
        f"/approvals/{reply_id}/decision?from_demo={delivery_id}",
        data={"csrf_token": token, "decision": "rejected"},
        follow_redirects=False,
    )
    assert rejected.headers["location"] == f"/approvals/{reply_id}?from_demo={delivery_id}"
    assert f'href="/demo/{delivery_id}"' in client.get(rejected.headers["location"]).text
    demo = client.get(f"/demo/{delivery_id}").text
    assert "Stopped · approval rejected" in demo
    assert "No mock reply will run" in demo


def test_retry_after_lower_bound_and_cap():
    from app.services import retry_delay

    assert retry_delay(1, 120) >= 120
    assert retry_delay(50) == 3600


def test_failed_delivery_waits_until_due(monkeypatch):
    with TestingSession() as db:
        d = Delivery(
            webhook_id="not-due",
            topic="orders/create",
            shop_domain="demo.myshopify.com",
            payload={},
            status="failed",
            attempts=1,
            next_attempt_at=utcnow() + timedelta(hours=1),
        )
        db.add(d)
        db.commit()
    response = client.post(
        "/api/n8n/process-next", headers={"X-N8N-Secret": settings.n8n_webhook_secret}
    )
    assert response.json()["status"] == "idle"


def test_no_public_support_facts_or_demo_mutations():
    assert (
        client.post("/api/support", json={"message": "Where is my order?"}).status_code
        == 401
    )
    assert client.post("/api/demo/order", data={"csrf_token": "x"}).status_code == 401


def test_missing_source_timestamp_cannot_overwrite_order_and_created_time_is_stable():
    from app.services import normalize_order

    with pytest.raises(ValueError):
        normalize_order({"id": "unsafe-update"})
    with TestingSession() as db:
        created = utcnow() - timedelta(days=4)
        order = Order(
            external_id="stable-created",
            order_number="#STABLE",
            created_at=created,
            source_updated_at=created,
            payload={},
        )
        db.add(order)
        db.commit()
        d = Delivery(
            webhook_id="stable-update",
            topic="orders/updated",
            shop_domain="demo.myshopify.com",
            payload={
                "id": "stable-created",
                "updated_at": utcnow().isoformat(),
                "fulfillment_status": "fulfilled",
            },
        )
        db.add(d)
        db.commit()
        assert process_delivery(db, d.id)["status"] == "processed"
        assert as_utc(order.created_at) == as_utc(created)


def test_worker_cannot_retry_exhausted_delivery_without_requeue():
    with TestingSession() as db:
        d = Delivery(
            webhook_id="exhausted-guard",
            topic="orders/create",
            shop_domain="demo.myshopify.com",
            payload={"id": "no-auto", "updated_at": utcnow().isoformat()},
            status="exhausted",
            attempts=3,
        )
        db.add(d)
        db.commit()
        assert process_delivery(db, d.id)["status"] == "exhausted"
        assert d.attempts == 3


@pytest.mark.parametrize("kind", ["delivery", "case", "approval"])
def test_manual_requeue_preserves_backoff_and_repeated_click_is_safe(kind):
    from app.models import Case

    token = sign_in()
    deadline = utcnow() + timedelta(minutes=5)
    with TestingSession() as db:
        if kind == "delivery":
            row = Delivery(
                webhook_id="manual-delay",
                topic="orders/create",
                shop_domain="demo.myshopify.com",
                payload={},
                status="failed",
                attempts=1,
                next_attempt_at=deadline,
            )
            prefix = "deliveries"
        elif kind == "case":
            row = Case(
                message="Please help",
                status="failed",
                attempts=1,
                next_attempt_at=deadline,
            )
            prefix = "cases"
        else:
            row = Approval(
                kind="exception",
                subject_type="order",
                subject_id=99,
                proposed_action="review_delayed_order",
                status="approved",
                execution_status="retryable",
                attempts=1,
                next_attempt_at=deadline,
            )
            prefix = "approvals"
        db.add(row)
        db.commit()
        record_id = row.id
        model = type(row)
    endpoint = f"/{prefix}/{record_id}/retry"
    assert (
        client.post(
            endpoint, data={"csrf_token": token}, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            endpoint, data={"csrf_token": token}, follow_redirects=False
        ).status_code
        == 409
    )
    with TestingSession() as db:
        row = db.get(model, record_id)
        assert as_utc(row.next_attempt_at) == deadline
        assert row.attempts == 1
    worker = {
        "delivery": "process-next",
        "case": "triage-next",
        "approval": "delayed-exceptions",
    }[kind]
    result = client.post(
        "/api/n8n/" + worker, headers={"X-N8N-Secret": settings.n8n_webhook_secret}
    )
    assert result.status_code == 200
    with TestingSession() as db:
        assert db.get(model, record_id).attempts == 1


def test_exhausted_action_recovers_through_authenticated_route_and_worker():
    token = sign_in()
    with TestingSession() as db:
        a = create_approval(db, "exception", "order", 99, "review_delayed_order", {})
        a.status = "approved"
        a.execution_status = "exhausted"
        a.attempts = 3
        db.add(
            ActionExecution(
                idempotency_key=f"approval:{a.id}", state="exhausted", attempts=3
            )
        )
        db.commit()
        aid = a.id
    response = client.post(
        f"/approvals/{aid}/retry", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
    for _ in range(2):
        assert (
            client.post("/api/n8n/delayed-exceptions", headers=headers).status_code
            == 200
        )
    with TestingSession() as db:
        a = db.get(Approval, aid)
        executions = db.scalars(select(ActionExecution)).all()
        assert a.execution_status == "succeeded" and a.attempts == 4
        assert len(executions) == 1 and executions[0].attempts == 4


def test_ambiguous_action_cannot_be_manually_requeued():
    token = sign_in()
    with TestingSession() as db:
        a = create_approval(db, "exception", "order", 99, "review_delayed_order", {})
        a.status = "approved"
        a.execution_status = "ambiguous"
        a.attempts = 1
        db.commit()
        aid = a.id
    response = client.post(
        f"/approvals/{aid}/retry", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 409
    with TestingSession() as db:
        assert db.get(Approval, aid).execution_status == "ambiguous"


def test_dashboard_distinguishes_worker_failure_from_business_completion():
    from app.models import Audit, Case

    token = sign_in()
    with TestingSession() as db:
        db.add(Case(message="retry me", status="failed", attempts=1, next_attempt_at=utcnow() - timedelta(minutes=1)))
        db.add(Delivery(webhook_id="waiting", topic="orders/create", shop_domain="demo.myshopify.com", payload={}, status="received", received_at=utcnow() - timedelta(minutes=7)))
        db.commit()
        main.audit(db, "workflow", 2, "guided_demo_triggered")
        db.commit()
        assert main.workflow_activity(db)[1]["last"] is None
        main.heartbeat(db, 2, {"status": "failed"}, failed=True)
        activity = main.workflow_activity(db)[1]
        assert activity["last"].event == "work_failed"
        assert activity["success"] is None
    page = client.get("/")
    assert page.status_code == 200
    assert "Retries eligible now" in page.text
    assert "1</strong><span>Retries eligible now" in page.text
    assert "Oldest intake queue age" in page.text
    assert "Last worker failure" in page.text
    assert "items need recovery or monitoring" in page.text


def test_dashboard_recovery_counts_link_to_the_complete_matching_queues():
    from app.models import Case

    sign_in()
    with TestingSession() as db:
        failed_case = Case(message="retry support", status="failed", attempts=1)
        exhausted_case = Case(message="exhausted support", status="exhausted", attempts=3)
        db.add_all(
            [
                failed_case,
                exhausted_case,
                Delivery(
                    webhook_id="retry-delivery",
                    topic="orders/create",
                    shop_domain="demo.myshopify.com",
                    payload={},
                    status="failed",
                    attempts=1,
                ),
                Delivery(
                    webhook_id="exhausted-delivery",
                    topic="orders/create",
                    shop_domain="demo.myshopify.com",
                    payload={},
                    status="exhausted",
                    attempts=3,
                ),
            ]
        )
        db.commit()
        failed_case_id = failed_case.id
        exhausted_case_id = exhausted_case.id
    dashboard = client.get("/")
    recovery = client.get("/recovery")
    assert 'href="/recovery?category=eligible">Eligible 2</a>' in dashboard.text
    assert 'href="/recovery?category=exhausted">Exhausted 2</a>' in dashboard.text
    assert f"Support case #{failed_case_id}" in recovery.text
    assert f"Support case #{exhausted_case_id}" in recovery.text
    assert "retry-delivery" in recovery.text and "exhausted-delivery" in recovery.text


def test_recovery_workspace_preserves_category_search_and_page_context():
    sign_in()
    with TestingSession() as db:
        db.add(Case(message="find me", status="failed", attempts=1,
                    failure_detail="Distinct recovery reason"))
        db.commit()
        case_id = db.scalar(select(Case.id))
    response = client.get("/recovery?category=eligible&q=Distinct&page=1")
    assert response.status_code == 200
    assert f"/cases/{case_id}?from_queue=recovery" in response.text
    detail = client.get(f"/cases/{case_id}?from_queue=recovery&q=Distinct&status=eligible&page=1")
    assert 'href="/recovery?q=Distinct&amp;page=1&amp;category=eligible"' in detail.text


def test_recovery_workspace_prioritizes_uncertain_pending_then_oldest_actionable():
    from app.recovery import recovery_page

    now = utcnow()
    with TestingSession() as db:
        db.add_all([
            Approval(kind="support", subject_type="case", subject_id=701,
                     proposed_action="send_reply", status="approved", execution_status="ambiguous",
                     created_at=now - timedelta(days=1)),
            Approval(kind="support", subject_type="case", subject_id=702,
                     proposed_action="send_reply", status="pending", execution_status="not_started",
                     created_at=now - timedelta(days=4)),
            Case(message="waiting older", status="failed", attempts=1,
                 next_attempt_at=now + timedelta(minutes=4), created_at=now - timedelta(days=10)),
        ])
        db.commit()
        rows, total, _, _ = recovery_page(db, now=now)
    assert total == 3
    assert [row["category"] for row in rows] == ["uncertain", "pending", "waiting"]
    assert "Reconcile the outcome" in rows[0]["next_action"]


def test_recovery_marks_new_revision_blocked_when_prior_action_is_ambiguous():
    from app.recovery import recovery_page

    with TestingSession() as db:
        prior = Approval(kind="support", subject_type="case", subject_id=704,
                         proposed_action="send_reply", revision=1, status="approved",
                         execution_status="ambiguous")
        db.add(prior)
        db.flush()
        db.add(ActionExecution(idempotency_key=f"approval:{prior.id}", state="ambiguous", attempts=1))
        db.add(Approval(kind="exception", subject_type="case", subject_id=704,
                        proposed_action="review_refund_request", revision=2, status="pending"))
        db.commit()
        rows, _, _, _ = recovery_page(db, category="blocked")
    blocked = next(row for row in rows if row["label"].startswith("Approval #"))
    assert blocked["category"] == "blocked"
    assert blocked["prior_terminal"] is True
    assert "cannot act" in blocked["next_action"]


def test_preview_scenario_uses_request_scoped_outcome_without_mutating_settings(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    before = (settings.mock_action_failures, settings.mock_action_ambiguous, settings.max_action_attempts)
    with TestingSession() as db:
        approval = create_preview_scenario(db, "ambiguous")
        assert approval.execution_status == "ambiguous"
    assert before == (settings.mock_action_failures, settings.mock_action_ambiguous, settings.max_action_attempts)


def test_preview_exhaustion_recovery_runs_one_explicit_attempt_and_persists_result(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    monkeypatch.setattr(settings, "mock_action_failures", 0)
    monkeypatch.setattr(settings, "mock_action_ambiguous", False)
    token = sign_in()
    with TestingSession() as db:
        approval = create_preview_scenario(db, "exhausted")
        approval_id = approval.id
        budget = settings.max_action_attempts
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert approval.attempts == budget
        assert execution.state == "exhausted" and execution.attempts == budget
        assert db.scalar(select(func.count()).select_from(main.Audit).where(
            main.Audit.entity_type == "approval", main.Audit.entity_id == approval_id,
            main.Audit.event == "action_execution_attempt_exhausted",
        )) == budget
    detail = client.get(f"/approvals/{approval_id}")
    assert "Queue action retry" in detail.text
    queued = client.post(f"/approvals/{approval_id}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert queued.status_code == 303
    detail = client.get(queued.headers["location"])
    assert "Run due local mock attempt" in detail.text
    result = client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}, follow_redirects=False)
    assert result.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert approval.execution_status == "succeeded"
        assert approval.attempts == budget + 1
        assert execution.state == "succeeded" and execution.attempts == budget + 1


def test_preview_retry_rejects_attempt_before_persisted_backoff_deadline(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval = create_preview_scenario(db, "retryable")
        approval_id = approval.id
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert approval.next_attempt_at is not None
        before_attempts = execution.attempts
    queued = client.post(f"/approvals/{approval_id}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert queued.status_code == 303
    early = client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}, follow_redirects=False)
    assert early.status_code == 409
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert approval.execution_status == "queued"
        assert execution.state == "retryable" and execution.attempts == before_attempts


def test_freshness_comparison_shows_reviewed_and_current_draft_values():
    with TestingSession() as db:
        approval = create_approval(db, "support", "case", 991, "send_reply", {"draft": "Original reply"})
        case = db.get(Case, 991)
        case.draft = "Updated reply with current policy"
        db.flush()
        comparison = next(item for item in approval_freshness_comparison(db, approval) if item["field"] == "Draft")
        assert comparison["reviewed"] == "Original reply"
        assert comparison["current"] == "Updated reply with current policy"


def test_replaced_approval_cannot_execute_even_if_original_evidence_returns():
    with TestingSession() as db:
        original = create_approval(db, "support", "case", 992, "send_reply", {"draft": "Original"})
        original.status = "approved"
        original.execution_status = "evidence_changed"
        case = db.get(Case, original.subject_id)
        case.draft = "Changed after review"
        db.flush()
        replacement = refresh_approval(db, original.id, "operator")
        case.draft = "Original"
        db.flush()
        with pytest.raises(ValueError, match="replaced"):
            queue_approval_retry(db, original.id, "operator")
        execute_approved(db, original)
        db.commit()
        assert original.replacement_approval_id == replacement.id
        assert original.execution_status == "superseded"
        assert db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{original.id}"
        )) is None


def test_terminal_execution_blocks_different_approval_kind_for_same_case():
    with TestingSession() as db:
        original = create_approval(db, "support", "case", 993, "send_reply", {
            "draft": "first", "category": "refund",
        })
        decide_approval(db, original.id, "approved", "operator")
        execute_approved(db, original, mock_outcome="ambiguous")
        case = db.get(Case, original.subject_id)
        alternate = Approval(
            kind="exception", subject_type="case", subject_id=case.id,
            proposed_action="review_refund_request", status="approved",
            execution_status="queued", supporting_facts={},
        )
        db.add(alternate)
        db.flush()
        execute_approved(db, alternate)
        db.commit()
        assert alternate.execution_status == "blocked_prior_outcome"
        assert db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{alternate.id}"
        )) is None


def test_completed_prior_execution_guidance_does_not_offer_impossible_reconciliation():
    from app.recovery import recovery_page

    sign_in()
    with TestingSession() as db:
        original = create_approval(db, "support", "case", 994, "send_reply", {"draft": "first"})
        decide_approval(db, original.id, "approved", "operator")
        run_due(db, original)
        alternate = Approval(kind="exception", subject_type="case", subject_id=original.subject_id,
                             proposed_action="review_refund_request", status="approved",
                             execution_status="queued", supporting_facts={})
        db.add(alternate)
        db.flush()
        execute_approved(db, alternate)
        db.commit()
        original_id, alternate_id = original.id, alternate.id
        assert alternate.execution_status == "blocked_prior_outcome"
        rows, _, _, _ = recovery_page(db, category="blocked")
        item = next(row for row in rows if row["record_id"] == alternate_id)
        assert item["next_action"] == "Inspect the related execution; this revision cannot act"
    page = client.get(f"/approvals/{alternate_id}")
    assert "The related mock action already completed" in page.text
    assert f"/approvals/{original_id}" in page.text
    assert "Review and reconcile the uncertain execution" not in page.text


def test_operator_confirmed_completion_is_not_described_as_unresolved():
    sign_in()
    original_id = make_ambiguous_approval(995)
    with TestingSession() as db:
        reconcile_approval_execution(db, original_id, "confirmed_completed",
            "The operator reviewed evidence that the action completed.", "CONFIRMED-995", "operator")
        original = db.get(Approval, original_id)
        alternate = Approval(kind="exception", subject_type="case", subject_id=original.subject_id,
                             proposed_action="review_refund_request", status="approved",
                             execution_status="queued", supporting_facts={})
        db.add(alternate)
        db.flush()
        execute_approved(db, alternate)
        db.commit()
        alternate_id = alternate.id
    page = client.get(f"/approvals/{alternate_id}")
    assert "An operator recorded the related execution as completed" in page.text
    assert "Review and reconcile the uncertain execution" not in page.text


def test_terminal_execution_on_prior_revision_blocks_new_revision():
    with TestingSession() as db:
        original = create_approval(db, "support", "case", 990, "send_reply", {"draft": "first"})
        decide_approval(db, original.id, "approved", "operator")
        execute_approved(db, original, mock_outcome="ambiguous")
        db.commit()
        replacement = service_create_approval(
            db, original.kind, original.subject_type, original.subject_id,
            original.proposed_action, dict(original.supporting_facts), revision=2,
        )
        decide_approval(db, replacement.id, "approved", "operator")
        execute_approved(db, replacement)
        db.commit()
        assert replacement.execution_status == "blocked_prior_outcome"
        assert db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{replacement.id}"
        )) is None


def test_guided_demo_does_not_present_failed_triage_as_complete():
    from app.models import Case

    token = sign_in()
    started = client.post("/demo/start", data={"csrf_token": token}, follow_redirects=False)
    delivery_id = int(started.headers["location"].rsplit("/", 1)[1])
    with TestingSession() as db:
        delivery = db.get(Delivery, delivery_id)
        delivery.status = "processed"
        order = Order(shop_domain="demo.myshopify.com", external_id=delivery.payload["id"], order_number=delivery.payload["name"], email=delivery.payload["email"], payload=delivery.payload)
        db.add(order)
        db.flush()
        create_approval(db, "exception", "order", order.id, "review_delayed_order", {})
        db.add(Case(message="Where is my order?", status="failed", request_key=f"guided-support:{delivery_id}", failure_detail="SimulatedFailure"))
        db.commit()
    page = client.get(f"/demo/{delivery_id}")
    assert page.status_code == 200
    assert "Triage did not complete" in page.text
    assert "inspect the failure" in page.text


def test_reconciliation_appends_unknown_then_definite_evidence_and_preserves_history():
    approval_id = make_ambiguous_approval(2191)
    with TestingSession() as db:
        first = reconcile_approval_execution(
            db, approval_id, "still_unknown",
            "The first review cannot establish the provider outcome.", "", "operator", synthetic=True,
        )
        first_id = first.id
        second = reconcile_approval_execution(
            db, approval_id, "confirmed_not_executed",
            "A later ledger review confirms no action was completed.", "SYNTH-LATER-2191", "operator2", synthetic=True,
        )
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert second.revision == 2 and second.previous_reconciliation_id == first_id
        assert second.outcome == "confirmed_not_executed"
        assert execution.state == "ambiguous" and execution.attempts == 1
        assert db.get(Approval, approval_id).execution_status == "reconciled_not_executed"
        rows = db.scalars(select(ExecutionReconciliation).where(
            ExecutionReconciliation.approval_id == approval_id
        ).order_by(ExecutionReconciliation.revision)).all()
        assert [(r.revision, r.outcome, r.actor) for r in rows] == [
            (1, "still_unknown", "operator"), (2, "confirmed_not_executed", "operator2")
        ]


def test_conflicting_definite_reconciliation_is_visible_and_can_be_resolved():
    approval_id = make_ambiguous_approval(2192)
    with TestingSession() as db:
        first = reconcile_approval_execution(
            db, approval_id, "confirmed_completed",
            "First source indicates the action was completed.", "SYNTH-COMPLETE-2192", "operator", synthetic=True,
        )
        conflict = reconcile_approval_execution(
            db, approval_id, "confirmed_not_executed",
            "A second source contradicts the first conclusion.", "SYNTH-NOEXEC-2192", "operator2", synthetic=True,
        )
        assert conflict.revision == 2 and conflict.previous_reconciliation_id == first.id
        assert conflict.outcome == "conflicting_evidence"
        assert conflict.asserted_outcome == "confirmed_not_executed"
        assert db.get(Approval, approval_id).execution_status == "disputed"
        resolved = reconcile_approval_execution(
            db, approval_id, "confirmed_not_executed",
            "A final reviewed ledger supports the second conclusion.", "SYNTH-FINAL-2192", "operator3", synthetic=True,
        )
        assert resolved.revision == 3 and resolved.previous_reconciliation_id == conflict.id
        assert resolved.outcome == "confirmed_not_executed"
        assert db.get(Approval, approval_id).execution_status == "reconciled_not_executed"
        assert db.scalar(select(func.count()).select_from(ExecutionReconciliation).where(
            ExecutionReconciliation.approval_id == approval_id
        )) == 3


def test_preview_scenario_creation_starts_pending_then_applies_named_mock_outcome(monkeypatch):
    from app.preview_scenarios import start_preview_scenario, execute_preview_approval
    monkeypatch.setattr(settings, "preview_mode", True)
    with TestingSession() as db:
        approval = start_preview_scenario(db, "retryable", "operator")
        approval_id = approval.id
        assert approval.status == "pending" and approval.execution_status == "not_started"
        assert db.scalar(select(ActionExecution)) is None
        decide_approval(db, approval_id, "approved", "operator")
        execute_preview_approval(db, approval, "operator")
        assert approval.execution_status == "retryable"
        assert approval.attempts == 1
        assert db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        )).state == "retryable"


def test_preview_scenario_paginates_histories_without_hiding_current_state(monkeypatch):
    from app.preview_scenarios import start_preview_scenario, load_preview_scenario, preview_scenario_contains
    from app.models import Audit
    from app.services import audit

    monkeypatch.setattr(settings, "preview_mode", True)
    with TestingSession() as db:
        first = start_preview_scenario(db, "ambiguous", "operator")
        original_id = first.id
        previous = first
        for revision in range(2, 22):
            previous.status = "superseded"
            replacement = create_approval(
                db, previous.kind, previous.subject_type, previous.subject_id,
                previous.proposed_action, previous.supporting_facts, revision=revision,
            )
            previous.replacement_approval_id = replacement.id
            previous = replacement
        current = previous
        decide_approval(db, current.id, "approved", "operator")
        execute_approved(db, current, mock_outcome="ambiguous")
        db.commit()
        for index in range(21):
            reconcile_approval_execution(
                db, current.id, "still_unknown",
                f"Follow-up evidence review {index} leaves the result uncertain.",
                "", f"operator-{index}", synthetic=True,
            )
        correlation_id = current.supporting_facts["correlation_id"]
        for index in range(101):
            audit(db, "case", current.subject_id, "preview_history_marker", "operator",
                  details={"index": index}, correlation_id=correlation_id)
        db.commit()
        loaded = load_preview_scenario(
            db, original_id, reconciliation_page=2, revision_page=2, audit_page=2
        )
        assert loaded["approval_pages"] == 2 and loaded["approval_page"] == 2
        assert loaded["current"].id == current.id
        assert loaded["current"] in loaded["approvals"]
        assert loaded["reconciliation_pages"] == 2 and loaded["reconciliation_total"] == 21
        assert loaded["reconciliations"][current.id].revision == 21
        assert loaded["audit_pages"] == 2 and loaded["audit_page"] == 2
        assert len(loaded["events"]) == loaded["audit_total"] - 100
        assert any(event.event == "preview_history_marker" for event in loaded["events"])
        assert any(event.event == "approval_requested" for event in loaded["events"])
        assert preview_scenario_contains(db, original_id, "approval", current.id)


def test_stale_preview_run_explains_comparison_then_creates_pending_replacement(monkeypatch):
    from app.preview_scenarios import start_preview_scenario
    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval = start_preview_scenario(db, "stale", "operator")
        approval_id = approval.id
    scenario_page = client.get(f"/demo/scenarios/{approval_id}")
    assert "Compare the saved snapshot with current facts" in scenario_page.text
    assert "Evidence changed after this approval was created." in scenario_page.text
    approval_page = client.get(f"/approvals/{approval_id}?from_scenario={approval_id}")
    assert "Create replacement for current facts" in approval_page.text
    assert "New synthetic customer information arrived after the approval snapshot." in approval_page.text
    refreshed = client.post(
        f"/approvals/{approval_id}/refresh?from_scenario={approval_id}",
        data={"csrf_token": token}, follow_redirects=False,
    )
    assert refreshed.status_code == 303
    assert refreshed.headers["location"].startswith("/demo/scenarios/")
    with TestingSession() as db:
        original = db.get(Approval, approval_id)
        replacement = db.get(Approval, original.replacement_approval_id)
        assert original.status == "superseded"
        assert replacement.status == "pending" and replacement.execution_status == "not_started"
        assert replacement.evidence_hash != original.evidence_hash


def test_blocked_preview_scenario_requires_approval_and_never_executes(monkeypatch):
    from app.preview_scenarios import start_preview_scenario
    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval = start_preview_scenario(db, "blocked", "operator")
        assert approval.status == "pending"
        assert approval.proposed_action == "unsupported_demo_action"
        approval_id = approval.id
    approval_page = client.get(f"/approvals/{approval_id}?from_scenario={approval_id}")
    assert "Approve to demonstrate block" in approval_page.text
    assert "no action will run" in approval_page.text
    decision = client.post(f"/approvals/{approval_id}/decision?from_scenario={approval_id}",
                           data={"csrf_token": token, "decision": "approved"}, follow_redirects=False)
    assert decision.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.execution_status == "invalid_action"
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval.id}"
        ))
        assert execution is None
    page = client.get(f"/demo/scenarios/{approval_id}")
    assert "Blocked · no mock adapter is registered" in page.text
    assert "No execution was created because the action contract has no registered mock adapter." in page.text


def test_blocked_approval_links_blocker_and_requeues_after_confirmed_nonexecution():
    token = sign_in()
    with TestingSession() as db:
        facts = {
            "draft": "Reviewed refund response", "category": "refund", "confidence": 92,
            "customer_message": "Please review this refund request.",
            "policy": settings.store_policies["refund"], "missing_info": "",
        }
        blocker = create_approval(db, "support", "case", 2391, "send_reply", facts)
        decide_approval(db, blocker.id, "approved", "operator")
        execute_approved(db, blocker, mock_outcome="ambiguous")
        db.commit()
        alternate = service_create_approval(
            db, "exception", "case", blocker.subject_id, "review_refund_request",
            dict(blocker.supporting_facts), revision=2,
        )
        decide_approval(db, alternate.id, "approved", "operator")
        execute_approved(db, alternate)
        db.commit()
        blocker_id, alternate_id = blocker.id, alternate.id
        assert alternate.execution_status == "blocked_prior_outcome"

    page = client.get(f"/approvals/{alternate_id}")
    assert f"/approvals/{blocker_id}" in page.text
    assert "Blocking approval" in page.text

    with TestingSession() as db:
        reconcile_approval_execution(
            db, blocker_id, "confirmed_not_executed",
            "The synthetic ledger confirms the earlier mock did not complete.",
            "SYNTH-PRIOR-2391", "operator", synthetic=True,
        )
        alternate = queue_approval_retry(db, alternate_id, "operator")
        assert alternate.execution_status == "queued"
        execute_approved(db, alternate)
        db.commit()
        assert alternate.execution_status == "succeeded"
        original_execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{blocker_id}"
        ))
        assert original_execution.state == "ambiguous" and original_execution.attempts == 1


def test_new_evidence_on_replaced_execution_blocks_recovery_and_preserves_completion():
    from app.recovery import recovery_counts, recovery_page

    sign_in()
    original_id = make_ambiguous_approval(2401)
    with TestingSession() as db:
        reconcile_approval_execution(db, original_id, "confirmed_not_executed",
            "The first ledger review found no completed action.", "LEDGER-1", "operator")
        replacement = refresh_approval(db, original_id, "operator")
        replacement_id = replacement.id
        decide_approval(db, replacement_id, "approved", "operator")
        conflict = reconcile_approval_execution(db, original_id, "confirmed_completed",
            "New ledger evidence suggests the first action completed.", "LEDGER-2", "operator")
        assert conflict.outcome == "conflicting_evidence"
        assert recovery_counts(db)["uncertain"] == 1
        rows, _, _, _ = recovery_page(db, category="uncertain")
        assert any(row["record_id"] == original_id for row in rows)
        execute_approved(db, replacement)
        db.commit()
        assert replacement.execution_status == "blocked_prior_outcome"
        reconcile_approval_execution(db, original_id, "confirmed_not_executed",
            "A further review supports the original non-execution conclusion.", "LEDGER-3", "operator")
        queue_approval_retry(db, replacement_id, "operator")
        execute_approved(db, replacement)
        db.commit()
        assert replacement.execution_status == "succeeded" and replacement.attempts == 1
        reconcile_approval_execution(db, original_id, "confirmed_completed",
            "A later independent record again indicates completion.", "LEDGER-4", "operator")
        assert db.get(Approval, original_id).execution_status == "disputed"
        assert db.get(Approval, replacement_id).execution_status == "succeeded"
        assert recovery_counts(db)["uncertain"] == 1
        execute_approved(db, replacement)
        db.commit()
        assert replacement.execution_status == "succeeded" and replacement.attempts == 1
    page = client.get(f"/approvals/{original_id}?from_recovery=uncertain")
    assert "New evidence about this execution belongs on this original approval" in page.text
    assert "Append reconciliation" in page.text


def test_preview_ambiguity_is_injected_once_across_replacement(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        original_id = start_preview_scenario(db, "ambiguous", "operator").id
    decision = {"csrf_token": token, "decision": "approved"}
    assert client.post(f"/approvals/{original_id}/decision", data=decision).status_code == 200
    with TestingSession() as db:
        assert db.get(Approval, original_id).execution_status == "ambiguous"
        reconcile_approval_execution(db, original_id, "confirmed_not_executed",
            "The synthetic ledger confirms no action was completed.", "PREVIEW-1", "operator", synthetic=True)
        replacement_id = refresh_approval(db, original_id, "operator").id
    assert client.post(f"/approvals/{replacement_id}/decision", data=decision).status_code == 200
    with TestingSession() as db:
        assert db.get(Approval, replacement_id).execution_status == "succeeded"
        assert db.get(Approval, replacement_id).attempts == 1
        assert db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{original_id}")).state == "ambiguous"
        reconcile_approval_execution(db, original_id, "confirmed_completed",
            "New synthetic evidence contradicts the earlier ledger review.",
            "PREVIEW-2", "operator", synthetic=True)
    scenario_page = client.get(f"/demo/scenarios/{replacement_id}")
    assert "A related execution has unresolved evidence" in scenario_page.text
    assert f"/approvals/{original_id}?from_scenario={replacement_id}" in scenario_page.text
    assert "Related outcome needs review" in scenario_page.text
    assert "Related approval #" in scenario_page.text
    assert 'class="scenario-stage attention"' in scenario_page.text
    assert "Scenario complete · local mock succeeded" not in scenario_page.text
    listing = client.get('/demo')
    assert "A related approval has unresolved evidence" in listing.text
    assert f"review approval #{original_id}" in listing.text
    with TestingSession() as db:
        reconcile_approval_execution(db, original_id, "confirmed_completed",
            "A final synthetic review supports the completed conclusion.",
            "PREVIEW-3", "operator", synthetic=True)
    resolved = client.get(f"/demo/scenarios/{replacement_id}")
    assert "A related execution is recorded as completed or still in progress" in resolved.text
    assert "Related outcome needs review" in resolved.text
    assert f"inspect approval #{original_id} before further action" in client.get('/demo').text


def test_preview_workspace_pages_saved_scenarios_and_links_journey_events(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    sign_in()
    with TestingSession() as db:
        ids = [start_preview_scenario(db, "success", "operator").id for _ in range(14)]
        unrelated = create_approval(db, "support", "case", 100, "send_reply", {})
        db.commit()

    first = client.get("/demo")
    assert first.status_code == 200
    assert "14 saved scenarios" in first.text
    assert f'href="/demo/scenarios/{unrelated.id}"' not in first.text
    assert "Five-minute reviewer path" in first.text
    assert f'href="/demo/scenarios/{ids[-1]}"' in first.text
    assert f'href="/demo/scenarios/{ids[0]}"' not in first.text
    assert 'href="?page=2"' in first.text

    older = client.get("/demo?page=2")
    assert older.status_code == 200
    assert f'href="/demo/scenarios/{ids[0]}"' in older.text
    assert 'href="?page=1"' in older.text

    journey = client.get(f"/demo/scenarios/{ids[0]}")
    assert journey.status_code == 200
    assert "Journey history" in journey.text
    assert "Customer request saved" in journey.text
    assert "Human review requested" in journey.text
    assert f'/approvals/{ids[0]}?from_scenario={ids[0]}' in journey.text
    assert "Saved event evidence" in journey.text


def test_confirmed_nonexecution_keeps_guided_outcome_open_until_fresh_approval(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    sign_in()
    with TestingSession() as db:
        approval_id = create_preview_scenario(db, "ambiguous", "operator").id
        reconcile_approval_execution(
            db, approval_id, "confirmed_not_executed",
            "Synthetic ledger shows no completed action for this request.",
            "LEDGER-1", "operator", synthetic=True,
        )
    page = client.get(f"/demo/scenarios/{approval_id}")
    assert page.status_code == 200
    assert "Operator confirmed non-execution; the original attempt remains ambiguous" in page.text
    assert 'class="scenario-stage attention"' in page.text
    assert "Create a fresh pending approval" in page.text


def test_preview_exhaustion_requires_due_attempts_and_decision_replay_does_not_retry(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setattr(settings, "max_action_attempts", 3)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval_id = start_preview_scenario(db, "exhausted", "operator").id
    decision = {"csrf_token": token, "decision": "approved"}
    client.post(f"/approvals/{approval_id}/decision", data=decision)
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.execution_status == "retryable" and approval.attempts == 1
        approval.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    client.post(f"/approvals/{approval_id}/decision", data=decision)
    with TestingSession() as db:
        assert db.get(Approval, approval_id).attempts == 1
    for expected_state, expected_attempts in (("retryable", 2), ("exhausted", 3)):
        response = client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        with TestingSession() as db:
            approval = db.get(Approval, approval_id)
            assert (approval.execution_status, approval.attempts) == (expected_state, expected_attempts)
            if expected_state == "retryable":
                assert client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}).status_code == 409
                approval.next_attempt_at = utcnow() - timedelta(seconds=1)
                db.commit()
    assert client.post(f"/approvals/{approval_id}/retry", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}, follow_redirects=False).status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert (approval.execution_status, approval.attempts) == ("succeeded", 4)


def test_preview_exhaustion_retry_stops_when_source_evidence_changes(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval_id = start_preview_scenario(db, "exhausted", "operator").id
    client.post(f"/approvals/{approval_id}/decision", data={"csrf_token": token, "decision": "approved"})
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.attempts == 1 and approval.execution_status == "retryable"
        db.get(Case, approval.subject_id).message = "The customer added new information."
        approval.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    response = client.post(f"/approvals/{approval_id}/preview-run", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        assert approval.execution_status == "evidence_changed" and approval.attempts == 1
        execution = db.scalar(select(ActionExecution).where(
            ActionExecution.idempotency_key == f"approval:{approval_id}"
        ))
        assert execution.attempts == 1 and execution.state == "retryable"


def test_preview_scenario_renders_waiting_and_due_backoff(monkeypatch):
    from app.preview_scenarios import start_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    token = sign_in()
    with TestingSession() as db:
        approval_id = start_preview_scenario(db, "retryable", "operator").id
    response = client.post(f"/approvals/{approval_id}/decision?from_scenario={approval_id}",
                           data={"csrf_token": token, "decision": "approved"})
    assert response.status_code == 200 and "Waiting for backoff" in response.text
    assert "Run one due local mock attempt" not in response.text
    with TestingSession() as db:
        approval = db.get(Approval, approval_id)
        approval.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
    due = client.get(f"/demo/scenarios/{approval_id}")
    assert due.status_code == 200 and "Run one due local mock attempt" in due.text


def test_preview_scenario_list_uses_latest_reconciliation_after_long_history(monkeypatch):
    from app.preview_scenarios import create_preview_scenario

    monkeypatch.setattr(settings, "preview_mode", True)
    monkeypatch.setitem(main.templates.env.globals, "preview_mode", True)
    sign_in()
    with TestingSession() as db:
        approval_id = create_preview_scenario(db, "ambiguous", "operator").id
        for index in range(51):
            reconcile_approval_execution(db, approval_id, "still_unknown",
                f"Synthetic review number {index} remains uncertain.", "", "operator", synthetic=True)
        reconcile_approval_execution(db, approval_id, "confirmed_completed",
            "A later ledger review confirms the action completed.", "LATEST-1", "operator", synthetic=True)
    page = client.get('/demo')
    assert "The operator recorded completion. The original execution remains ambiguous" in page.text
    with TestingSession() as db:
        reconcile_approval_execution(db, approval_id, "confirmed_not_executed",
            "A further ledger review contradicts the previous conclusion.", "LATEST-2", "operator", synthetic=True)
    page = client.get('/demo')
    assert "Conflicting evidence must be reviewed" in page.text
