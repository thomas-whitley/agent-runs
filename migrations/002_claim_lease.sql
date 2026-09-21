-- A worker killed outright cannot release its claim, so the claim carries a
-- lease instead. The worker refreshes heartbeat_at as it works, and a run whose
-- heartbeat has gone stale can be claimed by another worker.
ALTER TABLE runs ADD COLUMN heartbeat_at timestamptz;

CREATE INDEX runs_claimable_idx ON runs (status, heartbeat_at);
