"""Read-only operational check for a trusted scheduler; exits 0/1/2.

Optional local state tracks alert transitions without sending external messages.
Use a trusted private directory and one state file per monitored environment.
"""
import argparse
import json
import os
import tempfile
import time
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


def update_state(path, snapshot, repeat_seconds):
    path = Path(path)
    if not path.parent.is_dir() or path.is_symlink():
        raise ValueError("Use a trusted existing directory and regular state file.")
    lock_path = path.with_name(path.name + ".lock")
    # Non-following, private lock file. Scheduler directories must be access-controlled.
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(previous, dict) or len(previous) > 100 or any(
            not isinstance(key, str) or len(key) > 100 or not isinstance(value, int) or value < 0
            for key, value in previous.items()
        ):
            raise ValueError("Invalid monitor state")
        state, events = alert_transitions(previous, snapshot, int(time.time()), repeat_seconds)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".monitor-", delete=False) as output:
                temp = output.name
                json.dump(state, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
        return events


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", help="private local JSON state file for transition deduplication")
    parser.add_argument("--repeat-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    if not 60 <= args.repeat_seconds <= 86400:
        parser.error("repeat-seconds must be between 60 and 86400")
    try:
        with SessionLocal() as db:
            snapshot = operational_snapshot(db)
    except Exception:
        snapshot = {"status": "unavailable", "alerts": [{
            "code": "database_unavailable", "severity": "critical",
            "message": "Operational snapshot unavailable.",
            "action": "Check database connectivity and schema readiness."
        }]}
    if args.state_file:
        try:
            snapshot["transitions"] = update_state(args.state_file, snapshot, args.repeat_seconds)
        except (OSError, ValueError):
            snapshot["state_error"] = "Unable to update monitor state; inspect the private state directory."
            print(json.dumps(snapshot))
            return 2
    print(json.dumps(snapshot))
    return 2 if snapshot["status"] == "unavailable" else 1 if snapshot["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
