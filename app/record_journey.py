"""Bounded operational history and next-step presentation for record pages."""

from math import ceil
from urllib.parse import urlencode

from sqlalchemy import and_, func, or_, select

from app.models import Approval, Audit, Case
from app.outcome import approval_outcome_issue
from app.services import as_utc


EVENT_COPY = {
    "preview_order_context_attached": ("Synthetic order linked", "The saved order supplies context for this inquiry."),
    "preview_scenario_created": ("Customer request saved", "A synthetic inquiry started this isolated scenario."),
    "inquiry_queued": ("Customer request saved", "The inquiry is persisted and waiting for support automation."),
    "preview_triage_completed": ("Automation prepared a draft", "Review the saved support case and its order facts."),
    "draft_created": ("Automation prepared a draft", "A response draft was saved for review."),
    "approval_requested": ("Human review requested", "The proposed action and evidence were saved for an operator decision."),
    "approval_approved": ("Operator approved", "The decision is recorded; approval alone does not prove the action completed."),
    "approval_rejected": ("Operator rejected", "The decision is recorded and no action should execute."),
    "execution_outcome_reconciled": ("Execution outcome reconciled · operator assertion", "The operator conclusion is saved; the original execution remains recorded."),
    "approval_replaced_with_fresh_evidence": ("Approval replaced after evidence review", "A new review was created while the original decision and attempts remain in history."),
    "approval_refreshed_from_previous_revision": ("Fresh approval requested", "Current source facts were captured for a new human decision."),
    "support_draft_refreshed_for_approval": ("Draft refreshed for new review", "The fresh proposal uses current source facts."),
    "preview_source_changed_after_approval": ("Source facts changed", "Compare the reviewed snapshot with current facts before deciding again."),
}


def journey_event(event, *, query="", scenario_id=None):
    """Translate one immutable audit event into consistent operator-facing copy."""
    details = event.details or {}
    title, explanation = EVENT_COPY.get(
        event.event, (event.event.replace("_", " ").capitalize(), "")
    )
    if event.event.startswith("action_execution_"):
        state = event.event.removeprefix("action_execution_").replace("_", " ")
        title = f"Mock action: {state}"
        attempt = details.get("attempt")
        explanation = f"Attempt {attempt}. " if isinstance(attempt, int) else ""
        explanation += "The persisted execution and retry state determine the next supported action."
    elif event.event == "execution_outcome_reconciled":
        outcome = str(details.get("outcome") or "unknown").replace("_", " ")
        explanation = f"Operator conclusion: {outcome}. The original execution remains recorded."
    elif event.event == "approval_replaced_with_fresh_evidence":
        explanation = f"New approval #{details.get('replacement_approval_id')}; the original decision and attempts remain in history."
    elif event.event == "approval_refreshed_from_previous_revision":
        explanation = f"Replaces approval #{details.get('previous_approval_id')}."
    route = {"order": "orders", "case": "cases", "approval": "approvals", "delivery": "deliveries"}.get(event.entity_type)
    if scenario_id is not None:
        suffix = "?" + urlencode({"from_scenario": scenario_id})
    else:
        suffix = query
    return {
        "event": event,
        "title": title,
        "explanation": explanation,
        "href": f"/{route}/{event.entity_id}{suffix}" if route else None,
    }


def load_record_journey(
    db, kind, entity, *, query="", page=1, per_page=40,
    related_page=1, related_per_page=20,
):
    """Load paginated related approvals and an untruncated SQL-linked audit journey."""
    related = []
    related_total = 0
    related_pages = 1
    related_condition = None
    conditions = [(Audit.entity_type == kind) & (Audit.entity_id == entity.id)]
    if kind == "approval":
        correlation_id = (entity.supporting_facts or {}).get("correlation_id")
        if correlation_id:
            conditions.append(Audit.correlation_id == correlation_id)
    if kind in ("order", "case"):
        if kind == "order":
            case_ids = select(Case.id).where(Case.order_id == entity.id)
            related_condition = or_(
                and_(Approval.subject_type == "order", Approval.subject_id == entity.id),
                and_(Approval.subject_type == "case", Approval.subject_id.in_(case_ids)),
            )
            conditions.extend([
                and_(Audit.entity_type == "case", Audit.entity_id.in_(case_ids)),
                and_(
                    Audit.entity_type == "approval",
                    Audit.entity_id.in_(select(Approval.id).where(related_condition)),
                ),
            ])
        else:
            related_condition = and_(Approval.subject_type == "case", Approval.subject_id == entity.id)
            conditions.append(and_(
                Audit.entity_type == "approval",
                Audit.entity_id.in_(select(Approval.id).where(related_condition)),
            ))
        related_total = db.scalar(
            select(func.count()).select_from(Approval).where(related_condition)
        ) or 0
        related_pages = max(1, ceil(related_total / related_per_page))
        related_page = min(max(1, int(related_page or 1)), related_pages)
        related = list(db.scalars(
            select(Approval).where(related_condition)
            .order_by(Approval.id.desc())
            .offset((related_page - 1) * related_per_page).limit(related_per_page)
        ))
    condition = or_(*conditions)
    total = db.scalar(select(func.count()).select_from(Audit).where(condition)) or 0
    pages = max(1, ceil(total / per_page))
    page = min(max(1, page), pages)
    events = list(db.scalars(
        select(Audit).where(condition)
        .order_by(Audit.created_at.desc(), Audit.id.desc())
        .offset((page - 1) * per_page).limit(per_page)
    ))
    return {
        "related": related,
        "related_total": related_total,
        "related_page": related_page,
        "related_pages": related_pages,
        "events": [journey_event(item, query=query) for item in events],
        "total": total,
        "page": page,
        "pages": pages,
    }


