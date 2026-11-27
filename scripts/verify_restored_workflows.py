"""Verify the restored n8n support and exception schedules without targeted triggers."""
import argparse
import os
import time
import json
import httpx
from sqlalchemy import select
from app.config import settings
from app.db import SessionLocal
from app.models import Order, Case, Approval, ActionExecution

KEY = "ci-n8n-restored-support"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    parser.parse_args()
    assert os.environ.get("POPIFY_ENV") == "development" and settings.llm_mode == "mock"
    assert not settings.openai_api_key and not settings.shopify_admin_token
    started = time.monotonic()
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        body = {"message": "Where is my order?", "order_ref": "#N8N-RESTORE",
                "email": "restore@example.test", "request_key": KEY}
        headers = {"X-N8N-Secret": settings.n8n_webhook_secret}
        first = client.post("/api/support", json=body, headers=headers)
        assert first.status_code == 202
        case_id = first.json()["case_id"]
        repeat = client.post("/api/support", json=body, headers=headers)
        assert repeat.status_code == 202 and repeat.json()["case_id"] == case_id
    while time.monotonic() - started < 150:
        with SessionLocal() as db:
            order = db.scalar(select(Order).where(Order.external_id == "ci-n8n-restored-volume"))
            assert order
            case = db.get(Case, case_id)
            assert case
            exceptions = list(db.scalars(select(Approval).where(Approval.subject_type == "order",
                Approval.subject_id == order.id, Approval.proposed_action == "review_delayed_order")))
            replies = list(db.scalars(select(Approval).where(Approval.subject_type == "case",
                Approval.subject_id == case_id, Approval.proposed_action == "send_reply")))
            assert len(exceptions) <= 1 and len(replies) <= 1
            if case.status == "needs_review" and exceptions and replies:
                assert case.order_id == order.id and case.attempts == 1
                assert all(a.status == "pending" and a.execution_status == "not_started" for a in exceptions + replies)
                keys = [f"approval:{a.id}" for a in exceptions + replies]
                assert not list(db.scalars(select(ActionExecution).where(ActionExecution.idempotency_key.in_(keys))))
                print("PASS: restored support and exception schedules produced unique pending human decisions; no action executed")
                print(json.dumps({"restored_support_and_exception_seconds": round(time.monotonic()-started, 3)}))
                return
        time.sleep(2)
    raise RuntimeError("Restored support or exception scheduler did not complete within 150 seconds")


if __name__ == "__main__":
    main()
