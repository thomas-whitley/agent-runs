-- "Open it anyway": a failed repo chore offers a button, and pressing it
-- creates a run that reapplies the failed run's diff and opens the pull
-- request regardless. source_run_id names the run whose diff it carries.
ALTER TABLE runs ADD COLUMN source_run_id uuid REFERENCES runs (id);

ALTER TABLE approvals DROP CONSTRAINT approvals_action_check;
ALTER TABLE approvals DROP CONSTRAINT approvals_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_action_check
    CHECK (action IN ('start_run', 'resume_schedule', 'open_anyway'));
ALTER TABLE approvals ADD CONSTRAINT approvals_run_check
    CHECK ((action IN ('start_run', 'open_anyway')) = (run_id IS NOT NULL));
