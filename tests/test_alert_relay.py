import json
import pytest
from scripts.check_operations import update_outbox
from scripts.relay_alerts import LocalReceiptSink, relay_pending


def sample_outbox(path):
    return update_outbox(path, {"status": "attention", "alerts": [
        {"code": "orders_overdue", "severity": "warning", "message": "Synthetic", "action": "Inspect"},
        {"code": "support_overdue", "severity": "warning", "message": "Synthetic", "action": "Inspect"},
    ]})


def test_receiver_failure_and_crash_replay(tmp_path):
    path = tmp_path / "outbox.json"
    events = sample_outbox(path)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    sink = LocalReceiptSink(receipts)

    def interrupted(event):
        sink(event)
        raise OSError("synthetic crash before acknowledgement")

    with pytest.raises(OSError):
        relay_pending(path, interrupted)
    assert update_outbox(path) == events
    first_receipt = receipts / (events[0]["event_id"] + ".json")
    before = first_receipt.stat().st_ino
    assert relay_pending(path, sink, limit=1) == 1
    assert first_receipt.stat().st_ino == before
    assert json.loads(first_receipt.read_text()) == events[0]
    assert update_outbox(path) == events[1:]
    assert relay_pending(path, sink) == 1
    assert update_outbox(path) == []
    assert len(list(receipts.glob("*.json"))) == 2


def test_conflicting_receipt_preserves_event(tmp_path):
    path = tmp_path / "outbox.json"
    events = sample_outbox(path)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    sink = LocalReceiptSink(receipts)
    conflicting = receipts / (events[0]["event_id"] + ".json")
    conflicting.write_text("{}")
    conflicting.chmod(0o600)
    with pytest.raises(ValueError, match="Conflicting"):
        relay_pending(path, sink)
    assert update_outbox(path) == events
    assert conflicting.read_text() == "{}"
