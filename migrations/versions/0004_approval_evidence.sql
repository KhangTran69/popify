ALTER TABLE approvals ADD COLUMN evidence_hash VARCHAR(64);
ALTER TABLE action_executions ADD COLUMN proposed_action VARCHAR(80);
ALTER TABLE action_executions ADD COLUMN evidence_hash VARCHAR(64);
