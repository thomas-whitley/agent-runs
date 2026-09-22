-- The API's root span context, so the worker's step spans land in the same
-- trace instead of starting a second, unrelated one. NULL means tracing was
-- off, or the run predates this column.
ALTER TABLE runs ADD COLUMN trace_context text;
