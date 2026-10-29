"""Provider-neutral outbound action contract and local mock implementation."""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.config import settings


ActionState = Literal["succeeded", "retryable", "exhausted", "ambiguous"]


@dataclass(frozen=True)
class ActionRequest:
    kind: str
    subject_type: str
    subject_id: int
    action_name: str
    reviewed_evidence: dict
    evidence_hash: str
    idempotency_key: str
    attempt: int

    @property
    def action(self):
        return self.kind, self.subject_type, self.action_name


@dataclass(frozen=True)
class ActionResult:
    state: ActionState
    detail: str


class ActionAdapter(Protocol):
    def execute(self, request: ActionRequest) -> ActionResult: ...


class MockActionAdapter:
    """Deterministic local outcome simulator controlled by existing settings."""

    def __init__(self, outcome=None):
        self.outcome = outcome

    def execute(self, request: ActionRequest) -> ActionResult:
        if self.outcome == "ambiguous" or (
            self.outcome is None and settings.mock_action_ambiguous
        ):
            return ActionResult(
                "ambiguous",
                "Simulated timeout after submission. Confirm provider state before resolving; automatic retry is blocked.",
            )
        if self.outcome in ("retryable", "exhausted"):
            return ActionResult(
                self.outcome,
                "Simulated transient failure; no external request was made.",
            )
        if self.outcome is None and request.attempt <= settings.mock_action_failures:
            state = (
                "retryable"
                if request.attempt < settings.max_action_attempts
                else "exhausted"
            )
            return ActionResult(
                state, "Simulated transient failure; no external request was made."
            )
        return ActionResult("succeeded", "Mock action completed; no external API called.")


def normalize_adapter_result(adapter, request):
    """Convert adapter uncertainty into a safe, non-retryable outcome."""
    try:
        result = adapter.execute(request)
    except Exception:
        return ActionResult(
            "ambiguous",
            "Adapter execution outcome is uncertain. Confirm state before resolving; automatic retry is blocked.",
        )
    if (
        not isinstance(result, ActionResult)
        or not isinstance(result.state, str)
        or result.state not in {"succeeded", "retryable", "exhausted", "ambiguous"}
        or not isinstance(result.detail, str)
        or not result.detail.strip()
    ):
        return ActionResult(
            "ambiguous",
            "Adapter returned an invalid result. Confirm state before resolving; automatic retry is blocked.",
        )
    return ActionResult(result.state, result.detail.strip()[:240])
