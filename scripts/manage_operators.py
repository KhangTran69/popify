"""Provision individual operators from a trusted deployment terminal.

No password is accepted on the command line or printed.
"""
import argparse
import getpass
from app.db import SessionLocal
from app.identity import ROLES, manage_account


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "password", "role", "disable", "enable", "revoke"))
    parser.add_argument("username")
    parser.add_argument("--role", choices=ROLES)
    args = parser.parse_args()
    if args.action in ("create", "role") and not args.role:
        parser.error("--role is required")
    password = None
    if args.action in ("create", "password"):
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match")
    with SessionLocal() as db:
        try:
            manage_account(db, args.username, args.action, role=args.role, password=password)
        except ValueError as exc:
            parser.error(str(exc))
    print("Operator account updated; existing sessions revoked.")


if __name__ == "__main__":
    main()
