"""Seed only a synthetic session for the disposable logical restore exercise."""
import argparse
import os
from app.db import SessionLocal
from app.identity import manage_account, managed_login
from app.config import settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable", action="store_true", required=True)
    parser.parse_args()
    assert os.environ.get("POPIFY_ENV") == "development" and settings.llm_mode == "mock"
    with SessionLocal() as db:
        manage_account(db, "ci-restore-user", "create", role="auditor", password="synthetic-restore-only-password")
        account, token, wait = managed_login(db, "ci-restore-user", "synthetic-restore-only-password", "ci-restore")
        assert account and token and not wait
    print("PASS: synthetic session present for restore isolation check")


if __name__ == "__main__":
    main()
