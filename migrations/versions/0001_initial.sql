CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    shop_domain VARCHAR(255) NOT NULL,
    external_id VARCHAR(100) NOT NULL,
    order_number VARCHAR(100) NOT NULL,
    email VARCHAR(320),
    currency VARCHAR(8) NOT NULL,
    total VARCHAR(32) NOT NULL,
    fulfillment_status VARCHAR(40) NOT NULL,
    financial_status VARCHAR(40) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSON NOT NULL,
    CONSTRAINT uq_order_external UNIQUE (shop_domain, external_id)
);
CREATE INDEX IF NOT EXISTS ix_orders_external_id ON orders (external_id);
CREATE INDEX IF NOT EXISTS ix_orders_order_number ON orders (order_number);

CREATE TABLE IF NOT EXISTS deliveries (
    id SERIAL PRIMARY KEY,
    webhook_id VARCHAR(150) NOT NULL,
    event_id VARCHAR(150),
    topic VARCHAR(80) NOT NULL,
    shop_domain VARCHAR(255) NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(30) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    payload JSON NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_deliveries_webhook_id ON deliveries (webhook_id);

CREATE TABLE IF NOT EXISTS approvals (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(40) NOT NULL,
    subject_type VARCHAR(40) NOT NULL,
    subject_id INTEGER NOT NULL,
    proposed_action VARCHAR(80) NOT NULL,
    supporting_facts JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    actor VARCHAR(120),
    decision_at TIMESTAMP WITH TIME ZONE,
    execution_status VARCHAR(30) NOT NULL,
    attempts INTEGER NOT NULL,
    failure_detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT uq_approval_action UNIQUE (kind, subject_type, subject_id, proposed_action)
);
CREATE INDEX IF NOT EXISTS ix_approval_queue ON approvals (status, execution_status);

CREATE TABLE IF NOT EXISTS audit_events (
    id SERIAL PRIMARY KEY,
    correlation_id VARCHAR(64) NOT NULL,
    entity_type VARCHAR(40) NOT NULL,
    entity_id INTEGER NOT NULL,
    event VARCHAR(80) NOT NULL,
    actor VARCHAR(120) NOT NULL,
    details JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_events_correlation_id ON audit_events (correlation_id);

CREATE TABLE IF NOT EXISTS action_executions (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(160) NOT NULL UNIQUE,
    state VARCHAR(30) NOT NULL,
    attempts INTEGER NOT NULL,
    detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE IF NOT EXISTS support_cases (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders (id),
    customer_email VARCHAR(320),
    message TEXT NOT NULL,
    category VARCHAR(40) NOT NULL,
    confidence INTEGER NOT NULL,
    draft TEXT,
    missing_info TEXT,
    status VARCHAR(30) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);
