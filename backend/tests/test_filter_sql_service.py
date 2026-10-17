"""SQL filter engine: predicate compilation, query builders, page assembly —
all string-asserted with fake adapters, no database (test_sql_diff_service style)."""

from __future__ import annotations

import pytest

from datasources.adapters.base import AdapterColumn, CountResult, SourceCacheInfo
from datasources.adapters.base import clear_caches as clear_adapter_caches
from filters import annotate_sql, sql_service
from filters.annotate_sql import duplicate_values_join
from filters.predicate_pandas import UnknownColumn
from filters.predicate_sql import ParamGen, compile_where
from filters.spec import parse_spec


def _pred(payload):
    return parse_spec({"name": "t", "select": payload}).select


def _resolver(columns=("amount", "email", "start", "end")):
    def resolve(name):
        if name in columns:
            return f't."{name}"', "number" if name in ("amount", "start", "end") else "string"
        return None

    return resolve


# ------------------------------------------------------------------ predicates


def test_eq_binds_and_coalesces():
    params = ParamGen()
    sql = compile_where(_pred({"op": "eq", "column": "amount", "value": 5}), _resolver(), params)
    assert sql == 'COALESCE(t."amount" = :fp0, FALSE)'
    assert params.values == {"fp0": 5}


def test_ne_is_distinct_from():
    sql = compile_where(
        _pred({"op": "ne", "column": "amount", "value": 5}), _resolver(), ParamGen()
    )
    assert "IS DISTINCT FROM :fp0" in sql


def test_string_ops_escape_and_case():
    params = ParamGen()
    contains = compile_where(
        _pred({"op": "contains", "column": "email", "pattern": "50%_x"}), _resolver(), params
    )
    assert "CAST(t.\"email\" AS TEXT) ILIKE :fp0 ESCAPE '\\'" in contains
    assert params.values["fp0"] == "%50\\%\\_x%"

    sensitive = compile_where(
        _pred({"op": "startswith", "column": "email", "pattern": "Ab", "case_sensitive": True}),
        _resolver(),
        ParamGen(),
    )
    assert " LIKE " in sensitive and "ILIKE" not in sensitive

    regex = compile_where(
        _pred({"op": "regex", "column": "email", "pattern": "^a+$"}), _resolver(), ParamGen()
    )
    assert " ~* :fp0" in regex

    duckdb_regex = compile_where(
        _pred({"op": "regex", "column": "email", "pattern": "^a+$"}),
        _resolver(),
        ParamGen(),
        dialect="duckdb",
    )
    assert "regexp_matches" in duckdb_regex
    assert "'i'" in duckdb_regex


def test_membership_and_between():
    params = ParamGen()
    not_in = compile_where(
        _pred({"op": "not_in", "column": "email", "values": ["a", "b", None]}),
        _resolver(),
        params,
    )
    assert "NOT IN (:fp0, :fp1)" in not_in  # None dropped, matches pandas
    between = compile_where(
        _pred({"op": "between", "column": "amount", "low": 1, "high": 9}),
        _resolver(),
        ParamGen(),
    )
    assert ">= :fp0" in between and "<= :fp1" in between


def test_column_vs_column_and_nesting():
    sql = compile_where(
        _pred(
            {
                "op": "and",
                "children": [
                    {"op": "lt", "column": "start", "value": {"column": "end"}},
                    {"op": "not", "child": {"op": "is_null", "column": "email"}},
                ],
            }
        ),
        _resolver(),
        ParamGen(),
    )
    assert 'COALESCE(t."start" < t."end", FALSE)' in sql
    assert '(NOT (t."email" IS NULL))' in sql


def test_numeric_literal_coerces_text_column():
    sql = compile_where(_pred({"op": "gt", "column": "email", "value": 5}), _resolver(), ParamGen())
    assert 'CAST(t."email" AS NUMERIC) > :fp0' in sql


def test_unknown_column_raises():
    with pytest.raises(UnknownColumn):
        compile_where(_pred({"op": "eq", "column": "nope", "value": 1}), _resolver(), ParamGen())


