"""Disposable, synthetic preview scenarios. Never imported by workflow code."""

import uuid
from datetime import timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import aliased

from app.config import settings
from app.models import (
    ActionExecution, Approval, Audit, Case, ExecutionReconciliation, Order, utcnow,
)
from app.services import (
    audit, create_approval, decide_approval, execute_approved,
    lock_approval_lifecycle, subject_execution_keys,
)


SCENARIOS = ("pending", "success", "rejected", "retryable", "exhausted", "stale", "ambiguous", "blocked")
SCENARIO_GUIDANCE = {
    "pending": "Review the evidence and choose a human decision.",
    "success": "Approve once and inspect the saved mock result.",
    "rejected": "Reject the request and confirm no action ran.",
    "retryable": "Observe a transient failure, wait for backoff, and run a due retry.",
    "exhausted": "Run each due mock attempt through its backoff until the budget is exhausted, then authorize recovery.",
    "stale": "Compare changed facts and review a replacement approval.",
    "ambiguous": "Reconcile uncertain execution without blindly retrying it.",
    "blocked": "Inspect an unsupported action that cannot execute.",
}


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
        record_exhausted_attempts(db, approval, actor)
    else:
        execute_approved(db, approval, mock_outcome=None if name == "success" else name)
    db.commit()
    return approval


def start_preview_scenario(db, name, actor="preview"):
    """Create a fresh run at the human decision stage; do not pre-resolve it."""
    if not settings.preview_mode:
        raise PermissionError("synthetic scenarios are available only in isolated preview mode")
    if name not in SCENARIOS:
        raise ValueError("unknown preview scenario")
    token = uuid.uuid4().hex[:8]
    now = utcnow()
    order = Order(
        shop_domain="preview.invalid", external_id=f"preview-run-{token}",
        order_number=f"#RUN-{token.upper()}", email="synthetic@example.test",
        currency="USD", total="84.00", fulfillment_status="unfulfilled",
        financial_status="paid", created_at=now - timedelta(hours=72),
        source_updated_at=now, payload={"synthetic": True},
    )
    db.add(order)
    db.flush()
    correlation_id = f"preview:{name}:{token}"
    case = Case(
        order_id=order.id, order_ref=order.order_number, customer_email=order.email,
        message=f"Synthetic {name} support request", category="order_status",
        confidence=88, draft=f"Synthetic {name} response draft.", status="needs_review",
        correlation_id=correlation_id,
    )
    db.add(case)
    db.flush()
    audit(db, "order", order.id, "preview_order_context_attached", actor,
          details={"synthetic": True, "scenario": name}, correlation_id=correlation_id)
    audit(db, "case", case.id, "preview_scenario_created", actor,
          details={"scenario": name, "synthetic": True, "interactive": True},
          correlation_id=correlation_id)
    audit(db, "case", case.id, "preview_triage_completed", actor,
          details={"draft_saved": True, "category": case.category,
                   "confidence": case.confidence, "synthetic": True},
          correlation_id=correlation_id)
    action = "unsupported_demo_action" if name == "blocked" else "send_reply"
    approval = create_approval(db, "support", "case", case.id, action, {
        "order_number": order.order_number, "order_id": order.id,
        "customer_email": case.customer_email, "case_order_ref": case.order_ref,
        "order_email": order.email, "customer_message": case.message,
        "policy": settings.store_policies[case.category],
        "order_fulfillment_status": order.fulfillment_status,
        "order_financial_status": order.financial_status,
        "order_total": "USD 84.00", "order_source_updated_at": order.source_updated_at.isoformat(),
        "category": case.category, "confidence": case.confidence,
        "draft": case.draft, "missing_info": "", "case_status": case.status,
        "correlation_id": correlation_id,
    })
    if name == "stale":
        case.message = "New synthetic customer information arrived after the approval snapshot."
        db.flush()
        audit(db, "case", case.id, "preview_source_changed_after_approval", actor,
              details={"synthetic": True}, correlation_id=correlation_id)
    db.commit()
    return approval


