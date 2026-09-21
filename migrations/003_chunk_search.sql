-- Chunks are addressed by (source, ord) so re-indexing the corpus is a no-op
-- rather than a duplicate.
ALTER TABLE chunks ADD COLUMN ord integer NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD CONSTRAINT chunks_source_ord_key UNIQUE (source, ord);

-- Full text search is the retrieval path when there is no embedding key. The
-- column is generated, so it cannot drift from the body it indexes.
ALTER TABLE chunks
    ADD COLUMN body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED;

CREATE INDEX chunks_body_tsv_idx ON chunks USING gin (body_tsv);
