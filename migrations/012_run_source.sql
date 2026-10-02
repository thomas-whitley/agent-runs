-- Where a run came from, set when it is created. A repo chore waits for the
-- same Approve button whatever its source; this column only says who asked.
-- Rows from before this migration read as api.
ALTER TABLE runs ADD COLUMN source text NOT NULL DEFAULT 'api'
    CHECK (source IN ('telegram', 'mcp', 'n8n', 'api', 'scheduler'));
