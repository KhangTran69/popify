"""Verify migration 0008 on fresh and populated PostgreSQL schemas."""

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
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    migration_0007 = MIGRATIONS / "0007_execution_reconciliation.sql"
    migration_0008 = MIGRATIONS / "0008_reconciliation_followups.sql"
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
            with isolated.begin() as connection:
                if index == 0:
                    for migration in migrations:
                        apply(connection, migration)
                    assert connection.exec_driver_sql(
                        "SELECT count(*) FROM execution_reconciliations"
                    ).scalar_one() == 0
                    columns = connection.exec_driver_sql("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'execution_reconciliations'
                    """).scalars().all()
                    assert {"revision", "previous_reconciliation_id", "asserted_outcome"}.issubset(columns)
                else:
                    # Simulate a populated schema at 0006, then preserve a legacy
                    # ambiguous approval and reconciliation while applying 0007/0008.
                    for migration in migrations:
                        if migration.name >= "0007_execution_reconciliation.sql":
                            break
                        apply(connection, migration)
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
                    apply(connection, migration_0007)
                    old_id = connection.exec_driver_sql("""
                        INSERT INTO execution_reconciliations
                            (action_execution_id, approval_id, outcome, explanation, reference,
                             actor, correlation_id, synthetic, created_at)
                        VALUES (1, 1, 'still_unknown', 'Historical operator uncertainty.', NULL,
                                'legacy-operator', 'approval:1', FALSE, now())
                        RETURNING id
                    """).scalar_one()
                    apply(connection, migration_0008)
                    row = connection.execute(text("""
                        SELECT a.status, a.execution_status, a.actor, a.attempts,
                               e.state, e.attempts,
                               (SELECT count(*) FROM execution_reconciliations)
                        FROM approvals a CROSS JOIN action_executions e
                        WHERE a.id = 1 AND e.idempotency_key = 'approval:1'
                    """)).one()
                    assert row == ("approved", "ambiguous", "legacy-operator", 1,
                                   "ambiguous", 1, 1), row
                    historical = connection.exec_driver_sql("""
                        SELECT revision, outcome, previous_reconciliation_id
                        FROM execution_reconciliations WHERE id = %s
                    """, (old_id,)).one()
                    assert historical == (1, "still_unknown", None), historical
                    connection.exec_driver_sql("""
                        INSERT INTO execution_reconciliations
                            (action_execution_id, approval_id, revision, previous_reconciliation_id,
                             outcome, explanation, reference, actor, correlation_id, synthetic, created_at)
                        VALUES (1, 1, 2, %s, 'confirmed_not_executed',
                                'Later evidence confirms non-execution.', 'SYNTH-VERIFY',
                                'operator-2', 'approval:1', FALSE, now())
                    """, (old_id,))
                    result = connection.exec_driver_sql("""
                        SELECT count(*), max(revision), min(actor), max(outcome)
                        FROM execution_reconciliations WHERE approval_id = 1
                    """).one()
                    assert result == (2, 2, "legacy-operator", "still_unknown"), result
        print("PASS: migration 0008 applies to a fresh schema and upgrades populated 0007 data, preserving historical ambiguous decisions and allowing an append-only follow-up.")
    finally:
        for isolated in isolated_engines:
            isolated.dispose()
        with engine.begin() as connection:
            for schema in schemas:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


if __name__ == "__main__":
    run()
