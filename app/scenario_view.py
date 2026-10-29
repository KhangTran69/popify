"""Presentation state for the isolated scenario journey.

Kept outside the route so the decision, recovery and stage copy share one
persisted-state interpretation. This module never mutates an approval.
"""

from app.models import Approval, utcnow
from app.services import approval_freshness_differences, as_utc, prior_terminal_execution
from app.preview_scenarios import SCENARIO_GUIDANCE, unresolved_related_approvals
from app.record_journey import journey_event
from app.outcome import approval_outcome_issue


def build_scenario_view(db, scenario):
    current = scenario["current"]
    execution = scenario["executions"].get(f"approval:{current.id}")
    outcome_issue = approval_outcome_issue(current, execution)
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
    elif outcome_issue:
        next_step = "Decision and execution records disagree. Inspect the saved history and ask a maintainer to investigate."
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
    execution_persisted_success = bool(execution and execution.state == "succeeded")
    assertion_persisted_complete = bool(
        status == "reconciled_completed" and reconciliation
        and reconciliation.outcome == "confirmed_completed"
    )
    result_complete = not blocking_approval and not outcome_issue and (
        (current.status == "rejected" and execution is None)
        or (status == "succeeded" and execution_persisted_success)
        or assertion_persisted_complete
    )
    if blocking_approval and blocking_approval.execution_status in ("ambiguous", "disputed"):
        outcome_detail = f"Related approval #{blocking_approval.id} has unresolved execution evidence. Inspect its original record."
    elif blocking_approval:
        outcome_detail = f"Related approval #{blocking_approval.id} has a prior execution. Inspect that outcome before acting."
    elif outcome_issue:
        outcome_detail = ("Approval status says succeeded, but no successful persisted execution record exists."
                          if outcome_issue == "success_without_execution"
                          else "The approval is rejected but an execution record also exists.")
    elif current.status == "rejected":
        outcome_detail = (
            "Rejected by the operator; no action execution was created."
            if execution is None
            else "The approval is rejected but an execution record also exists. Ask a maintainer to investigate this inconsistent state."
        )
    elif status == "reconciled_not_executed":
        outcome_detail = "Operator confirmed non-execution; the original attempt remains ambiguous. Create a fresh approval before acting."
    elif status == "reconciled_completed":
        outcome_detail = "Operator recorded completion; the original execution remains ambiguous and no provider was queried."
    elif status == "succeeded" and not execution_persisted_success:
        outcome_detail = "Approval status says succeeded, but no successful persisted execution record exists."
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
    provenance = scenario.get("recovery_provenance")
    prior_recovery, prior_execution, prior_reconciliation = provenance or (None, None, None)
    if blocking_approval and blocking_approval.execution_status in ("ambiguous", "disputed"):
        reviewer_result = {
            "tone": "bad",
            "title": "Recovery blocked by unresolved earlier evidence",
            "summary": (
                f"Approval #{blocking_approval.id} has {blocking_approval.execution_status.replace('_', ' ')} evidence. "
                + (f"The current approval's execution remains separately persisted as {execution.state}. " if execution else "")
                + "Review the earlier approval before treating this journey as complete."
            ),
            "proof": f"Current approval #{current.id} · earlier approval #{blocking_approval.id}",
            "links": [{"label": f"Review blocking approval #{blocking_approval.id}", "href": f"/approvals/{blocking_approval.id}?from_scenario={scenario['scenario_id']}"}],
        }
    elif current.execution_status == "succeeded" and not execution_persisted_success:
        reviewer_result = {
            "tone": "bad",
            "title": "Successful execution record missing",
            "summary": "The approval state says succeeded, but the persisted ActionExecution record is missing or has another state. Investigate this inconsistency before reporting completion.",
            "proof": f"Approval #{current.id} · execution state unavailable",
            "links": [{"label": f"Inspect approval #{current.id}", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"}],
        }
    elif current.execution_status == "succeeded" and prior_recovery and prior_execution and prior_reconciliation and execution_persisted_success:
        reviewer_result = {
            "tone": "good",
            "title": "Recovery completed through a fresh approval",
            "summary": (
                f"Approval #{prior_recovery.id} has a persisted ambiguous execution and the operator's "
                f"{prior_reconciliation.outcome.replace('_', ' ')} assertion ({prior_reconciliation.actor}, "
                f"{prior_reconciliation.created_at.strftime('%d %b %Y %H:%M UTC')}, reference "
                f"{prior_reconciliation.reference or 'none'}). Approval #{current.id} then captured fresh evidence, "
                "received a separate human decision, and persisted a successful local mock execution."
            ),
            "proof": f"Decision by {current.actor or 'unknown'} at {current.decision_at.strftime('%d %b %Y %H:%M UTC') if current.decision_at else 'time not recorded'} · approval revision {current.revision} · execution #{execution.id}, {execution.attempts} attempt{'s' if execution.attempts != 1 else ''} · {prior_reconciliation.correlation_id}",
            "links": [
                {"label": f"Original ambiguous execution and assertion · approval #{prior_recovery.id}", "href": f"/approvals/{prior_recovery.id}?from_scenario={scenario['scenario_id']}#approval-actions"},
                {"label": f"Fresh decision and saved result · approval #{current.id}", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}#operational-journey"},
            ],
        }
    elif current.execution_status == "succeeded" and execution_persisted_success:
        reviewer_result = {
            "tone": "good", "title": "Local mock outcome persisted",
            "summary": "The approval decision and successful ActionExecution are persisted in the disposable preview database. No provider or live workflow was called.",
            "proof": f"Approval #{current.id} · revision {current.revision} · execution #{execution.id} · {execution.attempts} attempt{'s' if execution.attempts != 1 else ''} · decision by {current.actor or 'unknown'}",
            "links": [{"label": f"Inspect approval and execution #{current.id}", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"}],
        }
    elif current.status == "rejected":
        rejected_has_execution = bool(execution)
        reviewer_result = {
            "tone": "bad" if rejected_has_execution else "good",
            "title": "Rejected decision has an execution record" if rejected_has_execution else "Rejection persisted",
            "summary": "The saved approval is rejected but an ActionExecution also exists; investigate before describing this as a clean rejection." if rejected_has_execution else "The human decision is saved and no execution record was created.",
            "proof": f"Approval #{current.id} · {current.actor or 'operator not recorded'} · {current.decision_at.strftime('%d %b %Y %H:%M UTC') if current.decision_at else 'time not recorded'}",
            "links": [{"label": f"Inspect approval #{current.id}", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"}],
        }
    elif current.execution_status == "reconciled_completed":
        reviewer_result = {"tone": "warn", "title": "Closed by operator assertion", "summary": "The original execution remains ambiguous. This conclusion is persisted evidence supplied by an operator, not provider verification.", "proof": f"Approval #{current.id} · no provider queried", "links": [{"label": "Review the operator assertion", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"}]}
    else:
        reviewer_result = {"tone": "warn", "title": "Journey still requires attention", "summary": next_step, "proof": f"Approval #{current.id} · {current.status} · {current.execution_status.replace('_', ' ')}", "links": [{"label": "Open current approval", "href": f"/approvals/{current.id}?from_scenario={scenario['scenario_id']}"}]}
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
            "reviewer_result": reviewer_result,
            "approvals": scenario["approvals"],
            "events": scenario["events"],
            "journey_events": [journey_event(item, scenario_id=scenario["scenario_id"])
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