def test_sqlserver_predicates_use_total_tsql_boolean_expressions():
    def resolve(name):
        if name == "amount":
            return "t.[amount]", "number"
        if name == "email":
            return "t.[email]", "string"
        return None

    params = ParamGen()
    eq = compile_where(
        _pred({"op": "eq", "column": "amount", "value": 5}),
        resolve,
        params,
        dialect="sqlserver",
    )
    assert "CASE WHEN (t.[amount] = :fp0) THEN 1 ELSE 0 END = 1" in eq
    assert "COALESCE" not in eq and "FALSE" not in eq

    ne = compile_where(
        _pred({"op": "ne", "column": "amount", "value": 5}),
        resolve,
        ParamGen(),
        dialect="sqlserver",
    )
    assert "IS DISTINCT FROM" not in ne
    assert "IS NULL AND :fp0 IS NOT NULL" in ne


def test_redshift_string_predicate_uses_projectable_match_flag():
    params = ParamGen()
    predicate = compile_where(
        _pred({"op": "contains", "column": "email", "pattern": "50%_x"}),
        _resolver(),
        params,
        dialect="redshift",
    )
    assert 'CAST(t."email" AS VARCHAR(65535)) ILIKE :fp0' in predicate
    assert "ESCAPE" not in predicate
    assert params.values == {"fp0": "%50\\%\\_x%"}

    inner = sql_service.build_filter_join_sql(
        '"public"."t"',
        [{"name": "email"}],
        {"email": ('t."email"', "string")},
        [],
        select_sql=predicate,
        rule_sqls=[predicate],
        group_expr=None,
        order_expr="NULL",
        dialect="redshift",
    )
    assert f"CASE WHEN ({predicate}) THEN 1 ELSE 0 END AS _m" in inner
    assert f"CASE WHEN ({predicate}) THEN 1 ELSE 0 END AS _r0" in inner

    summary = sql_service.build_filter_summary_sql(inner, 1, dialect="redshift")
    assert "q._m = 1" in summary and "q._r0 = 1" in summary


# ------------------------------------------------------------------ builders


def test_duplicate_values_join_sql():
    step = parse_spec(
        {"name": "t", "annotate": [{"kind": "duplicate_values", "id": "d", "columns": ["email"]}]}
    ).annotate[0]
    join = duplicate_values_join(step, '"public"."t"', "a0")
    assert 'COUNT(*) AS "d_count"' in join.join_sql
    assert 'MIN(CAST("email" AS TEXT)) AS "d_group"' in join.join_sql
    assert 'WHERE "email" IS NOT NULL' in join.join_sql
    assert "HAVING COUNT(*) >= 2" in join.join_sql
    assert 'ON a0.g0 = t."email"' in join.join_sql
    assert join.outputs == {"d_count": 'a0."d_count"', "d_group": 'a0."d_group"'}


def test_duplicate_values_join_uses_closed_brackets_on_sqlserver():
    step = parse_spec(
        {"name": "t", "annotate": [{"kind": "duplicate_values", "id": "d", "columns": ["email"]}]}
    ).annotate[0]
    join = duplicate_values_join(step, "[dbo].[t]", "a0", quote="[", dialect="sqlserver")
    assert "[email]" in join.join_sql
    assert "[email[" not in join.join_sql


def _display_spec():
    return parse_spec(
        {
            "name": "flags",
            "annotate": [{"kind": "duplicate_values", "id": "d", "columns": ["email"]}],
            "select": {"op": "lt", "column": "balance", "value": 0},
            "present": {
                "rules": [{"id": "neg", "severity": "danger", "tooltip": "bal {balance}"}],
                "group_by": "d_group",
            },
        }
    )


def _schema():
    return [
        AdapterColumn("id", "integer", "number"),
        AdapterColumn("balance", "numeric", "number"),
        AdapterColumn("email", "text", "string"),
    ]


