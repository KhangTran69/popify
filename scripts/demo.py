"""Repeatable local demonstration; all approvals execute mock actions only."""

import json
import re
import uuid
from datetime import timedelta
import httpx
from sqlalchemy import select, func
from app.config import settings
from app.db import SessionLocal
from app.models import Delivery, Order, Case, Approval, ActionExecution, Audit, utcnow
from app.services import audit


def run():
    run_id = uuid.uuid4().hex[:10]
    ref = f"#DEMO-{run_id.upper()}"
    now = utcnow()
    created = now - timedelta(hours=72)
    # Synthetic intake has the same durable queue boundary as a verified Shopify webhook.
    # Signature verification is exercised separately by send_demo_webhook and the test suite.
    with SessionLocal() as db:
        payload = {
            "id": f"demo-{run_id}",
            "name": ref,
            "email": "alex@example.test",
            "currency": "USD",
            "total_price": "84.00",
            "financial_status": "paid",
            "fulfillment_status": None,
            "created_at": created.isoformat(),
            "updated_at": now.isoformat(),
        }
        d = Delivery(
            webhook_id=f"demo-{run_id}",
            topic="orders/create",
            shop_domain="demo.myshopify.com",
            payload=payload,
            status="received",
        )
        db.add(d)
        db.flush()
        audit(
            db,
            "delivery",
            d.id,
            "synthetic_delivery_received",
            correlation_id=d.webhook_id,
        )
        db.commit()
        delivery_id = d.id
    with (
        httpx.Client(
            base_url="http://localhost:8000", timeout=65, follow_redirects=True
        ) as api,
        httpx.Client(base_url="http://n8n:5678", timeout=65) as n8n,
    ):

        def trigger(path):
            result = n8n.post("/webhook/commerceops/" + path)
            result.raise_for_status()
            return result.json()

        # Drain at most 50 queued items so existing work is preserved.
        for _ in range(50):
            trigger("orders/process")
            with SessionLocal() as db:
                if db.get(Delivery, delivery_id).status == "processed":
                    break
        else:
            raise RuntimeError(
                "Demo order did not reach processed state within 50 runs"
            )
        trigger("exceptions/scan")
        key = "demo-support-" + run_id
        response = api.post(
            "/api/support",
            headers={"X-N8N-Secret": settings.n8n_webhook_secret},
            json={
                "message": "Where is my order? It has been three days.",
                "order_ref": ref,
                "email": "alex@example.test",
                "request_key": key,
            },
        )
        response.raise_for_status()
        case_id = response.json()["case_id"]
        for _ in range(50):
            trigger("support/triage")
            with SessionLocal() as db:
                if db.get(Case, case_id).status == "needs_review":
                    break
        else:
            raise RuntimeError("Demo case did not reach review within 50 runs")
        page = api.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
        response = api.post(
            "/login",
            data={
                "username": settings.operator_username,
                "password": settings.operator_password,
                "csrf_token": token,
            },
        )
        response.raise_for_status()
        token = re.search(r'name="csrf_token" value="([^"]+)', response.text).group(1)
        with SessionLocal() as db:
            case = db.get(Case, case_id)
            order_id = case.order_id
            assert order_id and "unfulfilled" in case.draft
            approval = db.scalar(
                select(Approval).where(
                    Approval.subject_type == "case",
                    Approval.subject_id == case_id,
                    Approval.proposed_action == "send_reply",
                )
            )
            approval_id = approval.id
            assert (
                approval.status == "pending"
                and approval.execution_status == "not_started"
            )
            exception = db.scalar(
                select(Approval).where(
                    Approval.subject_type == "order", Approval.subject_id == order_id
                )
            )
            assert exception
            exception_id = exception.id
        for target in [approval_id, exception_id]:
            response = api.post(
                f"/approvals/{target}/decision",
                data={"csrf_token": token, "decision": "approved"},
            )
            response.raise_for_status()
        trigger("exceptions/scan")
        # Repeat the same approval and worker call: there must still be one execution per action.
        api.post(
            f"/approvals/{approval_id}/decision",
            data={"csrf_token": token, "decision": "approved"},
        ).raise_for_status()
        trigger("exceptions/scan")
    with SessionLocal() as db:
        a = db.get(Approval, approval_id)
        assert a.execution_status == "succeeded", a.execution_status
        executions = db.scalar(
            select(func.count(ActionExecution.id)).where(
                ActionExecution.idempotency_key == f"approval:{a.id}"
            )
        )
        assert executions == 1
        d = db.get(Delivery, delivery_id)
        result = {
            "run_id": run_id,
            "order_id": order_id,
            "order_ref": ref,
            "case_id": case_id,
            "approval_id": a.id,
            "exception_id": exception_id,
            "mock_execution": a.execution_status,
            "execution_count": executions,
            "receipt_to_persistence_seconds": round(
                (d.processed_at - d.received_at).total_seconds(), 3
            ),
            "workflow_heartbeats": {
                str(n): db.scalar(
                    select(func.count(Audit.id)).where(
                        Audit.entity_type == "workflow", Audit.entity_id == n
                    )
                )
                for n in [1, 2, 3]
            },
        }
    print(json.dumps(result, indent=2))
    print(f"Open http://localhost:8001/orders/{order_id} for the complete timeline.")


if __name__ == "__main__":
    run()
