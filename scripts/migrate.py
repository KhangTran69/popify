"""Apply ordered PostgreSQL migrations atomically, serialized across API startups."""
from pathlib import Path
from sqlalchemy import text
from app.db import engine

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def migrate(target=engine):
    if target.dialect.name != "postgresql":
        raise RuntimeError("Production migrations require PostgreSQL.")
    with target.begin() as connection:
        # Serialize competing migrators before checking or creating the ledger.
        connection.execute(text("SELECT pg_advisory_xact_lock(607291042)"))
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(120) PRIMARY KEY, applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())"
        ))
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            version = migration.name
            applied = connection.execute(text("SELECT 1 FROM schema_migrations WHERE version=:version"),
                                         {"version": version}).first()
            if applied:
                continue
            for statement in migration.read_text().split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                               {"version": version})
            print(f"Applied {version}")


if __name__ == "__main__":
    migrate()
