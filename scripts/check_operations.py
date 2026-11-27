"""Read-only operational check for a trusted scheduler; exits 0/1/2.

Run with the application's database configuration and a least-privilege
read-only database account. No customer payloads or credentials are emitted.
"""
import json
from app.db import SessionLocal
from app.operations import operational_snapshot


def main():
    try:
        with SessionLocal() as db:
            snapshot = operational_snapshot(db)
    except Exception:
        print(json.dumps({"status": "unavailable", "alerts": [{
            "code": "database_unavailable", "severity": "critical",
            "message": "Operational snapshot unavailable.",
            "action": "Check database connectivity and schema readiness."
        }]}))
        return 2
    print(json.dumps(snapshot))
    return 1 if snapshot["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