def test_build_display_marks_annotations_and_nulls_missing_joins():
    spec = _display_spec()
    display, exprs = sql_service.build_display(_schema(), spec, joins=[])
    names = [c["name"] for c in display]
    assert names == ["id", "balance", "email", "d_count", "d_group"]
    assert display[3]["origin"] == "annotation"
    # no join materialized -> NULL literal, predicates over it are FALSE
    assert exprs["d_count"] == ("NULL", "number")


def test_join_and_page_sql_shapes():
    spec = _display_spec()
    step = spec.annotate[0]
    join = duplicate_values_join(step, '"s"."t"', "a0")
    display, exprs = sql_service.build_display(_schema(), spec, [join])
    params = ParamGen()
    select_sql = compile_where(spec.select, lambda n: exprs.get(n), params)
    inner = sql_service.build_filter_join_sql(
        '"s"."t"',
        display,
        exprs,
        [join],
        select_sql=select_sql,
        rule_sqls=[select_sql],
        group_expr=exprs["d_group"][0],
        order_expr='(t."id")::text',
    )
    assert 't."id" AS d0' in inner and 'a0."d_group" AS d4' in inner
    assert f"({select_sql}) AS _m" in inner and "AS _r0" in inner
    assert 'a0."d_group" AS _grp' in inner and '(t."id")::text AS _ord' in inner
    assert "LEFT JOIN (" in inner

    where_gen = ParamGen("w")
    where = sql_service.build_filter_where(
        display,
        where_gen,
        matches_only=True,
        enabled_rule_idx=[0],
        n_rules=1,
        filters={"email": "x"},
        search="q",
    )
    assert "q._m" in where and "(q._r0)" in where
    assert "CAST(q.d2 AS TEXT) ILIKE :w0 ESCAPE '\\'" in where
    assert where_gen.values["w0"] == "%x%"

    page_gen = ParamGen("pg")
    grouped = sql_service.build_filter_page_sql(
        inner, where, display, page_gen, grouping=True, offset=0, limit=100
    )
    assert "ORDER BY (q._grp IS NULL) ASC, q._grp, q._ord" in grouped
    assert "LIMIT :pg0 OFFSET :pg1" in grouped

    sorted_sql = sql_service.build_filter_page_sql(
        inner, "", display, ParamGen("pg"), grouping=False, sort_by="balance", sort_dir="desc"
    )
    assert "ORDER BY q.d1 DESC NULLS LAST, q._ord" in sorted_sql


def test_sqlserver_page_and_summary_use_tsql_shapes():
    display = [{"name": "balance"}]
    page = sql_service.build_filter_page_sql(
        "SELECT 1 AS d0, 1 AS _m, NULL AS _grp, 1 AS _ord",
        " WHERE (q._m = 1)",
        display,
        ParamGen("pg"),
        grouping=True,
        dialect="sqlserver",
    )
    assert "CASE WHEN q._grp IS NULL THEN 1 ELSE 0 END) ASC" in page
    assert "OFFSET :pg1 ROWS FETCH NEXT :pg0 ROWS ONLY" in page
    assert "LIMIT" not in page and "NULLS LAST" not in page

    summary = sql_service.build_filter_summary_sql(
        "SELECT 1 AS _m, NULL AS _grp, 1 AS _r0", 1, dialect="sqlserver"
    )
    assert "SUM(CASE WHEN ((q._m = 1)) THEN 1 ELSE 0 END)" in summary
    assert "COUNT(*) FILTER" not in summary


def test_empty_enabled_rules_compiles_to_false():
    where = sql_service.build_filter_where(
        [{"name": "id"}], ParamGen("w"), matches_only=False, enabled_rule_idx=[], n_rules=0
    )
    assert "FALSE" in where


# ------------------------------------------------------------------ end-to-end


