"""Disposable, synthetic preview scenarios. Never imported by workflow code."""

import uuid
from datetime import timedelta
from sqlalchemy import select

from app.config import settings
from app.models import (
    ActionExecution, Approval, Audit, Case, ExecutionReconciliation, Order, utcnow,
)
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
    audit(
        db, "order", order.id, "preview_order_context_attached", actor,
        details={"synthetic": True, "scenario": name},
        correlation_id=record.correlation_id,
    )
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


def load_preview_scenario(db, approval_id):
    """Load one bounded, correlated synthetic scenario for its guided record page."""
    if not settings.preview_mode:
        return None
    selected = db.get(Approval, approval_id)
    if not selected or selected.subject_type != "case":
        return None
    case = db.get(Case, selected.subject_id)
    if not case or not (case.correlation_id or "").startswith("preview:"):
        return None
    order = db.get(Order, case.order_id) if case.order_id else None
    approvals = db.scalars(
        select(Approval)
        .where(
            Approval.subject_type == "case",
            Approval.subject_id == case.id,
        )
        .order_by(Approval.revision, Approval.id)
        .limit(20)
    ).all()
    leaf_approvals = [item for item in approvals if item.replacement_approval_id is None]
    current = leaf_approvals[-1] if leaf_approvals else approvals[-1]
    approval_ids = [item.id for item in approvals]
    executions = db.scalars(
        select(ActionExecution)
        .where(ActionExecution.idempotency_key.in_([f"approval:{item}" for item in approval_ids]))
        .order_by(ActionExecution.id)
        .limit(20)
    ).all() if approval_ids else []
    reconciliations = db.scalars(
        select(ExecutionReconciliation)
        .where(ExecutionReconciliation.approval_id.in_(approval_ids))
        .order_by(ExecutionReconciliation.created_at, ExecutionReconciliation.id)
        .limit(20)
    ).all() if approval_ids else []
    events = db.scalars(
        select(Audit)
        .where(Audit.correlation_id == case.correlation_id)
        .order_by(Audit.created_at.desc(), Audit.id.desc())
        .limit(100)
    ).all()
    segments = case.correlation_id.split(":")
    name = segments[1].replace("-", " ") if len(segments) > 1 else "scenario"
    return {
        "name": name,
        "correlation_id": case.correlation_id,
        "order": order,
        "case": case,
        "approvals": approvals,
        "current": current,
        "executions": {item.idempotency_key: item for item in executions},
        "reconciliations": {item.approval_id: item for item in reconciliations},
        "events": list(reversed(events)),
        "scenario_id": current.id,
    }


def preview_scenario_contains(db, scenario_id, kind, record_id):
    """Check record membership before accepting a scenario return destination."""
    try:
        scenario_id = int(scenario_id)
        record_id = int(record_id)
    except (TypeError, ValueError):
        return False
    scenario = load_preview_scenario(db, scenario_id)
    if not scenario:
        return False
    if kind == "approval":
        return any(item.id == record_id for item in scenario["approvals"])
    if kind == "case":
        return scenario["case"].id == record_id
    if kind == "order":
        return bool(scenario["order"] and scenario["order"].id == record_id)
    return False