def record_exhausted_attempts(db, approval, actor="preview"):
    """Persist the configured mock attempt budget and one audit event per try."""
    attempts = settings.max_action_attempts
    failure = "Synthetic automatic retry budget exhausted; no external request was made."
    execution = db.scalar(select(ActionExecution).where(
        ActionExecution.idempotency_key == f"approval:{approval.id}"
    ))
    if execution:
        raise ValueError("This preview approval already has an execution")
    execution = ActionExecution(
        idempotency_key=f"approval:{approval.id}",
        proposed_action=approval.proposed_action,
        evidence_hash=approval.evidence_hash,
        state="exhausted", attempts=attempts, detail=failure,
    )
    db.add(execution)
    approval.attempts = attempts
    approval.execution_status = "exhausted"
    approval.failure_detail = failure
    approval.next_attempt_at = None
    correlation_id = (approval.supporting_facts or {}).get("correlation_id")
    for attempt in range(1, attempts + 1):
        audit(db, "approval", approval.id, "action_execution_attempt_exhausted", actor,
              details={"attempt": attempt, "automatic_attempt_budget": attempts,
                       "synthetic": True}, correlation_id=correlation_id)


def execute_preview_approval(db, approval, actor):
    """Run one guarded mock attempt, scoped to the whole synthetic journey."""
    if not settings.preview_mode:
        raise PermissionError("preview outcomes are available only in isolated preview mode")
    approval = lock_approval_lifecycle(db, approval.id)
    prior = db.scalar(select(ActionExecution).where(
        ActionExecution.idempotency_key == f"approval:{approval.id}"
    ))
    case = db.get(Case, approval.subject_id) if approval.subject_type == "case" else None
    parts = (case.correlation_id or "").split(":") if case else []
    scenario_name = parts[1] if len(parts) >= 3 and parts[0] == "preview" else "success"
    has_journey_attempt = bool(db.scalar(select(ActionExecution.id).where(
        ActionExecution.idempotency_key.in_(subject_execution_keys(
            approval.subject_type, approval.subject_id
        )),
    ).limit(1)))
    outcome = None
    if scenario_name in ("retryable", "ambiguous") and not has_journey_attempt:
        outcome = scenario_name
    elif scenario_name == "exhausted":
        attempts = prior.attempts if prior else 0
        if not has_journey_attempt or (prior and attempts < settings.max_action_attempts):
            outcome = "exhausted" if attempts + 1 >= settings.max_action_attempts else "retryable"
    execute_approved(db, approval, mock_outcome=outcome)


def latest_reconciliations(db, approval_ids):
    """Return the current assertion for each displayed approval."""
    if not approval_ids:
        return {}
    latest = (select(
        ExecutionReconciliation.approval_id.label("approval_id"),
        func.max(ExecutionReconciliation.revision).label("revision"),
    ).where(ExecutionReconciliation.approval_id.in_(approval_ids))
      .group_by(ExecutionReconciliation.approval_id).subquery())
    rows = db.scalars(select(ExecutionReconciliation).join(
        latest,
        (ExecutionReconciliation.approval_id == latest.c.approval_id)
        & (ExecutionReconciliation.revision == latest.c.revision),
    ).limit(len(approval_ids))).all()
    return {item.approval_id: item for item in rows}


def related_approval_ids(db, approval_ids, states):
    """Find one related approval in the requested states for each displayed row."""
    if not approval_ids:
        return {}
    related = aliased(Approval)
    rows = db.execute(select(Approval.id, func.max(related.id)).join(
        related, (related.subject_type == Approval.subject_type)
        & (related.subject_id == Approval.subject_id) & (related.id != Approval.id),
    ).where(Approval.id.in_(approval_ids), related.execution_status.in_(states))
      .group_by(Approval.id).limit(len(approval_ids))).all()
    return dict(rows)


def unresolved_related_approvals(db, approval_ids):
    return related_approval_ids(db, approval_ids, ("ambiguous", "disputed"))