class _FakeAdapter:
    source_ref = '"public"."t"'
    db_schema = "public"
    table = "t"
    is_table = True
    connection_url = "postgresql://fake"

    def __init__(self, generation=0):
        self.cache_info = SourceCacheInfo(key=("pg", "url", "t", generation), ttl=60)
        self.queries = []

    def schema(self):
        return _schema()

    def count(self, filters=None, search=None):
        return CountResult(4)

    def run(self, sql, params=None, timeout_ms=0):
        self.queries.append((sql, params or {}))
        if "pg_index" in sql:
            return ["attname"], [("id",)]
        if "COUNT(*) = COUNT(DISTINCT" in sql:
            return ["ok"], [(True,)]
        if "COUNT(*) FILTER" in sql:
            # total, matched, groups, r0
            return ["total", "matched", "groups", "r0"], [(4, 2, 1, 2)]
        if sql.startswith("SELECT COUNT(*) FROM ("):
            return ["c"], [(2,)]
        # page rows: d0..d4, _m, _r0, _grp, _ord
        return (
            ["d0", "d1", "d2", "d3", "d4", "_m", "_r0", "_grp", "_ord"],
            [
                (1, -5.0, "a@x.com", 2, "grp-a", True, True, "grp-a", "1"),
                (3, -1.0, "a@x.com", 2, "grp-a", True, True, "grp-a", "3"),
            ],
        )


@pytest.fixture(autouse=True)
def _clear_caches():
    clear_adapter_caches()
    sql_service.clear_caches()
    annotate_sql.clear_registry()
    yield
    clear_adapter_caches()
    sql_service.clear_caches()
    annotate_sql.clear_registry()


def test_build_sql_filter_page_end_to_end():
    adapter = _FakeAdapter()
    page = sql_service.build_sql_filter_page(
        adapter, _display_spec(), session_id="s1", matches_only=True
    )
    assert page["kind"] == "filter"
    assert page["summary"]["matched_rows"] == 2
    assert page["summary"]["rule_counts"] == {"neg": 2}
    assert page["summary"]["identity_resolved"] == "key:id"
    assert page["summary"]["group_count"] == 1
    assert page["filtered_rows"] == 2
    assert [r[0] for r in page["rows"]] == [1, 3]
    # both rows share group ordinal 0 and carry the rendered tooltip
    assert page["row_meta"][0]["g"] == 0 and page["row_meta"][1]["g"] == 0
    assert page["row_meta"][0]["s"] == "danger"
    assert page["row_meta"][0]["t"] == "bal -5.0"
    # every literal was bound — no raw values inside any executed SQL
    for sql, _params in adapter.queries:
        assert "-5" not in sql and "%x%" not in sql


def test_summary_cache_invalidates_on_generation_bump():
    def summary_queries(adapter):
        return [q for q, _ in adapter.queries if "COUNT(*) FILTER" in q]

    a0 = _FakeAdapter(generation=0)
    sql_service.build_sql_filter_page(a0, _display_spec(), session_id="s1")
    sql_service.build_sql_filter_page(a0, _display_spec(), session_id="s1")
    assert len(summary_queries(a0)) == 1  # second call hit the cache

    a1 = _FakeAdapter(generation=1)  # temp_table_generation bumped
    sql_service.build_sql_filter_page(a1, _display_spec(), session_id="s1")
    assert len(summary_queries(a1)) == 1  # re-queried under the new key


def test_keyless_source_degrades_but_pushdown_still_works():
    class _KeylessAdapter(_FakeAdapter):
        def run(self, sql, params=None, timeout_ms=0):
            self.queries.append((sql, params or {}))
            if "pg_index" in sql:
                return ["attname"], []
            if "COUNT(*) = COUNT(DISTINCT" in sql:
                return ["ok"], [(False,)]
            if "COUNT(*) FILTER" in sql:
                return ["total", "matched", "groups", "r0"], [(4, 2, 0, 2)]
            if sql.startswith("SELECT COUNT(*) FROM ("):
                return ["c"], [(2,)]
            return (
                ["d0", "d1", "d2", "d3", "d4", "_m", "_r0", "_grp", "_ord"],
                [(1, -5.0, "a", None, None, True, True, None, "x")],
            )

    spec = parse_spec(
        {
            "name": "t",
            "annotate": [
                {
                    "kind": "fuzzy_duplicates",
                    "id": "dup",
                    "on": "email",
                    "strategy": "token_sort",
                }
            ],
            "select": {"op": "lt", "column": "balance", "value": 0},
        }
    )
    page = sql_service.build_sql_filter_page(_KeylessAdapter(), spec, session_id="s1")
    assert page["summary"]["identity_resolved"] == "none"
    assert page["summary"]["degraded"] == "no_key_annotations"
    assert page["summary"]["annotations"][0]["status"] == "skipped"
    # select pushdown still worked
    assert page["summary"]["matched_rows"] == 2


