CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task         text NOT NULL,
    status       text NOT NULL DEFAULT 'pending',
    claimed_by   text,
    token_budget integer NOT NULL DEFAULT 50000,
    tokens_used  integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE TABLE steps (
    run_id      uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    kind        text NOT NULL,
    input       jsonb,
    output      jsonb,
    tokens      integer NOT NULL DEFAULT 0,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (run_id, seq)
);

-- events.id is what the SSE "id:" field carries, so it must be monotonic
-- across the whole table, not per run. Last-Event-ID is compared against it.
CREATE TABLE events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     uuid NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq        integer NOT NULL,
    payload    jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

CREATE INDEX events_run_id_id_idx ON events (run_id, id);

CREATE TABLE chunks (
    id        serial PRIMARY KEY,
    source    text NOT NULL,
    body      text NOT NULL,
    embedding vector(1536)
);
