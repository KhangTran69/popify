"""Disposable, synthetic preview scenarios. Never imported by workflow code."""

import uuid
from datetime import timedelta

from app.config import settings
from app.models import ActionExecution, Case, Order, utcnow
from app.services import audit, create_approval, decide_approval, execute_approved


SCENARIOS = ("pending", "success", "rejected", "retryable", "exhausted", "stale", "ambiguous")


def create_preview_scenario(db, name, actor="preview"):
    if not settings.preview_mode:
        raise PermissionError("synthetic scenarios are available only in isolated preview mode")
    if name not in SCENARIOS:
        raise ValueError("unknown preview scenario")
    token = uuid.uuid4().hex[:8]
    now = utcnow()
    order = Order(
        shop_domain="preview.invalid", external_id=f"preview-{token}",
        order_number=f"#PREVIEW-{token.upper()}", email="synthetic@example.test",
        currency="USD", total="84.00", fulfillment_status="unfulfilled",
        financial_status="paid", created_at=now - timedelta(hours=72),
        source_updated_at=now, payload={"synthetic": True},
    )
    db.add(order)
    db.flush()
    record = Case(
        order_id=order.id, order_ref=order.order_number, customer_email=order.email,
        message=f"Synthetic {name} support request", category="order_status",
        confidence=88, draft=f"Synthetic {name} response draft.", status="needs_review",
        correlation_id=f"preview:{name}:{token}",
    )
    db.add(record)
    db.flush()
    audit(db, "case", record.id, "preview_scenario_created", actor,
          details={"scenario": name, "synthetic": True}, correlation_id=record.correlation_id)
    approval = create_approval(db, "support", "case", record.id, "send_reply", {
        "order_number": order.order_number, "order_id": order.id,
        "customer_email": record.customer_email, "case_order_ref": record.order_ref,
        "order_email": order.email, "customer_message": record.message,
        "policy": settings.store_policies[record.category],
        "order_fulfillment_status": order.fulfillment_status,
        "order_financial_status": order.financial_status, "order_total": "USD 84.00",
        "order_source_updated_at": order.source_updated_at.isoformat(),
        "category": record.category, "confidence": record.confidence,
        "draft": record.draft, "missing_info": "", "case_status": record.status,
        "correlation_id": record.correlation_id,
    })
    if name == "pending":
        db.commit()
        return approval
    decision = "rejected" if name == "rejected" else "approved"
    decide_approval(db, approval.id, decision, actor)
    if name == "rejected":
        return approval
    if name == "stale":
        record.message = "Synthetic request changed after the approval snapshot."
        db.flush()
        execute_approved(db, approval)
    elif name == "exhausted":
        # This is a persisted historical outcome: the recorded attempt count
        # equals the configured automatic budget, with one audit event per try.
        attempts = settings.max_action_attempts
        failure = "Synthetic automatic retry budget exhausted; no external request was made."
        execution = ActionExecution(
            idempotency_key=f"approval:{approval.id}",
            proposed_action=approval.proposed_action,
            evidence_hash=approval.evidence_hash,
            state="exhausted",
            attempts=attempts,
            detail=failure,
        )
        db.add(execution)
        approval.attempts = attempts
        approval.execution_status = "exhausted"
        approval.failure_detail = failure
        for attempt in range(1, attempts + 1):
            audit(
                db, "approval", approval.id, "action_execution_attempt_exhausted", actor,
                details={"attempt": attempt, "automatic_attempt_budget": attempts, "synthetic": True},
                correlation_id=record.correlation_id,
            )
    else:
        execute_approved(db, approval, mock_outcome=None if name == "success" else name)
    db.commit()
    return approval
