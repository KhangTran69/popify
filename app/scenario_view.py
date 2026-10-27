"""Presentation state for the isolated scenario journey.

Kept outside the route so the decision, recovery and stage copy share one
persisted-state interpretation. This module never mutates an approval.
"""

from app.models import Approval, utcnow
from app.services import approval_freshness_differences, as_utc, prior_terminal_execution
from app.preview_scenarios import SCENARIO_GUIDANCE, unresolved_related_approvals


def _journey_event(event, scenario_id):
    """Turn a saved audit event into a concise, linked operator history row."""
    details = event.details or {}
    event_name = event.event
    title = event_name.replace("_", " ").capitalize()
    explanation = ""
    if event_name == "preview_order_context_attached":
        title, explanation = "Synthetic order linked", "The saved order supplies context for this inquiry."
    elif event_name in ("preview_scenario_created", "inquiry_queued"):
        title, explanation = "Customer request saved", "A synthetic inquiry started this isolated scenario."
    elif event_name in ("preview_triage_completed", "draft_created"):
        title, explanation = "Automation prepared a draft", "Review the saved support case and its order facts."
    elif event_name == "approval_requested":
        title, explanation = "Human review requested", "The proposed action and evidence were saved for an operator decision."
    elif event_name in ("approval_approved", "approval_rejected"):
        title = "Operator approved" if event_name == "approval_approved" else "Operator rejected"
        explanation = "The decision is recorded; approval alone does not prove an action completed." if event_name == "approval_approved" else "The action did not execute."
    elif event_name.startswith("action_execution_"):
        state = event_name.removeprefix("action_execution_").replace("_", " ")
        title = f"Mock action: {state}"
        attempt = details.get("attempt")
        explanation = f"Attempt {attempt}. " if isinstance(attempt, int) else ""
        explanation += "The saved execution and retry state determine the next action."
    elif event_name == "execution_outcome_reconciled":
        title = "Operator reconciled uncertain outcome"
        explanation = f"Conclusion: {str(details.get('outcome') or 'unknown').replace('_', ' ')}. The original execution remains recorded."
    elif event_name == "approval_replaced_with_fresh_evidence":
        title = "Approval replaced after evidence review"
        explanation = f"New approval #{details.get('replacement_approval_id')}; the original decision and attempts remain in history."
    elif event_name == "approval_refreshed_from_previous_revision":
        title = "Fresh approval requested"
        explanation = f"Replaces approval #{details.get('previous_approval_id')}."
    elif event_name == "support_draft_refreshed_for_approval":
        title, explanation = "Draft refreshed for new review", "The fresh proposal uses current source facts."
    elif event_name == "preview_source_changed_after_approval":
        title, explanation = "Source facts changed", "Compare the reviewed snapshot with the current request before deciding again."
    route = {"order": "orders", "case": "cases", "approval": "approvals"}.get(event.entity_type)
    return {
        "event": event, "title": title, "explanation": explanation,
        "href": f"/{route}/{event.entity_id}?from_scenario={scenario_id}" if route else None,
    }


