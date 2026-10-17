"""
Live-Postgres integration test for full-table preview pushdown and the
SQL-side diff. Skipped unless STYCH_TEST_POSTGRES_URL is set, e.g.:

    STYCH_TEST_POSTGRES_URL=postgresql+psycopg2://user:pass@localhost:5432/testdb \
        pytest tests/integration/test_preview_pushdown_pg.py
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

PG_URL = os.getenv("STYCH_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="STYCH_TEST_POSTGRES_URL not set — live Postgres required"
)


@pytest.fixture()
def pg_table():
    """A 25K-row table plus a mutated stych_temp working copy."""
    suffix = uuid.uuid4().hex[:8]
    table = f"itest_orders_{suffix}"
    temp_table = f"itest_work_{suffix}"
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                f'CREATE TABLE public."{table}" (id INT PRIMARY KEY, '
                f"amount DOUBLE PRECISION, city TEXT)"
            )
        )
        conn.execute(
            text(
                f'INSERT INTO public."{table}" '
                "SELECT g, CASE WHEN g % 10 = 0 THEN NULL ELSE g * 1.5 END, "
                "CASE WHEN g % 3 = 0 THEN 'paris' ELSE 'lyon' END "
                "FROM generate_series(1, 25000) g"
            )
        )
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS "stych_temp"'))
        conn.execute(
            text(f'CREATE TABLE "stych_temp"."{temp_table}" AS ' f'SELECT * FROM public."{table}"')
        )
        # Mutations: impute NULL amounts, delete some rows, add a column.
        conn.execute(
            text(f'UPDATE "stych_temp"."{temp_table}" SET amount = 0 WHERE amount IS NULL')
        )
        conn.execute(text(f'DELETE FROM "stych_temp"."{temp_table}" WHERE id > 24990'))
        conn.execute(text(f'ALTER TABLE "stych_temp"."{temp_table}" ADD COLUMN flag INT DEFAULT 1'))
    yield table, temp_table
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))
        conn.execute(text(f'DROP TABLE IF EXISTS "stych_temp"."{temp_table}"'))
    engine.dispose()


def test_pushdown_page_filter_sort_count(pg_table):
    from datasources.adapters.postgres import PostgresTableAdapter

    table, _ = pg_table
    adapter = PostgresTableAdapter(PG_URL, schema="public", table=table)

    assert [c.name for c in adapter.schema()] == ["id", "amount", "city"]
    assert adapter.count().value == 25000

    # Page far beyond the old 10K sample boundary.
    rows = adapter.fetch_page(offset=20000, limit=5, sort_by="id", sort_dir="asc")
    assert [r[0] for r in rows] == [20001, 20002, 20003, 20004, 20005]

    filtered = adapter.count({"city": "par"}, None)
    assert filtered.value == 25000 // 3  # every third row is 'paris'

    top = adapter.fetch_page(
        offset=0, limit=1, sort_by="amount", sort_dir="desc", filters={"city": "lyon"}
    )
    assert top[0][2] == "lyon"

    stats = adapter.stats()
    assert stats["amount"]["nulls"] == 2500


def test_sql_diff_summary_and_page(pg_table):
    from api import sql_diff_service
    from datasources.adapters.postgres import PostgresTableAdapter

    table, temp_table = pg_table
    base = PostgresTableAdapter(PG_URL, schema="public", table=table)
    temp = PostgresTableAdapter(PG_URL, schema="stych_temp", table=temp_table)

    sql_diff_service.clear_spec_cache()
    page = sql_diff_service.build_sql_diff_page(
        base, temp, session_id="itest", page=1, page_size=50
    )
    s = page["summary"]
    assert s["alignment"] == "key:id"
    assert s["rows_removed"] == 10
    assert (
        s["rows_changed"] == 2499
    )  # NULL->0 imputations still present (id 24990 kept? ids %10==0 up to 24990)
    assert s["columns_added"] == ["flag"]
    assert page["total_rows"] == 25000

    # Imputed narrowing returns only NULL->value rows with imputed cell meta.
    imputed = sql_diff_service.build_sql_diff_page(
        base, temp, session_id="itest", page=1, page_size=10, change_kind="imputed"
    )
    assert imputed["filtered_rows"] == 2499
    row_meta = imputed["cell_meta"][0]
    assert any(m and m["k"] == "imputed" for m in row_meta)

    # Ghost rows appear when filtering to removed.
    removed = sql_diff_service.build_sql_diff_page(
        base, temp, session_id="itest", page=1, page_size=10, change_kind="removed"
    )
    assert removed["filtered_rows"] == 10
    assert all(m and m["k"] == "removed" for m in removed["cell_meta"][0])
