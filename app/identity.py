"""Database-backed identity and sessions for the single-store operator console."""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta, timezone

from sqlalchemy import delete, func, select, text
from app.models import OperatorAccount, OperatorSession, LoginBucket, Audit, utcnow
from app.config import settings

ROLES = ("admin", "support", "reviewer", "auditor")
SESSION_HOURS = 8
ITERATIONS = 600_000
WINDOW_SECONDS = 900
MAX_FAILURES = 5
MAX_BUCKETS = 10_000
USERNAME = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
RESERVED = {"system", "preview", "n8n", "shopify", "integration", "worker", "mock", "unknown", "deployment_admin"}
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


def purge_expired_identity(db, *, apply=False, now=None):
    """Remove only expired authentication artifacts, never business or audit data."""
    now = now or utcnow()
    if apply and db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(607291041)"))
    results = {}
    for name, model in (("expired_sessions", OperatorSession), ("expired_login_buckets", LoginBucket)):
        predicate = model.expires_at <= now
        if apply:
            results[name] = db.execute(delete(model).where(predicate)).rowcount
        else:
            results[name] = db.scalar(select(func.count()).select_from(model).where(predicate)) or 0
    if apply:
        db.commit()
    return results


def managed_login(db, username, password, client_address, *, previous_token=None):
    """Serialize the bounded login budget across PostgreSQL workers.

    The lock covers only login requests, not normal operator or worker activity.
    A gateway must also limit total traffic before it reaches the application.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(607291041)"))
    now = utcnow()
    db.execute(delete(LoginBucket).where(LoginBucket.expires_at <= now))
    normalized = canonical_username(username)
    bucket_account = normalized if USERNAME.fullmatch(normalized) else "invalid-username"
    keys = [hmac.new(settings.app_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
            for value in ("ip:" + client_address, "account:" + bucket_account)]
    buckets = [db.get(LoginBucket, key) for key in keys]
    waits = [max(1, int((_aware(b.expires_at) - now).total_seconds()) + 1)
             for b in buckets if b and b.failures >= MAX_FAILURES]
    if waits:
        db.commit()
        return None, None, max(waits)
    account = db.scalar(select(OperatorAccount).where(
        OperatorAccount.username == normalized)) if USERNAME.fullmatch(normalized) else None
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
    # Successful reauthentication replaces the presented session, even on account switch.
    if isinstance(previous_token, str) and previous_token.isascii() and 32 <= len(previous_token) <= 128:
        db.execute(delete(OperatorSession).where(OperatorSession.token_hash == _digest(previous_token)))
    # Limit active sessions per account without weakening revocation.
    older = list(db.scalars(select(OperatorSession.token_hash).where(
        OperatorSession.account_id == account.id).order_by(OperatorSession.expires_at.desc()).offset(4)))
    if older:
        db.execute(delete(OperatorSession).where(OperatorSession.token_hash.in_(older)))
    token = secrets.token_urlsafe(32)
    db.add(OperatorSession(token_hash=_digest(token), account_id=account.id,
                           expires_at=now + timedelta(hours=SESSION_HOURS)))
    _event(db, account, "operator_signed_in", account.username)
    db.commit()
    return account, token, 0


def session_account(db, token, *, refresh=False, lock_mutation=False):
    """Resolve current identity in one snapshot; optionally lock through mutation commit.

    Lock account before session, matching account-management order. Revalidate the
    token after acquiring the account lock, because revocation may have won the race.
    """
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        return None
    digest = _digest(token)
    query = select(OperatorAccount).join(
        OperatorSession, OperatorSession.account_id == OperatorAccount.id
    ).where(OperatorSession.token_hash == digest, OperatorSession.expires_at > utcnow(),
            OperatorAccount.active.is_(True)).execution_options(populate_existing=True)
    account = db.scalar(query)
    if account is None or not lock_mutation or db.get_bind().dialect.name != "postgresql":
        return account
    account = db.scalar(select(OperatorAccount).where(OperatorAccount.id == account.id)
                        .with_for_update(read=True).execution_options(populate_existing=True))
    if not account or not account.active:
        return None
    session = db.scalar(select(OperatorSession).where(
        OperatorSession.token_hash == digest, OperatorSession.account_id == account.id,
        OperatorSession.expires_at > utcnow()
    ).with_for_update(read=True).execution_options(populate_existing=True))
    return account if session else None


def revoke_session(db, token, account):
    if isinstance(token, str) and token.isascii() and len(token) <= 128:
        db.execute(delete(OperatorSession).where(OperatorSession.token_hash == _digest(token),
                                                 OperatorSession.account_id == account.id))
    _event(db, account, "operator_signed_out", account.username)
    db.commit()


def manage_account(db, username, action, role=None, password=None, *, actor="deployment_admin", protect_last_admin=False, administrator_token=None):
    """Manage a stable operator identity under a trusted administrator boundary."""
    validate_username(username)
    # Serialize with login to prevent account changes racing new sessions.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(607291041)"))
    if protect_last_admin:
        # Acquire the acting account/session locks only after the global
        # account-management lock. Explicit sign-out does not take that
        # global lock, so revalidation alone cannot serialize with logout.
        administrator = session_account(db, administrator_token, lock_mutation=True)
        if not administrator or administrator.role != "admin" or administrator.username != actor:
            raise PermissionError("Administrator session no longer authorized.")
    account = db.scalar(select(OperatorAccount).where(OperatorAccount.username == username).with_for_update())
    if protect_last_admin and account and account.active and account.role == "admin" and (
        action == "disable" or (action == "role" and role != "admin")
    ):
        remaining = db.scalar(select(func.count()).select_from(OperatorAccount).where(
            OperatorAccount.active.is_(True), OperatorAccount.role == "admin", OperatorAccount.id != account.id))
        if not remaining:
            raise ValueError("At least one active administrator must remain.")
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
    _event(db, account, "operator_account_" + action, actor)
    db.commit()
