ALTER TABLE execution_reconciliations
    DROP CONSTRAINT uq_reconciliation_execution,
    DROP CONSTRAINT uq_reconciliation_approval,
    DROP CONSTRAINT ck_reconciliation_outcome,
    DROP CONSTRAINT ck_reconciliation_reference;

ALTER TABLE execution_reconciliations
    ADD COLUMN revision INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN previous_reconciliation_id INTEGER REFERENCES execution_reconciliations (id) ON DELETE RESTRICT,
    ADD COLUMN asserted_outcome VARCHAR(32);

ALTER TABLE execution_reconciliations
    ADD CONSTRAINT ck_reconciliation_revision CHECK (revision > 0),
    ADD CONSTRAINT ck_reconciliation_outcome CHECK (
        outcome IN ('confirmed_completed', 'confirmed_not_executed', 'still_unknown', 'conflicting_evidence')
    ),
    ADD CONSTRAINT ck_reconciliation_reference CHECK (
        outcome IN ('still_unknown', 'conflicting_evidence') OR reference IS NOT NULL
    ),
    ADD CONSTRAINT ck_reconciliation_asserted_outcome CHECK (
        (outcome = 'conflicting_evidence' AND asserted_outcome IN ('confirmed_completed', 'confirmed_not_executed', 'still_unknown'))
        OR (outcome != 'conflicting_evidence' AND asserted_outcome IS NULL)
    ),
    ADD CONSTRAINT uq_reconciliation_execution_revision UNIQUE (action_execution_id, revision),
    ADD CONSTRAINT uq_reconciliation_previous UNIQUE (previous_reconciliation_id);

CREATE INDEX ix_reconciliation_execution_revision
    ON execution_reconciliations (action_execution_id, revision DESC);
CREATE INDEX ix_reconciliation_approval_revision
    ON execution_reconciliations (approval_id, revision DESC);
