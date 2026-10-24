import pytest
import base64, hashlib, hmac, json
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import main
from app.db import Base, get_db
from app.config import settings
from app.models import Order, Delivery, Approval, ActionExecution, utcnow
from app.services import (
    process_delivery,
    create_approval,
    decide_approval,
    verify_shopify,
    create_support_case,
    execute_approved,
    as_utc,
    SupportRequestKeyConflict,
)

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)


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
