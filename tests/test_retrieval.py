"""Retrieval over a fixed corpus, by full text search and by vector distance."""

import hashlib
import math

import pytest

from app.migrations import apply_migrations
from app.retrieval import CorpusChunk, TextRetriever, VectorRetriever, index_corpus

CORPUS = [
    CorpusChunk(
        source="stdlib/re",
        ord=0,
        body="The re module provides regular expression matching. re.findall returns "
        "every non overlapping match of a pattern in a string as a list.",
    ),
    CorpusChunk(
        source="stdlib/itertools",
        ord=0,
        body="The itertools module provides iterator building blocks. itertools.chain "
        "links several iterables end to end into one sequence.",
    ),
    CorpusChunk(
        source="stdlib/datetime",
        ord=0,
        body="The datetime module supplies classes for manipulating dates and times. "
        "datetime.timedelta represents a duration between two moments.",
    ),
]

EMBEDDING_DIMENSIONS = 1536


class BagOfWordsEmbedder:
    """Deterministic and offline. Similar wording lands in the same dimensions."""

    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in text.lower().split():
            word = word.strip(".,()")
            if not word:
                continue
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
        length = math.sqrt(sum(value * value for value in vector))
        return [value / length for value in vector] if length else vector


@pytest.fixture
def corpus_db(clean_db):
    apply_migrations(clean_db)
    import psycopg

    with psycopg.connect(clean_db, autocommit=True) as conn:
        yield conn


def test_indexing_the_corpus_stores_every_chunk(corpus_db):
    added = index_corpus(corpus_db, CORPUS)

    assert added == 3
    count = corpus_db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert count == 3


def test_indexing_twice_adds_nothing(corpus_db):
    index_corpus(corpus_db, CORPUS)

    assert index_corpus(corpus_db, CORPUS) == 0
    assert corpus_db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3


def test_full_text_search_returns_the_chunk_that_matches(corpus_db):
    index_corpus(corpus_db, CORPUS)

    results = TextRetriever().search(corpus_db, "regular expression matching", limit=1)

    assert [chunk.source for chunk in results] == ["stdlib/re"]


def test_full_text_search_ranks_the_best_chunk_first(corpus_db):
    index_corpus(corpus_db, CORPUS)

    results = TextRetriever().search(corpus_db, "duration between two dates", limit=3)

    assert results[0].source == "stdlib/datetime"


def test_full_text_search_returns_nothing_for_an_unrelated_query(corpus_db):
    index_corpus(corpus_db, CORPUS)

    assert TextRetriever().search(corpus_db, "zzzzqqqq", limit=3) == []


def test_vector_search_returns_the_nearest_chunk(corpus_db):
    embedder = BagOfWordsEmbedder()
    index_corpus(corpus_db, CORPUS, embedder=embedder)

    results = VectorRetriever(embedder).search(corpus_db, "regular expression matching", limit=1)

    assert [chunk.source for chunk in results] == ["stdlib/re"]


def test_vector_search_stores_an_embedding_for_every_chunk(corpus_db):
    index_corpus(corpus_db, CORPUS, embedder=BagOfWordsEmbedder())

    missing = corpus_db.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    assert missing == 0


def test_text_search_needs_no_embeddings(corpus_db):
    index_corpus(corpus_db, CORPUS)

    missing = corpus_db.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
    assert missing == 3
    assert TextRetriever().search(corpus_db, "iterator building blocks", limit=1)


def settings_for_retrieval(voyage_api_key=None):
    from app.config import Settings

    return Settings(
        database_url="unused",
        keepalive_seconds=15.0,
        model="stub",
        token_budget=50_000,
        max_runs_per_day=20,
        worker_id="worker-test",
        poll_seconds=0.05,
        verify_timeout_seconds=10.0,
        lease_seconds=60.0,
        model_timeout_seconds=25.0,
        replica_id="replica-test",
        voyage_api_key=voyage_api_key,
        mercury_bearer_token=None,
        api_base_url="http://localhost:8000",
        mercury_config_path="/config/mercury.yaml",
        embedding_model="voyage-3",
    )


def test_without_an_embedding_key_retrieval_is_full_text_search():
    from app.retrieval import build_retriever

    retriever = build_retriever(settings_for_retrieval())

    assert isinstance(retriever, TextRetriever)
    assert retriever.name == "postgres full text search"


def test_with_an_embedding_key_retrieval_is_vector_distance():
    from app.retrieval import build_retriever

    retriever = build_retriever(settings_for_retrieval(voyage_api_key="not-a-real-key"))

    assert isinstance(retriever, VectorRetriever)
    assert retriever.name == "pgvector cosine distance"
