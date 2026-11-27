CREATE TABLE operator_accounts (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    role VARCHAR(16) NOT NULL CHECK (role IN ('admin','support','reviewer','auditor')),
    password_hash VARCHAR(160) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE operator_sessions (
    token_hash VARCHAR(64) PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES operator_accounts(id) ON DELETE CASCADE,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_operator_sessions_account_id ON operator_sessions(account_id);
CREATE INDEX ix_operator_sessions_expires_at ON operator_sessions(expires_at);
CREATE TABLE login_buckets (
    key VARCHAR(64) PRIMARY KEY,
    failures INTEGER NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX ix_login_buckets_expires_at ON login_buckets(expires_at);
CREATE INDEX ix_audit_workflow_recent ON audit_events(entity_type, entity_id, event, id);
