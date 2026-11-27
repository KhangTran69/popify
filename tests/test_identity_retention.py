from datetime import timedelta
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.db import Base
from app.identity import purge_expired_identity
from app.models import OperatorAccount, OperatorSession, LoginBucket, Audit, utcnow
from scripts.privacy_inventory import inventory


def test_only_expired_identity_artifacts_are_removed():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = utcnow()
    with Session(engine) as db:
        account = OperatorAccount(username="synthetic", role="admin", password_hash="not-a-real-hash")
        db.add(account)
        db.flush()
        for key, offset in (("expired", -1), ("active", 1)):
            expiry = now + timedelta(minutes=offset)
            db.add(OperatorSession(token_hash=key, account_id=account.id, expires_at=expiry))
            db.add(LoginBucket(key=key, failures=1, expires_at=expiry))
        db.add(Audit(entity_type="operator", entity_id=account.id, event="synthetic", correlation_id="synthetic"))
        db.commit()
        counts = {"expired_sessions": 1, "expired_login_buckets": 1}
        assert purge_expired_identity(db, now=now) == counts
        assert inventory(db, 30)["authentication_artifacts"] == counts
        assert db.get(OperatorSession, "expired") is not None
        assert purge_expired_identity(db, apply=True, now=now) == counts
        assert db.get(OperatorSession, "expired") is None
        assert db.get(LoginBucket, "expired") is None
        assert db.get(OperatorSession, "active") is not None
        assert db.get(LoginBucket, "active") is not None
        assert db.scalar(select(func.count()).select_from(OperatorAccount)) == 1
        assert db.scalar(select(func.count()).select_from(Audit)) == 1
        assert purge_expired_identity(db, apply=True, now=now) == {"expired_sessions": 0, "expired_login_buckets": 0}
    engine.dispose()
