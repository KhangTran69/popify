"""Ordered migration ledger contract shared by startup and readiness."""
from pathlib import Path
from sqlalchemy import text

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def migration_paths():
    paths = sorted(MIGRATIONS.glob("*.sql"))
    if not paths:
        raise RuntimeError("Bundled migration files are missing.")
    return paths


def applied_versions(executor):
    return list(executor.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars())


def ledger_is_current(executor):
    return applied_versions(executor) == [path.name for path in migration_paths()]


def pending_migrations(executor):
    paths = migration_paths()
    expected = [path.name for path in paths]
    applied = applied_versions(executor)
    if applied != expected[:len(applied)]:
        raise RuntimeError("Migration history is not a prefix of bundled migrations; review compatibility before startup.")
    return paths[len(applied):]
