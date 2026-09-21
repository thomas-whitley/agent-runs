"""Retrieval over the corpus.

Two paths, one interface. With an embedding key the chunks carry vectors and
the search is cosine distance in pgvector. Without one the search is Postgres
full text search over the same rows, and the README says retrieval, not vector.
"""

from dataclasses import dataclass
from typing import Protocol

import psycopg

DEFAULT_LIMIT = 3
EMBEDDING_DIMENSIONS = 1536


@dataclass(frozen=True)
class CorpusChunk:
    source: str
    ord: int
    body: str


@dataclass(frozen=True)
class Chunk:
    id: int
    source: str
    body: str


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Retriever(Protocol):
    def search(self, conn: psycopg.Connection, query: str, limit: int) -> list[Chunk]: ...


_INSERT_CHUNK = """
INSERT INTO chunks (source, ord, body, embedding)
VALUES (%s, %s, %s, %s)
ON CONFLICT (source, ord) DO NOTHING
"""

# plainto_tsquery joins every term with AND, which means a chunk only matches
# when it contains all of them. Retrieval wants any term, ranked. Rewriting the
# operators of the query plainto_tsquery produced keeps the input sanitised.
_TEXT_SEARCH = """
WITH q AS (
    SELECT nullif(
        replace(plainto_tsquery('english', %s)::text, ' & ', ' | '), ''
    )::tsquery AS query
)
SELECT chunks.id, chunks.source, chunks.body
FROM chunks, q
WHERE q.query IS NOT NULL AND chunks.body_tsv @@ q.query
ORDER BY ts_rank(chunks.body_tsv, q.query) DESC, chunks.id
LIMIT %s
"""

_VECTOR_SEARCH = """
SELECT id, source, body
FROM chunks
WHERE embedding IS NOT NULL
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def index_corpus(
    conn: psycopg.Connection,
    chunks: list[CorpusChunk],
    embedder: Embedder | None = None,
) -> int:
    """Store any chunk not already present. Returns how many were added."""
    if not chunks:
        return 0

    embeddings: list[list[float] | None]
    if embedder is None:
        embeddings = [None] * len(chunks)
    else:
        embeddings = list(embedder.embed([chunk.body for chunk in chunks]))

    added = 0
    for chunk, embedding in zip(chunks, embeddings, strict=True):
        vector = str(list(embedding)) if embedding is not None else None
        cursor = conn.execute(_INSERT_CHUNK, (chunk.source, chunk.ord, chunk.body, vector))
        added += cursor.rowcount
    return added


class TextRetriever:
    """Postgres full text search. Needs no key and no embeddings."""

    name = "postgres full text search"
    embedder: Embedder | None = None

    def search(
        self, conn: psycopg.Connection, query: str, limit: int = DEFAULT_LIMIT
    ) -> list[Chunk]:
        rows = conn.execute(_TEXT_SEARCH, (query, limit)).fetchall()
        return [Chunk(id=row[0], source=row[1], body=row[2]) for row in rows]


class VectorRetriever:
    """Cosine distance in pgvector over the stored embeddings."""

    name = "pgvector cosine distance"

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self.embedder = embedder

    def search(
        self, conn: psycopg.Connection, query: str, limit: int = DEFAULT_LIMIT
    ) -> list[Chunk]:
        embedding = self._embedder.embed([query])[0]
        rows = conn.execute(_VECTOR_SEARCH, (str(list(embedding)), limit)).fetchall()
        return [Chunk(id=row[0], source=row[1], body=row[2]) for row in rows]


class VoyageEmbedder:
    """Voyage AI embeddings over plain HTTP, so this needs no extra dependency."""

    ENDPOINT = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str = "voyage-3", timeout_seconds: float = 30.0):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        import json
        import urllib.request

        payload = json.dumps({"input": texts, "model": self._model}).encode()
        request = urllib.request.Request(
            self.ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = json.load(response)
        return [item["embedding"] for item in body["data"]]


def build_retriever(settings) -> Retriever:
    """Vectors when there is an embedding key, full text search when there is not."""
    if settings.voyage_api_key:
        return VectorRetriever(
            VoyageEmbedder(settings.voyage_api_key, model=settings.embedding_model)
        )
    return TextRetriever()
