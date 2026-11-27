"""Read-only operational check for a trusted scheduler; exits 0/1/2.

Optional local state tracks alert transitions without sending external messages.
Use a trusted private directory and one state file per monitored environment.
"""
import argparse
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
import fcntl
from app.db import SessionLocal
from app.operations import operational_snapshot


def alert_transitions(previous, snapshot, now, repeat_seconds):
    """Return a bounded state and open/reminder/resolved events.

    An unavailable snapshot cannot prove prior business alerts are resolved.
    """
    current = {alert["code"]: alert for alert in snapshot["alerts"]}
    state = {}
    events = []
    for code, alert in current.items():
        last = previous.get(code)
        if last is None or now - last >= repeat_seconds or now < last:
            events.append({**alert, "transition": "opened" if last is None else "reminder"})
            state[code] = now
        else:
            state[code] = last
    for code, last in previous.items():
        if code in current:
            continue
        if snapshot["status"] == "unavailable":
            state[code] = last
        else:
            events.append({"code": code, "transition": "resolved"})
    return state, events


MAX_PENDING_EVENTS = 500


@contextmanager
def locked_state(path):
    path = Path(path)
    if not path.parent.is_dir() or path.is_symlink():
        raise ValueError("Use a trusted existing directory and regular state file.")
    lock_path = path.with_name(path.name + ".lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            # Limit reads and reject symlinks and non-regular files before parsing.
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 512_000:
                raise ValueError("Invalid monitor state file")
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as source:
                previous = json.loads(source.read(512_001))
        else:
            previous = None
        yield path, previous


def atomic_write(path, value):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".monitor-", delete=False) as output:
            temp = output.name
            json.dump(value, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def validate_state(previous):
    if not isinstance(previous, dict) or len(previous) > 100 or any(
        not isinstance(key, str) or len(key) > 100 or not isinstance(value, int) or value < 0
        for key, value in previous.items()
    ):
        raise ValueError("Invalid monitor state")


def update_state(path, snapshot, repeat_seconds):
    with locked_state(path) as (path, previous):
        previous = {} if previous is None else previous
        validate_state(previous)
        state, events = alert_transitions(previous, snapshot, int(time.time()), repeat_seconds)
        atomic_write(path, state)
        return events


def update_outbox(path, snapshot=None, repeat_seconds=900, *, acknowledge=None):
    """Atomically persist alert transitions before exposing them to a relay.

    Pending events survive a failed relay or process restart; acknowledgement
    is explicit and idempotent. The caller must deduplicate by event_id.
    """
    with locked_state(path) as (path, previous):
        data = {"version": 1, "state": {}, "pending": []} if previous is None else previous
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Invalid alert outbox version")
        state, pending = data.get("state"), data.get("pending")
        validate_state(state)
        if not isinstance(pending, list) or len(pending) > MAX_PENDING_EVENTS:
            raise ValueError("Invalid alert outbox")
        ids = set()
        for event in pending:
            if not isinstance(event, dict):
                raise ValueError("Invalid alert event")
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or len(event_id) != 32 or any(c not in "0123456789abcdef" for c in event_id) or event_id in ids:
                raise ValueError("Invalid alert event ID")
            if event.get("transition") not in ("opened", "reminder", "resolved") or not isinstance(event.get("code"), str):
                raise ValueError("Invalid alert transition")
            if not isinstance(event.get("observed_at_epoch"), int) or event["observed_at_epoch"] < 0:
                raise ValueError("Invalid alert event timestamp")
            ids.add(event_id)
        if acknowledge is not None:
            pending = [event for event in pending if event["event_id"] != acknowledge]
        elif snapshot is not None:
            now = int(time.time())
            state, events = alert_transitions(state, snapshot, now, repeat_seconds)
            # Never silently drop alerts; an operator must resolve a saturated relay.
            if len(pending) + len(events) > MAX_PENDING_EVENTS:
                raise ValueError("Alert outbox is full")
            pending = pending + [{**event, "event_id": uuid.uuid4().hex, "observed_at_epoch": now}
                                 for event in events]
        # A relay can drain a saturated outbox without collecting a snapshot
        # or rewriting the durable state. Existing IDs and ordering are preserved.
        if acknowledge is not None or snapshot is not None:
            atomic_write(path, {"version": 1, "state": state, "pending": pending})
        return pending


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--state-file", help="private local JSON state file for transition deduplication")
    mode.add_argument("--outbox-file", help="private durable alert outbox with explicit delivery acknowledgement")
    relay = parser.add_mutually_exclusive_group()
    relay.add_argument("--ack-event", help="acknowledge a delivered event ID; requires --outbox-file")
    relay.add_argument("--list-pending", action="store_true",
                       help="read pending events without polling the database; requires --outbox-file")
    parser.add_argument("--repeat-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    if not 60 <= args.repeat_seconds <= 86400:
        parser.error("repeat-seconds must be between 60 and 86400")
    if args.list_pending:
        if not args.outbox_file:
            parser.error("--list-pending requires --outbox-file")
        try:
            pending = update_outbox(args.outbox_file)
        except (OSError, ValueError):
            print(json.dumps({"status": "unavailable", "state_error": "Unable to read alert outbox."}))
            return 2
        print(json.dumps({"status": "pending" if pending else "clear",
                          "pending_count": len(pending), "pending_events": pending}))
        return 1 if pending else 0
    if args.ack_event:
        if not args.outbox_file or len(args.ack_event) != 32 or any(c not in "0123456789abcdef" for c in args.ack_event):
            parser.error("--ack-event requires --outbox-file and a valid event ID")
        try:
            pending = update_outbox(args.outbox_file, acknowledge=args.ack_event)
        except (OSError, ValueError):
            print(json.dumps({"status": "unavailable", "state_error": "Unable to acknowledge alert event."}))
            return 2
        print(json.dumps({"status": "acknowledged", "pending_count": len(pending)}))
        return 0
    try:
        with SessionLocal() as db:
            snapshot = operational_snapshot(db)
    except Exception:
        snapshot = {"status": "unavailable", "alerts": [{
            "code": "database_unavailable", "severity": "critical",
            "message": "Operational snapshot unavailable.",
            "action": "Check database connectivity and schema readiness."
        }]}
    if args.state_file or args.outbox_file:
        try:
            if args.outbox_file:
                pending = update_outbox(args.outbox_file, snapshot, args.repeat_seconds)
                snapshot["pending_events"] = pending
                snapshot["handoff"] = {"pending_count": len(pending),
                    "oldest_pending_seconds": max(0, int(time.time()) - min(e["observed_at_epoch"] for e in pending)) if pending else None,
                    "status": "awaiting_acknowledgement" if pending else "clear"}
            else:
                snapshot["transitions"] = update_state(args.state_file, snapshot, args.repeat_seconds)
        except (OSError, ValueError):
            snapshot["state_error"] = "Unable to update monitor state; inspect the private state directory."
            print(json.dumps(snapshot))
            return 2
    print(json.dumps(snapshot))
    return 2 if snapshot["status"] == "unavailable" else 1 if snapshot["alerts"] or snapshot.get("pending_events") else 0


if __name__ == "__main__":
    raise SystemExit(main())
