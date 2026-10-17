"""
Live-Postgres integration test for SQL filter pushdown: predicate WHERE
compilation, inline duplicate_values joins, fuzzy annotation materialization
into stych_temp, and group-adjacent ordering. Skipped unless
STYCH_TEST_POSTGRES_URL is set.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from filters import annotate_sql, sql_service
from filters.spec import parse_spec

PG_URL = os.getenv("STYCH_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="STYCH_TEST_POSTGRES_URL not set — live Postgres required"
)


@pytest.fixture()
def pg_table():
    suffix = uuid.uuid4().hex[:8]
    table = f"itest_filters_{suffix}"
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                f'CREATE TABLE public."{table}" (id INT PRIMARY KEY, '
                f"balance DOUBLE PRECISION, vendor TEXT)"
            )
        )
        conn.execute(
            text(
                f'INSERT INTO public."{table}" VALUES '
                "(1, -5.0, 'Acme Corporation'), (2, 10.0, 'Acme Corporatio'), "
                "(3, -1.0, 'Zed Ltd'), (4, 3.0, 'Zed Ltd'), (5, 7.0, 'Solo Inc'), "
                "(6, NULL, 'Acme Corporation')"
            )
        )
    yield table
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))
        conn.execute(text('DROP SCHEMA IF EXISTS "stych_temp" CASCADE'))
    engine.dispose()


@pytest.fixture()
def adapter(pg_table):
    from datasources.adapters.base import clear_caches
    from datasources.adapters.postgres import PostgresTableAdapter

    clear_caches()
    sql_service.clear_caches()
    annotate_sql.clear_registry()
    yield PostgresTableAdapter(PG_URL, schema="public", table=pg_table)
    clear_caches()
    sql_service.clear_caches()
    annotate_sql.clear_registry()


def test_predicate_pushdown_counts(adapter):
    spec = parse_spec(
        {
            "name": "neg",
            "select": {"op": "lt", "column": "balance", "value": 0},
            "present": {"rules": [{"id": "r", "severity": "danger"}]},
        }
    )
    page = sql_service.build_sql_filter_page(adapter, spec, session_id="it1", matches_only=True)
    assert page["summary"]["matched_rows"] == 2  # NULL balance excluded
    assert page["filtered_rows"] == 2
    assert sorted(r[0] for r in page["rows"]) == [1, 3]
    assert page["summary"]["identity_resolved"] == "key:id"


def test_duplicate_values_inline_join(adapter):
    spec = parse_spec(
        {
            "name": "dups",
            "annotate": [{"kind": "duplicate_values", "id": "d", "columns": ["vendor"]}],
            "select": {"op": "not_null", "column": "d_group"},
            "present": {"group_by": "d_group", "matches_only": True},
        }
    )
    page = sql_service.build_sql_filter_page(adapter, spec, session_id="it2")
    # 'Acme Corporation' ×2 and 'Zed Ltd' ×2 group; grouped rows come adjacent.
    assert page["summary"]["group_count"] == 2
    bands = [m["g"] for m in page["row_meta"]]
    assert bands == sorted(bands)


def test_fuzzy_annotation_materializes_into_stych_temp(adapter, pg_table):
    spec = parse_spec(
        {
            "name": "fuzzy",
            "annotate": [
                {
                    "kind": "fuzzy_duplicates",
                    "id": "dup",
                    "on": "vendor",
                    "strategy": "token_sort",
                    "threshold": 0.9,
                }
            ],
            "select": {"op": "not_null", "column": "dup_group"},
            "present": {"group_by": "dup_group", "matches_only": True},
        }
    )
    page = sql_service.build_sql_filter_page(adapter, spec, session_id="it3")
    assert page["summary"]["annotations"][0]["status"] == "ok"
    # Acme trio (2 exact + 1 fuzzy at 0.968) + Zed pair
    assert page["summary"]["group_count"] == 2
    assert page["summary"]["matched_rows"] == 5

    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'stych_temp' AND table_name LIKE 'flt_%'"
                )
            )
        ]
    engine.dispose()
    assert tables, "fuzzy annotation table was not materialized in stych_temp"
