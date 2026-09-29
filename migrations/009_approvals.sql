-- A question the bot asked with inline buttons, per docs/mercury.md. The
-- callback carries the row id. One press answers it; nobody pressing for 24
-- hours expires it, and a run waiting on it ends cancelled.
CREATE TABLE approvals (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action        text NOT NULL CHECK (action IN ('start_run', 'resume_schedule')),
    run_id        uuid REFERENCES runs (id) ON DELETE CASCADE,
    schedule_name text,
    chat_id       bigint NOT NULL,
    message_id    bigint,
    text          text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    answer        text CHECK (answer IN ('approved', 'declined', 'expired')),
    answered_at   timestamptz,
    CHECK ((action = 'start_run') = (run_id IS NOT NULL)),
    CHECK ((action = 'resume_schedule') = (schedule_name IS NOT NULL))
);
CREATE INDEX approvals_open ON approvals (expires_at) WHERE answer IS NULL;
