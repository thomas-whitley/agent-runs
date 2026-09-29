-- The repo a repo_chore works on, as owner/name. NULL for every other type.
-- The instruction stays on runs.task, like every other type's input.
ALTER TABLE runs ADD COLUMN repo text;
