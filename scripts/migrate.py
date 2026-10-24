from pathlib import Path
from sqlalchemy import text
from app.db import engine

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
with engine.begin() as connection:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(120) PRIMARY KEY, applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())"
        )
    )
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        version = migration.name
        applied = connection.execute(
            text("SELECT 1 FROM schema_migrations WHERE version=:version"),
            {"version": version},
        ).first()
        if applied:
            continue
        for statement in migration.read_text().split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.execute(
            text("INSERT INTO schema_migrations(version) VALUES (:version)"),
            {"version": version},
        )
        print(f"Applied {version}")
