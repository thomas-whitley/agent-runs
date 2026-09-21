import psycopg

from app.migrations import apply_migrations

CORE_TABLES = {"runs", "steps", "events", "chunks"}


def table_names(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {row[0] for row in rows}


def test_migrations_create_the_core_tables(clean_db):
    apply_migrations(clean_db)

    assert CORE_TABLES <= table_names(clean_db)


def test_applying_migrations_twice_is_a_no_op(clean_db):
    applied_first = apply_migrations(clean_db)
    applied_again = apply_migrations(clean_db)

    assert applied_first, "the first run should apply at least one migration"
    assert applied_again == []
    assert CORE_TABLES <= table_names(clean_db)