def record_guidance(
    kind, entity, *, freshness_differences=None, reconciliation=None, now=None,
    detail_query="", blocking_approval=None, blocking_execution=None,
    refresh_supported=False, refresh_blocked=False, related_total=0,
    action_execution=None, preview_mode=False, retry_due=False,
):
    """Describe the persisted state and its one supported operator next step."""
    freshness_differences = freshness_differences or []
    if kind == "approval":
        status = entity.execution_status
        issue = approval_outcome_issue(entity, action_execution)
        if issue:
            return {"tone": "bad", "state": "Persisted outcome inconsistent", "action": "The decision and execution records disagree. Inspect their saved history and ask a maintainer to resolve the data before reporting an outcome.", "href": "#operational-journey", "link": "Inspect saved history"}
        if status in ("ambiguous", "disputed"):
            return {"tone": "bad", "state": "Outcome uncertain", "action": "Append supported evidence to this execution; retries and replacement remain blocked while its outcome is uncertain.", "href": "#approval-actions", "link": "Review reconciliation controls"}
        if blocking_execution:
            target = (
                f"/approvals/{blocking_approval.id}{detail_query}"
                if blocking_approval
                else f"#operational-journey"
            )
            return {
                "tone": "bad",
                "state": "Related execution blocks this decision",
                "action": "Review the prior execution before taking further action.",
                "href": target,
                "link": f"Open blocking approval #{blocking_approval.id}" if blocking_approval else "Inspect operational history",
            }
        if entity.replacement_approval_id:
            return {"tone": "warn", "state": "Historical revision", "action": f"Open replacement approval #{entity.replacement_approval_id}; this record remains unchanged.", "href": f"/approvals/{entity.replacement_approval_id}{detail_query}", "link": "Open replacement approval"}
        if entity.status == "pending" and freshness_differences:
            possible = refresh_supported and not refresh_blocked
            return {"tone": "bad", "state": "Evidence is stale", "action": "Compare reviewed evidence with current facts, then create a fresh approval." if possible else "Evidence changed, but this approval cannot currently be replaced. Inspect its history and blocker.", "href": "#evidence-comparison" if possible else "#operational-journey", "link": "Compare reviewed and current evidence" if possible else "Inspect saved evidence and history"}
        if entity.status == "pending":
            return {"tone": "warn", "state": "Decision required", "action": "Review the captured evidence and approve or reject this mock action.", "href": "#approval-actions", "link": "Go to decision controls"}
        if entity.status == "rejected":
            return {"tone": "good", "state": "Rejected", "action": "No execution is expected; inspect the journey for the recorded decision.", "href": "#operational-journey", "link": "Review decision history"}
        if status == "succeeded":
            if action_execution and action_execution.state == "succeeded":
                return {"tone": "good", "state": "Mock execution persisted", "action": "Inspect the saved execution attempt and audit evidence below.", "href": "#operational-journey", "link": "Review persisted execution"}
            return {"tone": "bad", "state": "Execution record missing", "action": "The approval state says succeeded but no successful execution record is available. Ask a maintainer to inspect persistence before reporting completion.", "href": "#operational-journey", "link": "Inspect audit evidence"}
        if status == "reconciled_not_executed":
            return {"tone": "warn", "state": "Non-execution asserted", "action": "Create a fresh approval before attempting another mock action.", "href": "#approval-actions", "link": "Review replacement approval control"}
        if status == "reconciled_completed":
            return {"tone": "warn", "state": "Completion asserted by operator", "action": "The original attempt remains ambiguous; this operator assertion is not provider verification.", "href": "#approval-actions", "link": "Review assertion history"}
        if status == "exhausted":
            return {"tone": "bad", "state": "Attempts exhausted", "action": "Review prior attempts, then explicitly queue manual recovery if still appropriate.", "href": "#approval-actions", "link": "Review recovery controls"}
        if status == "failed":
            return {"tone": "bad", "state": "Action attempt failed", "action": "Review the saved failure and queue one explicit manual recovery attempt.", "href": "#approval-actions", "link": "Queue supported recovery"}
        if status in ("queued", "retryable"):
            waiting = bool(entity.next_attempt_at and now and as_utc(entity.next_attempt_at) > now)
            queued = status == "queued"
            if waiting:
                state = "Waiting for backoff"
                action = f"Wait until the recorded retry deadline: {as_utc(entity.next_attempt_at).strftime('%d %b %Y · %H:%M UTC')}."
            elif preview_mode and retry_due:
                state = "Retry eligible" if status == "retryable" else "Queued attempt ready"
                action = "Run one due local mock attempt in the isolated preview."
            elif queued:
                state, action = "Attempt queued", "The approved action is queued for the next worker run."
            else:
                state = "Retry scheduled"
                action = "The worker will retry this approved action when its persisted retry deadline is due."
            return {"tone": "warn", "state": state, "action": action, "href": "#approval-actions", "link": "Review attempt status"}
        if status == "invalid_action":
            return {"tone": "bad", "state": "Unsupported action", "action": "A maintainer must correct the action contract; execution and retry are unavailable.", "href": "#operational-journey", "link": "Inspect blocked decision"}
        if status == "evidence_changed":
            possible = refresh_supported and not refresh_blocked
            return {"tone": "bad", "state": "Evidence changed", "action": "Compare current facts and create a replacement approval." if possible else "Evidence changed, but replacement is unavailable while a prior execution blocks it.", "href": "#evidence-comparison" if freshness_differences else "#operational-journey", "link": "Compare current facts" if freshness_differences else "Inspect execution history"}
        if status == "superseded":
            return {"tone": "warn", "state": "Approval replaced", "action": "This revision cannot execute. Review the linked replacement approval.", "href": "#approval-revisions", "link": "Review approval revisions"}
    if kind == "case" and entity.status in ("failed", "exhausted"):
        eligible = entity.status == "exhausted" or not entity.next_attempt_at or not now or as_utc(entity.next_attempt_at) <= now
        return {"tone": "bad", "state": "Manual triage recovery available" if entity.status == "exhausted" else "Triage retry eligible" if eligible else "Waiting for triage backoff", "action": "Queue a triage recovery attempt; the worker will honor any recorded deadline." if entity.status == "exhausted" else "Queue one triage retry; it will run when due." if eligible else f"A retry can be queued now, but the worker will wait until {as_utc(entity.next_attempt_at).strftime('%d %b %Y · %H:%M UTC')}.", "href": "#case-recovery", "link": "Review triage recovery"}
    if kind == "delivery" and entity.status in ("failed", "exhausted"):
        return {"tone": "bad", "state": "Delivery recovery required", "action": "Queue a manual delivery retry; the worker will still honor any recorded deadline." if entity.status == "exhausted" else "Queue a delivery retry and check the worker result.", "href": "#delivery-recovery", "link": "Review delivery recovery"}
    if kind == "case" and entity.status == "queued":
        return {"tone": "warn", "state": "Waiting for triage", "action": "The support worker will classify the request and save a draft.", "href": "#record-details", "link": "Review customer request"}
    if kind == "case" and entity.status == "resolved_mock":
        return {"tone": "good", "state": "Mock response completed", "action": "Review the saved draft, approval, and execution history.", "href": "#operational-journey", "link": "Review saved outcome"}
    if kind == "delivery" and entity.status == "processed":
        return {"tone": "good", "state": "Delivery processed", "action": "Review the linked order or intake audit history.", "href": "#operational-journey", "link": "Review intake history"}
    if kind == "delivery" and entity.status in ("received", "processing"):
        return {"tone": "warn", "state": "Delivery queued", "action": "The intake worker owns the next transition; check the delivery again after its next run.", "href": "#record-details", "link": "Review delivery status"}
    if kind == "order" and related_total:
        return {"tone": "warn", "state": f"{related_total} related approval{'s' if related_total != 1 else ''}", "action": "Review related decisions and their latest persisted outcomes.", "href": "#related-approvals", "link": "Review related approvals"}
    if kind == "order":
        return {"tone": "good" if entity.fulfillment_status == "fulfilled" else "warn", "state": f"Order {entity.fulfillment_status.replace('_', ' ')} · payment {entity.financial_status.replace('_', ' ')}", "action": "Review the saved order facts and any new support inquiries linked to this order.", "href": "#record-details", "link": "Review order facts"}
    if kind in ("case", "delivery") and entity.status in ("needs_review", "drafted"):
        return {"tone": "warn", "state": "Human review may be required", "action": "Check linked pending approvals before closing this record.", "href": "#related-approvals", "link": "Review related approvals"}
    return {"tone": "good", "state": "No immediate action", "action": "Review the operational journey for linked decisions and persisted outcomes.", "href": "#operational-journey", "link": "Review operational history"}