# -- annotation materialization across engines ------------------------------


def _fake_adapter(dialect, **attrs):
    from types import SimpleNamespace

    return SimpleNamespace(
        dialect=dialect, ident_quote="`" if dialect != "postgres" else '"', **attrs
    )


def test_annotation_join_quotes_identifiers_for_the_target_engine():
    """A Postgres-quoted column reaching BigQuery is a syntax error, so the
    join must use the engine's own quote character throughout."""
    from filters.annotate_sql import _table_join
    from filters.spec import AnnotationOutput, SqlStep

    step = SqlStep(
        id="s1",
        kind="sql",
        key_column="id",
        sql="SELECT 1",
        outputs=[AnnotationOutput(name="flag", kind="number")],
    )
    adapter = _fake_adapter("bigquery", connection_config={"project_id": "p"}, db_schema="ds")
    join = _table_join(adapter, step, "a0", "t1", "id", "id")
    assert "`id`" in join.join_sql and '"id"' not in join.join_sql
    assert "CAST(" in join.join_sql and "::text" not in join.join_sql


def test_sql_annotation_materialization_uses_select_into_for_sqlserver(monkeypatch):
    from filters.spec import AnnotationOutput, SqlStep

    class Runner:
        temp_schema = "stych_temp"

        def __init__(self):
            self.statements = []

        def ensure_temp_schema(self):
            pass

        def drop_temp_table(self, table):
            self.dropped = table

        def execute_statements_transaction(self, statements, *, allowed_schema):
            self.statements.extend(statements)

        def get_row_count(self, schema, table):
            assert (schema, table) == ("stych_temp", "flt_hash_s1")
            return 3

    runner = Runner()
    adapter = _fake_adapter("sqlserver", connection_url="mssql+pymssql://fake")
    adapter.source_ref = "dbo.[source]"
    probes = []

    def run(sql):
        probes.append(sql)
        return ["id", "flag"], []

    adapter.run = run
    monkeypatch.setattr(annotate_sql, "_runner", lambda _adapter: runner)
    monkeypatch.setattr(
        annotate_sql,
        "_scratch_ref",
        lambda _adapter, table: f"[stych_temp].[{table}]",
    )
    step = SqlStep(
        id="s1",
        kind="sql",
        key_column="id",
        sql="SELECT id, 1 AS flag FROM {table}",
        outputs=[AnnotationOutput(name="flag", dtype="bool")],
    )

    assert annotate_sql._materialize_sql(adapter, step, "flt_hash_s1") == 3
    assert "SELECT * INTO [stych_temp].[flt_hash_s1]" in runner.statements[0]
    assert "CREATE TABLE" not in runner.statements[0]
    assert "SELECT TOP 0 *" in probes[0]


def test_fuzzy_duplicates_stay_off_engines_that_would_sample_them():
    """Fuzzy grouping needs every value in memory, so it is refused rather than
    silently computed from the first N rows."""
    from filters.annotate_sql import _can_compute_fuzzy

    assert _can_compute_fuzzy(_fake_adapter("postgres", connection_url="postgresql://x/y")) is True
    assert _can_compute_fuzzy(_fake_adapter("bigquery")) is False
    assert _can_compute_fuzzy(_fake_adapter("databricks")) is False
