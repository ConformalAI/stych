"""
Unit tests for the pushdown table adapters: pure SQL/filter-doc builders,
LIKE escaping, identifier quoting, the count/schema caches, and adapter
resolution from DataContext metadata. No database required.
"""

import re
from types import SimpleNamespace

import pandas as pd
import pytest

from datasources.adapters import (
    AdapterError,
    AdapterUnavailable,
    CountResult,
    TempTableUnavailable,
    cached_count,
    clear_caches,
    resolve_adapter,
    supports_pushdown,
)
from datasources.adapters import base as adapter_base
from datasources.adapters import mongodb as mongo_a
from datasources.adapters import postgres as pg_a
from datasources.adapters import sqlserver as ms_a
from datasources.adapters.base import escape_like, sql_type_kind


def _ctx(meta, df=None):
    return SimpleNamespace(metadata=meta, working_dataframe=df)


# ------------------------------------------------------------------- escaping


def test_escape_like_metacharacters():
    assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"
    assert escape_like("a[b]", extra="[") == "a\\[b]"


def test_sql_type_kind_mapping():
    assert sql_type_kind("integer") == "number"
    assert sql_type_kind("double precision") == "number"
    assert sql_type_kind("boolean") == "bool"
    assert sql_type_kind("bit") == "bool"
    assert sql_type_kind("timestamp with time zone") == "date"
    assert sql_type_kind("character varying") == "string"
    assert sql_type_kind("uuid") == "string"
    assert sql_type_kind("bytea") == "other"


# ------------------------------------------------------------------- postgres


def test_pg_select_quoting_and_pushdown():
    sql, params = pg_a.build_select_sql(
        '"public"."orders"',
        ["id", 'we"ird'],
        offset=200,
        limit=100,
        sort_by='we"ird',
        sort_dir="desc",
        filters={"id": "50%"},
        search="foo",
        stable_tiebreak="ctid",
    )
    assert '"we""ird"' in sql  # quote-doubled, never mangled
    assert 'ORDER BY "we""ird" DESC NULLS LAST, ctid' in sql
    assert "LIMIT :limit OFFSET :offset" in sql
    assert params["limit"] == 100 and params["offset"] == 200
    assert params["f0"] == "%50\\%%"  # LIKE-escaped needle, bound not inlined
    assert "ILIKE :f0 ESCAPE '\\'" in sql
    assert ":srch" in sql and params["srch"] == "%foo%"


def test_pg_unknown_sort_and_filter_columns_dropped():
    sql, params = pg_a.build_select_sql(
        '"s"."t"',
        ["a"],
        offset=0,
        limit=10,
        sort_by="nope",
        filters={"ghost": "x"},
    )
    assert "ORDER BY" not in sql
    assert "WHERE" not in sql
    assert set(params) == {"limit", "offset"}


def test_pg_count_sql():
    sql, params = pg_a.build_count_sql('"s"."t"', ["a", "b"], filters={"a": "x"})
    assert sql.startswith("SELECT COUNT(*) FROM")
    assert params["f0"] == "%x%"


def test_pg_custom_query_wrapping_and_validation():
    ref = pg_a.build_source_ref(None, None, "SELECT a FROM t")
    assert ref == "(SELECT a FROM t) AS _src"
    ref = pg_a.build_source_ref(None, None, "WITH c AS (SELECT 1) SELECT * FROM c;")
    assert ref.endswith(") AS _src") and ";" not in ref
    with pytest.raises(AdapterError):
        pg_a.build_source_ref(None, None, "SELECT a; DROP TABLE t")
    with pytest.raises(AdapterError):
        pg_a.build_source_ref(None, None, "DELETE FROM t")
    with pytest.raises(AdapterError):
        pg_a.build_source_ref("public", None, None)  # no table, no query


def test_pg_stats_sql_positional_aliases():
    cols = [
        pg_a.AdapterColumn("amt", "numeric", "number"),
        pg_a.AdapterColumn('we"ird', "text", "string"),
    ]
    sql = pg_a.build_stats_sql('"s"."t"', cols)
    assert "AS n0" in sql and "AS u0" in sql and "AS av0" in sql
    assert "AS n1" in sql and "AS mn1" not in sql  # strings: no min/max/avg
    assert '"we""ird"' in sql


# ------------------------------------------------------------------ sqlserver


def test_mssql_bracket_quoting_and_offset_fetch():
    sql, params = ms_a.build_select_sql(
        "[dbo].[t]",
        ["a]b", "c"],
        offset=50,
        limit=25,
        sort_by="a]b",
        sort_dir="asc",
        filters={"c": "x[y"},
    )
    assert "[a]]b]" in sql  # ] doubled inside brackets
    assert "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY" in sql
    # NULLS LAST emulation via CASE
    assert "CASE WHEN [a]]b] IS NULL THEN 1 ELSE 0 END, [a]]b] ASC" in sql
    assert params["f0"] == "%x\\[y%"  # [ escaped for T-SQL LIKE


def test_mssql_default_order_for_offset_fetch():
    sql, _ = ms_a.build_select_sql("[dbo].[t]", ["a"], offset=0, limit=10)
    assert "ORDER BY (SELECT NULL)" in sql


