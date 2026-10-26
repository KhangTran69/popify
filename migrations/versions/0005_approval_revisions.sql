ALTER TABLE approvals ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN replacement_approval_id INTEGER;
ALTER TABLE approvals DROP CONSTRAINT uq_approval_action;
ALTER TABLE approvals ADD CONSTRAINT uq_approval_revision UNIQUE (kind, subject_type, subject_id, proposed_action, revision);
ALTER TABLE approvals ADD CONSTRAINT fk_approval_replacement FOREIGN KEY (replacement_approval_id) REFERENCES approvals (id);
