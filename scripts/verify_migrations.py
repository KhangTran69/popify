"""Verify migration 0007 on fresh and populated PostgreSQL schemas."""

from pathlib import Path
import uuid

from sqlalchemy import create_engine, text

from app.config import settings
from app.db import engine

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def apply(connection, path):
    for statement in path.read_text().split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement)


def run():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Migration verification requires PostgreSQL")
    schemas = ["migration_fresh_" + uuid.uuid4().hex,
               "migration_upgrade_" + uuid.uuid4().hex]
    isolated_engines = []
    try:
        with engine.begin() as connection:
            for schema in schemas:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for index, schema in enumerate(schemas):
            isolated = create_engine(
                settings.database_url,
                connect_args={"options": f"-csearch_path={schema}"},
            )
            isolated_engines.append(isolated)
            migrations = sorted(MIGRATIONS.glob("*.sql"))
            if index == 1:
                migrations = migrations[:-1]
            with isolated.begin() as connection:
                for migration in migrations:
                    apply(connection, migration)
                if index == 1:
                    connection.exec_driver_sql("""
                        INSERT INTO approvals
                            (kind, subject_type, subject_id, proposed_action, supporting_facts,
                             status, actor, decision_at, execution_status, attempts,
                             failure_detail, created_at, next_attempt_at, evidence_hash, revision)
                        VALUES ('support', 'case', 1, 'send_reply', '{"draft":"historical"}',
                                'approved', 'legacy-operator', now(), 'ambiguous', 1,
                                'historical uncertain outcome', now(), NULL, NULL, 1)
                    """)
                    connection.exec_driver_sql("""
                        INSERT INTO action_executions
                            (idempotency_key, state, attempts, detail, created_at, updated_at,
                             proposed_action, evidence_hash)
                        VALUES ('approval:1', 'ambiguous', 1, 'historical timeout', now(), now(),
                                'send_reply', NULL)
                    """)
                    apply(connection, MIGRATIONS / "0007_execution_reconciliation.sql")
                    row = connection.execute(text("""
                        SELECT a.status, a.execution_status, a.actor, a.attempts,
                               e.state, e.attempts,
                               (SELECT count(*) FROM execution_reconciliations)
                        FROM approvals a CROSS JOIN action_executions e
                        WHERE a.id = 1 AND e.idempotency_key = 'approval:1'
                    """)).one()
                    assert row == ("approved", "ambiguous", "legacy-operator", 1,
                                   "ambiguous", 1, 0), row
                else:
                    assert connection.exec_driver_sql("""
                        SELECT count(*) FROM execution_reconciliations
                    """).scalar_one() == 0
                    columns = connection.exec_driver_sql("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'execution_reconciliations'
                    """).scalars().all()
                    assert {"approval_id", "action_execution_id", "outcome", "actor",
                            "correlation_id", "synthetic", "created_at"}.issubset(columns)
        print("PASS: migration 0007 applies to a fresh schema and upgrades populated 0001–0006 data without changing the historical ambiguous execution or approval.")
    finally:
        for isolated in isolated_engines:
            isolated.dispose()
        with engine.begin() as connection:
            for schema in schemas:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


if __name__ == "__main__":
    run()
