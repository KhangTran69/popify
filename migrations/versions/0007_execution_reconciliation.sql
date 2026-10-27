CREATE TABLE execution_reconciliations (
    id SERIAL PRIMARY KEY,
    action_execution_id INTEGER NOT NULL REFERENCES action_executions (id) ON DELETE RESTRICT,
    approval_id INTEGER NOT NULL REFERENCES approvals (id) ON DELETE RESTRICT,
    outcome VARCHAR(32) NOT NULL,
    explanation TEXT NOT NULL,
    reference VARCHAR(240),
    actor VARCHAR(120) NOT NULL,
    correlation_id VARCHAR(150) NOT NULL,
    synthetic BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT ck_reconciliation_outcome CHECK (
        outcome IN ('confirmed_completed', 'confirmed_not_executed', 'still_unknown')
    ),
    CONSTRAINT ck_reconciliation_reference CHECK (
        outcome = 'still_unknown' OR reference IS NOT NULL
    ),
    CONSTRAINT uq_reconciliation_execution UNIQUE (action_execution_id),
    CONSTRAINT uq_reconciliation_approval UNIQUE (approval_id)
);
CREATE INDEX ix_execution_reconciliations_correlation_id ON execution_reconciliations (correlation_id);
