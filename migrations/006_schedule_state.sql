-- Tracks consecutive failures per named schedule entry (one row per site
-- under site_uptime, for example), so a flaky check suspends on its own
-- without silencing every other schedule entry.
CREATE TABLE schedule_state (
    name                 text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0,
    suspended            boolean NOT NULL DEFAULT false,
    suspended_at         timestamptz
);
