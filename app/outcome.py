"""Interpret terminal approval claims using their persisted execution record."""


def approval_outcome_issue(approval, execution):
    if approval.status == "rejected" and execution is not None:
        return "rejected_with_execution"
    if approval.execution_status == "succeeded" and (
        approval.status != "approved" or execution is None or execution.state != "succeeded"
    ):
        return "success_without_execution"
    return None
