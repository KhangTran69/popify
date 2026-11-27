"""Limit newly persisted webhook payloads to fields used by PoPify."""
import math

# These fields support order identity, timestamp ordering, support matching and policy.
ORDER_FIELDS = {
    "id": 100, "order_id": 100, "name": 100, "order_number": 100,
    "email": 320, "currency": 8, "total_price": 32, "total": 32,
    "financial_status": 40, "fulfillment_status": 40,
    "created_at": 80, "updated_at": 80,
}


def minimized_order_payload(payload):
    result = {}
    for key, limit in ORDER_FIELDS.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("Invalid order field")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Invalid order number")
            str(value).encode("utf-8", errors="strict")
            if len(str(value)) > limit or "\x00" in str(value):
                raise ValueError("Invalid order field")
            # These fields are consumed as strings by the database or timestamp parser.
            if key not in {"id", "order_id", "order_number", "total_price", "total"} and not isinstance(value, str):
                raise ValueError("Invalid order field")
        result[key] = value
    return result


def valid_text_fields(*values):
    """Accept database-safe text without silently rewriting signed or idempotent data."""
    try:
        return all(value is None or ("\x00" not in value and bool(value.encode("utf-8", errors="strict") or value == "")) for value in values)
    except (UnicodeError, TypeError, AttributeError):
        return False