def load_preview_scenario(db, approval_id, reconciliation_page=1, revision_page=1, audit_page=1):
    """Load bounded, paginated histories and keep current state independently visible."""
    if not settings.preview_mode:
        return None
    selected = db.get(Approval, approval_id)
    if not selected or selected.subject_type != "case":
        return None
    case = db.get(Case, selected.subject_id)
    if not case or not (case.correlation_id or "").startswith("preview:"):
        return None
    order = db.get(Order, case.order_id) if case.order_id else None
    approvals_total = db.scalar(select(func.count()).select_from(Approval).where(
        Approval.subject_type == "case", Approval.subject_id == case.id
    )) or 0
    approval_pages = max(1, (approvals_total + 19) // 20)
    revision_page = min(max(1, int(revision_page or 1)), approval_pages)
    approval_history = db.scalars(
        select(Approval)
        .where(Approval.subject_type == "case", Approval.subject_id == case.id)
        .order_by(Approval.revision.desc(), Approval.id.desc())
        .offset((revision_page - 1) * 20)
        .limit(20)
    ).all()
    approval_history.reverse()
    current = db.scalar(
        select(Approval)
        .where(
            Approval.subject_type == "case", Approval.subject_id == case.id,
            Approval.replacement_approval_id.is_(None),
        )
        .order_by(Approval.revision.desc(), Approval.id.desc())
        .limit(1)
    ) or selected
    page_approval_ids = [item.id for item in approval_history]
    display_approvals = list(approval_history)
    if current.id not in page_approval_ids:
        display_approvals.append(current)
    approval_ids = list(dict.fromkeys([*page_approval_ids, current.id]))
    executions = db.scalars(
        select(ActionExecution)
        .where(ActionExecution.idempotency_key.in_([f"approval:{item}" for item in approval_ids]))
        .order_by(ActionExecution.id)
        .limit(21)
    ).all() if approval_ids else []
    reconciliations = latest_reconciliations(db, approval_ids)
    current_reconciliation_history = []
    reconciliation_total = db.scalar(select(func.count()).select_from(
        ExecutionReconciliation
    ).where(ExecutionReconciliation.approval_id == current.id)) or 0
    reconciliation_pages = max(1, (reconciliation_total + 19) // 20)
    reconciliation_page = min(max(1, int(reconciliation_page or 1)), reconciliation_pages)
    if reconciliation_total:
        current_reconciliation_history = db.scalars(
            select(ExecutionReconciliation)
            .where(ExecutionReconciliation.approval_id == current.id)
            .order_by(ExecutionReconciliation.revision.desc(), ExecutionReconciliation.id.desc())
            .offset((reconciliation_page - 1) * 20)
            .limit(20)
        ).all()
        current_reconciliation_history.reverse()
    events_total = db.scalar(select(func.count()).select_from(Audit).where(
        Audit.correlation_id == case.correlation_id
    )) or 0
    events_pages = max(1, (events_total + 99) // 100)
    audit_page = min(max(1, int(audit_page or 1)), events_pages)
    events = db.scalars(
        select(Audit)
        .where(Audit.correlation_id == case.correlation_id)
        .order_by(Audit.created_at.desc(), Audit.id.desc())
        .offset((audit_page - 1) * 100)
        .limit(100)
    ).all()
    segments = case.correlation_id.split(":")
    raw_name = segments[1] if len(segments) > 1 else "scenario"
    replay_aliases = {"completed": "success", "evidence-changed": "stale", "invalid-action": "blocked"}
    scenario_key = replay_aliases.get(raw_name, raw_name)
    if scenario_key not in SCENARIOS:
        scenario_key = "pending"
    name = raw_name.replace("-", " ")
    return {
        "name": name,
        "scenario_key": scenario_key,
        "correlation_id": case.correlation_id,
        "order": order,
        "case": case,
        "approvals": display_approvals,
        "approval_history": approval_history,
        "approval_page": revision_page,
        "approval_pages": approval_pages,
        "approval_total": approvals_total,
        "current": current,
        "executions": {item.idempotency_key: item for item in executions},
        "reconciliations": reconciliations,
        "reconciliation_history": current_reconciliation_history,
        "reconciliation_page": reconciliation_page,
        "reconciliation_pages": reconciliation_pages,
        "reconciliation_total": reconciliation_total,
        "events": list(reversed(events)),
        "audit_page": audit_page,
        "audit_pages": events_pages,
        "audit_total": events_total,
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
        candidate = db.get(Approval, record_id)
        return bool(candidate and candidate.subject_type == "case"
                    and candidate.subject_id == scenario["case"].id)
    if kind == "case":
        return scenario["case"].id == record_id
    if kind == "order":
        return bool(scenario["order"] and scenario["order"].id == record_id)
    return False
