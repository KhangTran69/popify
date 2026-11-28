"""Disposable Compose monitor/acknowledgement drill across a real database outage."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from scripts.relay_alerts import relay_pending, LocalReceiptSink

ROOT = Path("/tmp/popify-ci-monitor-private")
STATE = ROOT / "outbox.json"
MARKER = ROOT / "event"
RECEIPTS = ROOT / "receipts"


def poll(*args):
    result = subprocess.run([sys.executable, "-m", "scripts.check_operations", "--outbox-file", str(STATE), *args],
                            capture_output=True, text=True, timeout=30)
    return result.returncode, json.loads(result.stdout)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("unavailable", "recovered"))
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    assert os.environ.get("POPIFY_ENV") == "development"
    if args.stage == "unavailable":
        assert not ROOT.exists()
        ROOT.mkdir(mode=0o700)
        RECEIPTS.mkdir(mode=0o700)
        code, first = poll()
        assert code == 2 and first["status"] == "unavailable"
        event = next(e for e in first["pending_events"] if e["code"] == "database_unavailable")
        assert event["transition"] == "opened"
        code, repeat = poll()
        assert code == 2 and repeat["pending_events"] == first["pending_events"]
        code, listed = poll("--list-pending")
        assert code == 1 and listed["pending_events"] == first["pending_events"]
        # Simulate a relay crash after durable receipt but before acknowledgement.
        sink = LocalReceiptSink(RECEIPTS)
        sink(event)
        assert poll("--list-pending")[1]["pending_events"] == first["pending_events"]
        assert relay_pending(STATE, sink, limit=1) == 1
        assert json.loads((RECEIPTS / (event["event_id"] + ".json")).read_text()) == event
        assert poll("--ack-event", event["event_id"])[0] == 0
        code, listed = poll("--list-pending")
        assert code == 0 and listed["pending_events"] == []
        assert poll()[1]["pending_events"] == []
        MARKER.write_text(event["event_id"])
        print("PASS: real database outage alert persisted, deduplicated and acknowledged without database")
    else:
        assert MARKER.is_file()
        code, result = poll()
        assert code in (0, 1) and result["status"] != "unavailable"
        resolved = [e for e in result["pending_events"] if e["code"] == "database_unavailable"]
        assert len(resolved) == 1 and resolved[0]["transition"] == "resolved"
        assert resolved[0]["event_id"] != MARKER.read_text()
        assert result["handoff"]["pending_count"] >= 1
        assert relay_pending(STATE, LocalReceiptSink(RECEIPTS), limit=500) >= 1
        assert json.loads((RECEIPTS / (resolved[0]["event_id"] + ".json")).read_text()) == resolved[0]
        assert not any(e["code"] == "database_unavailable" for e in poll()[1]["pending_events"])
        for receipt in RECEIPTS.iterdir():
            receipt.unlink()
        RECEIPTS.rmdir()
        print("PASS: recovered database generates distinct durable resolution event and acknowledgement")
        STATE.unlink()
        STATE.with_name(STATE.name + ".lock").unlink()
        MARKER.unlink()
        ROOT.rmdir()


if __name__ == "__main__":
    run()
