-- Which kind of site_check a run is: uptime, lighthouse or broken_links.
-- The claim endpoints hand a self hosted worker only the kinds it declared,
-- and never uptime, which the scheduler Job runs and closes itself. NULL for
-- every type that is not a check.
ALTER TABLE runs ADD COLUMN check_kind text;
