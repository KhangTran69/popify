"""Database-backed identity and sessions for the single-store operator console."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta, timezone

from sqlalchemy import delete, func, select, text
from app.models import OperatorAccount, OperatorSession, LoginBucket, Audit, utcnow
from app.security import secure_equal

ROLES = ("admin", "support", "reviewer", "auditor")
SESSION_HOURS = 8
ITERATIONS = 600_000
WINDOW_SECONDS = 900
MAX_FAILURES = 5
MAX_BUCKETS = 10_000
USERNAME = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
RESERVED = {"system", "preview", "n8n", "shopify", "integration", "worker", "mock", "unknown"}
DUMMY_HASH = "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32


def canonical_username(value):
    return value.strip().lower()


def validate_username(value):
    if not USERNAME.fullmatch(value) or value in RESERVED:
        raise ValueError("Use a unique lowercase operator name (3–64 ASCII characters).")


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12–256 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    if len(password) > 256:
        return False
    try:
        scheme, rounds, salt, expected = encoded.split("$")
        if scheme != "pbkdf2_sha256" or int(rounds) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, UnicodeError):
        return False


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _event(db, account, event, actor):
    db.add(Audit(entity_type="operator", entity_id=account.id, event=event,
                 actor=actor, correlation_id=secrets.token_hex(16), details={}))


def managed_login(db, username, password, client_address):
    """Serialize the bounded login budget across PostgreSQL workers.

    The lock covers only login requests, not normal operator or worker activity.
    A gateway must also limit total traffic before it reaches the application.
    """
    now = utcnow()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(607291041)"))
    db.execute(delete(LoginBucket).where(LoginBucket.expires_at <= now))
    keys = [_digest("ip:" + client_address), _digest("account:" + canonical_username(username))]
    buckets = [db.get(LoginBucket, key) for key in keys]
    waits = [max(1, int((_aware(b.expires_at) - now).total_seconds()) + 1)
             for b in buckets if b and b.failures >= MAX_FAILURES]
    if waits:
        db.commit()
        return None, None, max(waits)
    account = db.scalar(select(OperatorAccount).where(
        OperatorAccount.username == canonical_username(username)))
    valid = verify_password(password, account.password_hash if account else DUMMY_HASH)
    if not account or not account.active or not valid:
        count = db.scalar(select(func.count()).select_from(LoginBucket)) or 0
        missing = sum(b is None for b in buckets)
        if count + missing > MAX_BUCKETS:
            db.commit()
            return None, None, WINDOW_SECONDS
        for key, bucket in zip(keys, buckets):
            if bucket is None:
                db.add(LoginBucket(key=key, failures=1, expires_at=now + timedelta(seconds=WINDOW_SECONDS)))
            else:
                bucket.failures += 1
        db.commit()
        return None, None, 0
    # Do not clear the shared IP failure bucket on successful login.
    if buckets[1] is not None:
        db.delete(buckets[1])
    db.execute(delete(OperatorSession).where(OperatorSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    db.add(OperatorSession(token_hash=_digest(token), account_id=account.id,
                           expires_at=now + timedelta(hours=SESSION_HOURS)))
    _event(db, account, "operator_signed_in", account.username)
    db.commit()
    return account, token, 0


def session_account(db, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        return None
    session = db.get(OperatorSession, _digest(token))
    if not session or _aware(session.expires_at) <= utcnow():
        return None
    account = db.get(OperatorAccount, session.account_id)
    return account if account and account.active else None


def revoke_session(db, token, account):
    if isinstance(token, str) and token.isascii() and len(token) <= 128:
        db.execute(delete(OperatorSession).where(OperatorSession.token_hash == _digest(token),
                                                 OperatorSession.account_id == account.id))
    _event(db, account, "operator_signed_out", account.username)
    db.commit()


def manage_account(db, username, action, role=None, password=None):
    """Called only by the trusted deployment administrator CLI."""
    validate_username(username)
    # Serialize with login to prevent account changes racing new sessions.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(607291041)"))
    account = db.scalar(select(OperatorAccount).where(OperatorAccount.username == username).with_for_update())
    if action == "create":
        if account:
            raise ValueError("Account already exists; usernames cannot be reused.")
        if role not in ROLES:
            raise ValueError("Invalid role.")
        account = OperatorAccount(username=username, role=role, password_hash=password_hash(password), active=True)
        db.add(account)
        db.flush()
    elif not account:
        raise ValueError("Account not found.")
    elif action == "password":
        account.password_hash = password_hash(password)
    elif action == "role":
        if role not in ROLES:
            raise ValueError("Invalid role.")
        account.role = role
    elif action == "disable":
        account.active = False
    elif action == "enable":
        account.active = True
    elif action != "revoke":
        raise ValueError("Unknown account action.")
    db.execute(delete(OperatorSession).where(OperatorSession.account_id == account.id))
    _event(db, account, "operator_account_" + action, "deployment_admin")
    db.commit()
