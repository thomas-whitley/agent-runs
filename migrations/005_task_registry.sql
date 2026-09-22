-- A run has a type, chosen from the registry in app/tasks.py. Existing rows
-- predate task types entirely, so they default to the one type that existed
-- before this column: pytest.
ALTER TABLE runs ADD COLUMN type text NOT NULL DEFAULT 'pytest';

-- The type's registered provider, resolved once at creation. The worker
-- reads this instead of MODEL. Nullable because site_check makes no model
-- call.
ALTER TABLE runs ADD COLUMN provider text;

-- Which executor ran a check: cloud or the self hosted worker. Written only
-- by checks/, arriving in step 2; unused until then.
ALTER TABLE runs ADD COLUMN executor text;

-- GET /runs pages newest first with keyset pagination on (created_at, id),
-- which this index turns into a scan instead of a sort on every page.
CREATE INDEX runs_created_at_id_idx ON runs (created_at DESC, id DESC);