def test_mssql_count_sql():
    sql, _ = ms_a.build_count_sql("[dbo].[t]", ["a"])
    assert sql == "SELECT COUNT_BIG(*) FROM [dbo].[t]"


# -------------------------------------------------------------------- mongodb


def test_mongo_filter_doc_regex_escaped_and_anded_with_base_query():
    doc = mongo_a.build_filter_doc(
        ["a", "b"], filters={"a": "x.y"}, search="q", base_query={"z": 1}
    )
    assert doc["$and"][0] == {"z": 1}
    filt = doc["$and"][1]["$expr"]["$regexMatch"]
    assert filt["regex"] == re.escape("x.y") and filt["options"] == "i"
    ors = doc["$and"][2]["$or"]
    assert len(ors) == 2  # search across all columns


def test_mongo_unknown_filter_column_dropped_and_empty_doc():
    assert mongo_a.build_filter_doc(["a"], filters={"ghost": "x"}) == {}
    single = mongo_a.build_filter_doc(["a"], filters={"a": "x"})
    assert "$and" not in single  # single clause not wrapped


# --------------------------------------------------------------------- caches


def test_cached_count_ttl_and_generation_keying(monkeypatch):
    clear_caches()
    calls = []

    def compute():
        calls.append(1)
        return CountResult(42)

    key_g0 = ("s1", "src", 0)
    assert cached_count(key_g0, 60, compute).value == 42
    assert cached_count(key_g0, 60, compute).value == 42
    assert len(calls) == 1  # second hit served from cache

    # A bumped generation is a different key -> recompute.
    assert cached_count(("s1", "src", 1), 60, compute).value == 42
    assert len(calls) == 2

    # TTL expiry -> recompute.
    now = [1000.0]
    monkeypatch.setattr(adapter_base.time, "monotonic", lambda: now[0])
    cached_count(("ttl",), 10, compute)
    now[0] += 11
    cached_count(("ttl",), 10, compute)
    assert len(calls) == 4
    clear_caches()


# ----------------------------------------------------------- resolve_adapter


def test_resolve_postgres_base_and_temp():
    meta = {
        "source": "postgres",
        "connection_url": "postgresql://u:p@h/db",
        "schema": "public",
        "table": "orders",
    }
    adapter = resolve_adapter(_ctx(meta), "base")
    assert adapter.dialect == "postgres" and adapter.source_ref == '"public"."orders"'
    assert adapter.supports_stats is True

    with pytest.raises(TempTableUnavailable):
        resolve_adapter(_ctx(meta), "temp")

    meta_t = {**meta, "temp_schema": "stych_temp", "temp_table": "w1", "temp_table_generation": 3}
    temp = resolve_adapter(_ctx(meta_t), "temp")
    assert temp.source_ref == '"stych_temp"."w1"'
    assert 3 in temp.cache_info.key  # generation participates in cache identity


def test_resolve_redacted_or_missing_url_unavailable():
    for url in (None, "", "[REDACTED]", "not-a-url"):
        meta = {"source": "postgres", "connection_url": url, "table": "t"}
        with pytest.raises(AdapterUnavailable):
            resolve_adapter(_ctx(meta), "base")
        assert supports_pushdown(meta) is False


def test_resolve_temp_rejected_for_non_postgres():
    meta = {"source": "sqlserver", "connection_url": "mssql+pymssql://u:p@h/db", "table": "t"}
    with pytest.raises(AdapterError):
        resolve_adapter(_ctx(meta), "temp")


def test_resolve_sqlserver_and_mongo_from_sample():
    df = pd.DataFrame({"n": [1, 2], "s": ["a", "b"]})
    ms = resolve_adapter(
        _ctx(
            {"source": "sqlserver", "connection_url": "mssql+pymssql://u:p@h/db", "table": "t"},
            df,
        ),
        "base",
    )
    assert ms.dialect == "sqlserver" and ms.supports_stats is False
    assert ms.sample_kinds == {"n": "number", "s": "string"}

    mg = resolve_adapter(
        _ctx(
            {
                "source": "mongodb",
                "connection_url": "mongodb://h/db",
                "database": "db",
                "collection": "c",
                "query": {"z": 1},
            },
            df,
        ),
        "base",
    )
    assert mg.dialect == "mongodb"
    assert [c.name for c in mg.schema()] == ["n", "s"]
    assert mg.base_query == {"z": 1}


def test_resolve_sqlserver_working_copy_uses_sqlserver_adapter():
    meta = {
        "source": "sqlserver",
        "connection_url": "mssql+pymssql://u:p@h/db",
        "schema": "dbo",
        "table": "orders",
        "temp_schema": "stych_temp",
        "temp_table": "stych_s1",
    }
    adapter = resolve_adapter(_ctx(meta, pd.DataFrame({"id": [1]})), "temp")
    assert type(adapter).__name__ == "SqlServerTableAdapter"
    assert adapter.dialect == "sqlserver"
    assert adapter.source_ref == "[stych_temp].[stych_s1]"


def test_supports_pushdown_only_for_db_sources():
    assert supports_pushdown({"source": "upload"}) is False
    assert supports_pushdown({"source": "postgres", "connection_url": "postgresql://x/y"})
