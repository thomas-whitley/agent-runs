"""The corpus the retrieval step searches.

The repo's own docs plus a bundled set of Python standard library notes, so a
retrieved chunk can change what the agent writes for the pytest task.
"""

from pathlib import Path

from app.retrieval import CorpusChunk

MAX_CHUNK_CHARACTERS = 800

_ROOT = Path(__file__).resolve().parent.parent

# Reference material only. The repo's own README and design docs were in here
# first and crowded out the standard library notes, because they are full of the
# same words a pytest file uses (test, assert, pattern) while saying nothing
# about how to write Python. The spec asks for a corpus that changes the agent's
# output on the pytest task, and they made it worse.
_SOURCES = ("corpus",)
_SINGLE_FILES: tuple[str, ...] = ()


def _split_into_chunks(text: str) -> list[str]:
    """One chunk per paragraph, with long paragraphs cut to a bounded size."""
    chunks: list[str] = []
    for paragraph in text.split("\n\n"):
        body = paragraph.strip()
        if not body or body.startswith("#"):
            continue
        while len(body) > MAX_CHUNK_CHARACTERS:
            chunks.append(body[:MAX_CHUNK_CHARACTERS])
            body = body[MAX_CHUNK_CHARACTERS:]
        if body:
            chunks.append(body)
    return chunks


def _markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in _SOURCES:
        path = root / directory
        if path.is_dir():
            files.extend(sorted(path.glob("*.md")))
    for name in _SINGLE_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
    return files


def load_corpus(root: Path | None = None) -> list[CorpusChunk]:
    """Every chunk of the corpus, keyed by file stem and position within it."""
    base = root or _ROOT
    chunks: list[CorpusChunk] = []

    for path in _markdown_files(base):
        source = path.stem
        for ordinal, body in enumerate(_split_into_chunks(path.read_text())):
            chunks.append(CorpusChunk(source=source, ord=ordinal, body=body))

    return chunks
