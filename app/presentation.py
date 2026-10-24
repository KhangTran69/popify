"""Small, validated presentation helpers for operator record pages."""

from urllib.parse import urlencode
from datetime import datetime

from app.services import as_utc
from app.models import utcnow


QUEUE_STATES = {
    "orders": {"unfulfilled", "fulfilled", "partial"},
    "cases": {"queued", "needs_review", "resolved_mock", "failed", "exhausted"},
    "approvals": {"pending", "approved", "rejected"},
    "failures": {"retryable", "exhausted", "ambiguous"},
    "deliveries": {"received", "processed", "failed", "exhausted"},
}
DEFAULT_QUEUE = {
    "order": "orders", "case": "cases", "approval": "approvals", "delivery": "deliveries"
}


def record_navigation(query, kind=None):
    """Return safe detail URL suffix and a local back URL."""
    demo_id = query.get("from_demo", "")
    if len(demo_id) <= 10 and demo_id.isascii() and demo_id.isdecimal() and 0 < int(demo_id) < 2**31:
        suffix = "?" + urlencode({"from_demo": demo_id})
        return suffix, f"/demo/{demo_id}"

    section = query.get("from_queue", "")
    if section not in QUEUE_STATES:
        return "", "/queues/" + DEFAULT_QUEUE.get(kind, "approvals")
    params = {"from_queue": section}
    q = query.get("q", "")
    if q and len(q) <= 150:
        params["q"] = q
    status = query.get("status", "")
    if status in QUEUE_STATES[section]:
        params["status"] = status
    page = query.get("page", "")
    if len(page) <= 7 and page.isascii() and page.isdecimal() and 0 < int(page) <= 1000000:
        params["page"] = str(int(page))
    suffix = "?" + urlencode(params)
    return suffix, "/queues/" + section + (
        "?" + urlencode({k: v for k, v in params.items() if k != "from_queue"})
        if len(params) > 1 else ""
    )


def retry_guidance(deadline):
    if deadline is None:
        return "Eligible on the next worker run after requeue."
    if as_utc(deadline) > utcnow():
        return "Waiting for the existing backoff deadline. Requeue will not move it earlier."
    return "Eligible now. Requeue makes this available to the next worker run."


def snapshot_utc(value):
    if not value:
        return "Not recorded"
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00"))).strftime(
            "%d %b %Y · %H:%M UTC"
        )
    except ValueError:
        return str(value)
