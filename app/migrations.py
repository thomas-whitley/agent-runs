"""Plain SQL migrations, applied in filename order at startup."""

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def apply_migrations(url: str, migrations_dir: Path | None = None) -> list[str]:
    """Apply every migration not yet recorded. Returns the names applied."""
    directory = migrations_dir or MIGRATIONS_DIR
    applied: list[str] = []

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(_TRACKING_TABLE)
        rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
        already = {row[0] for row in rows}

        for path in sorted(directory.glob("*.sql")):
            if path.name in already:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)

    return applied