def build_scenario_view(db, scenario):
    current = scenario["current"]
    execution = scenario["executions"].get(f"approval:{current.id}")
    reconciliation = scenario["reconciliations"].get(current.id)
    blocker_id = unresolved_related_approvals(db, [current.id]).get(current.id)
    if not blocker_id:
        related_execution = prior_terminal_execution(db, current)
        if related_execution and related_execution.idempotency_key.startswith("approval:"):
            related_id = related_execution.idempotency_key.removeprefix("approval:")
            blocker_id = int(related_id) if related_id.isdigit() else None
    blocking_approval = db.get(Approval, blocker_id) if blocker_id else None
    status = current.execution_status
    freshness_differences = approval_freshness_differences(db, current)
    if blocking_approval and blocking_approval.execution_status in ("ambiguous", "disputed"):
        next_step = "A related execution has unresolved evidence. Review its original approval; any completed result remains recorded."
    elif blocking_approval:
        next_step = "A related execution is recorded as completed or still in progress. Inspect its approval before taking further action."
    elif current.status == "pending" and freshness_differences:
        next_step = "The reviewed evidence is stale. Compare the saved snapshot with current facts before creating a replacement approval."
    elif current.status == "pending":
        next_step = "Review the evidence and approve or reject the pending decision."
    elif current.status == "rejected":
        next_step = "This scenario is complete; the rejected action has no execution record."
    elif status == "succeeded":
        next_step = "This scenario is complete; inspect the persisted mock execution and audit events."
    elif status == "ambiguous" and reconciliation is None:
        next_step = "Record what is known about the ambiguous outcome. No provider lookup is performed."
    elif status in ("ambiguous", "disputed") and reconciliation and reconciliation.outcome in ("still_unknown", "conflicting_evidence"):
        next_step = "The outcome remains uncertain. Append new supporting evidence; retries and replacement approvals stay blocked."
    elif status == "reconciled_completed":
        next_step = "This scenario is complete by operator assertion; the original execution remains ambiguous."
    elif status == "reconciled_not_executed":
        next_step = "Create a fresh pending approval before any new mock action."
    elif status == "retryable" and current.next_attempt_at and as_utc(current.next_attempt_at) > utcnow():
        next_step = f"Wait for the recorded retry deadline: {current.next_attempt_at.strftime('%d %b %Y · %H:%M UTC')}."
    elif status == "retryable":
        next_step = "Open the approval and run one explicit local mock recovery attempt."
    elif status == "exhausted":
        next_step = "Review the attempt history, then explicitly queue a manual recovery attempt."
    elif status == "evidence_changed":
        next_step = "Compare reviewed and current facts before creating a replacement approval."
    elif status == "invalid_action":
        next_step = "This action has no registered mock adapter. Inspect the blocked approval contract; no execution or retry is available."
    else:
        next_step = "Open the approval to review its supported recovery action."

    automation_complete = bool(scenario["case"].draft and scenario["case"].status != "queued")
    decision_complete = current.status in ("approved", "rejected")
    result_complete = not blocking_approval and (current.status == "rejected" or status in (
        "succeeded", "reconciled_completed"
    ))
    if blocking_approval and blocking_approval.execution_status in ("ambiguous", "disputed"):
        outcome_detail = f"Related approval #{blocking_approval.id} has unresolved execution evidence. Inspect its original record."
    elif blocking_approval:
        outcome_detail = f"Related approval #{blocking_approval.id} has a prior execution. Inspect that outcome before acting."
    elif current.status == "rejected":
        outcome_detail = "Rejected by the operator; no action execution was created."
    elif status == "reconciled_not_executed":
        outcome_detail = "Operator confirmed non-execution; the original attempt remains ambiguous. Create a fresh approval before acting."
    elif status == "reconciled_completed":
        outcome_detail = "Operator recorded completion; the original execution remains ambiguous and no provider was queried."
    elif execution:
        outcome_detail = (
            f"Mock execution: {execution.state.replace('_', ' ')} after "
            f"{execution.attempts} attempt{'s' if execution.attempts != 1 else ''}. {execution.detail or ''}"
        ).strip()
    else:
        outcome_detail = f"{status.replace('_', ' ')} · {current.attempts} attempts."
    trigger_detail = (
        f"Customer inquiry: {scenario['case'].message}"
        if scenario["case"]
        else "Synthetic customer inquiry is unavailable."
    )
    automation_detail = (
        f"Synthetic order {scenario['order'].order_number} was linked; automation "
        f"saved an {scenario['case'].category.replace('_', ' ')} reply draft. "
        "Preview does not run n8n."
        if scenario["order"] and scenario["case"]
        else f"Support draft is {scenario['case'].status.replace('_', ' ')}."
    )
    decision_detail = (
        f"{current.status.capitalize()} · {current.proposed_action.replace('_', ' ')}"
        + (f" by {current.actor} at {current.decision_at.strftime('%d %b %Y %H:%M UTC')}"
           if current.actor and current.decision_at else " · awaiting operator review")
    )
    stages = [
        {"name": "Trigger", "state": "complete" if scenario["order"] and scenario["case"] else "blocked",
         "detail": trigger_detail,
         "href": f"/orders/{scenario['order'].id}?from_scenario={scenario['scenario_id']}" if scenario["order"] else ""},
        {"name": "Automation", "state": "complete" if automation_complete else "waiting",
         "detail": automation_detail,
         "href": f"/cases/{scenario['case'].id}?from_scenario={scenario['scenario_id']}"},
        {"name": "Human decision", "state": "complete" if decision_complete else "attention" if freshness_differences else "current",
         "detail": "Evidence changed after this approval was created; review current facts before deciding." if freshness_differences else decision_detail,
         "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"},
        {"name": "Persisted outcome", "state": "complete" if result_complete else "attention",
         "detail": outcome_detail,
         "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"},
    ]
    return {
            "scenario": scenario,
            "scenario_objective": SCENARIO_GUIDANCE[scenario["scenario_key"]],
            "current": current,
            "execution": execution,
            "reconciliation": reconciliation,
            "blocking_approval": blocking_approval,
            "preview_waiting": bool(current.next_attempt_at and as_utc(current.next_attempt_at) > utcnow()),
            "next_step": next_step,
            "stages": stages,
            "approvals": scenario["approvals"],
            "events": scenario["events"],
            "journey_events": [_journey_event(item, scenario["scenario_id"])
                               for item in scenario["events"]],
            "reconciliable": bool(execution and execution.state == "ambiguous"
                                   and not current.replacement_approval_id
                                   and current.execution_status in ("ambiguous", "disputed", "reconciled_completed", "reconciled_not_executed")),
            "reconciliation_history": scenario["reconciliation_history"],
            "reconciliation_page": scenario["reconciliation_page"],
            "reconciliation_pages": scenario["reconciliation_pages"],
            "approval_history": scenario["approval_history"],
            "approval_page": scenario["approval_page"],
            "approval_pages": scenario["approval_pages"],
            "audit_page": scenario["audit_page"],
            "audit_pages": scenario["audit_pages"],
            "freshness_differences": freshness_differences,
    }
