from app.corpus import MAX_CHUNK_CHARACTERS, load_corpus


def test_the_corpus_loads_chunks():
    chunks = load_corpus()

    assert len(chunks) >= 20, "the corpus should be a few dozen chunks"


def test_every_chunk_has_a_source_and_a_body():
    for chunk in load_corpus():
        assert chunk.source
        assert chunk.body.strip()


def test_chunks_are_bounded_so_a_prompt_stays_small():
    for chunk in load_corpus():
        assert len(chunk.body) <= MAX_CHUNK_CHARACTERS, chunk.source


def test_source_and_ord_pairs_are_unique():
    keys = [(chunk.source, chunk.ord) for chunk in load_corpus()]

    assert len(keys) == len(set(keys))


def test_the_corpus_covers_the_standard_library():
    sources = {chunk.source for chunk in load_corpus()}

    assert any(source.startswith("python_stdlib") for source in sources)
