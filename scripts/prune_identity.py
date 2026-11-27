"""Trusted local maintenance: preview or remove expired auth artifacts only."""
import argparse
import json
from app.db import SessionLocal
from app.identity import purge_expired_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="delete expired sessions and expired login-rate buckets; default dry run")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        result = purge_expired_identity(db, apply=args.apply)
    print(json.dumps({"mode": "applied" if args.apply else "dry_run", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
