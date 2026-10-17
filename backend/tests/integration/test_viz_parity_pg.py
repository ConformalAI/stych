"""
Live-Postgres parity tests: the SQL compiler must produce the same tidy table
as the pandas compiler for the same spec + data. Skipped unless
STYCH_TEST_POSTGRES_URL is set, e.g.:

    STYCH_TEST_POSTGRES_URL=postgresql+psycopg2://user:pass@host:5432/db \
        pytest tests/integration/test_viz_parity_pg.py
"""

from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from viz.catalog import build_catalog_pandas, build_catalog_sql
from viz.compile_pandas import execute_pandas
from viz.compile_sql import compile_spec_sql
from viz.executor import sql_records_to_vizdata
from viz.expr import extend_catalog, resolve_calculations
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec, validate_spec

PG_URL = os.getenv("STYCH_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="STYCH_TEST_POSTGRES_URL not set — live Postgres required"
)

_DF = pd.DataFrame(
    {
        "order_date": pd.to_datetime(
            [
                "2024-01-05",
                "2024-01-20",
                "2024-02-10",
                "2024-02-15",
                "2024-03-01",
                "2024-03-02",
                "2024-03-20",
                None,
            ]
        ),
        "region": ["EU", "NA", "EU", "APAC", "NA", "EU", None, "EU"],
        "item": ["a", "b", "a", "c", "b", "b", "a", "a"],
        "revenue": [100.0, 250.5, 80.0, None, 40.0, 10.0, 5.0, 60.0],
        "qty": [1, 2, 3, 4, 5, 6, 7, 8],
    }
)

_COLUMNS = [
    {"column_name": "order_date", "data_type": "date"},
    {"column_name": "region", "data_type": "text"},
    {"column_name": "item", "data_type": "text"},
    {"column_name": "revenue", "data_type": "double precision"},
    {"column_name": "qty", "data_type": "integer"},
]


@pytest.fixture(scope="module")
def pg_table():
    suffix = uuid.uuid4().hex[:8]
    table = f"viz_parity_{suffix}"
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                f'CREATE TABLE public."{table}" (order_date DATE, region TEXT, '
                f"item TEXT, revenue DOUBLE PRECISION, qty INT)"
            )
        )
        for _, r in _DF.iterrows():
            conn.execute(
                text(
                    f'INSERT INTO public."{table}" VALUES '
                    f"(:order_date, :region, :item, :revenue, :qty)"
                ),
                {
                    "order_date": None if pd.isna(r["order_date"]) else r["order_date"].date(),
                    "region": r["region"] if isinstance(r["region"], str) else None,
                    "item": r["item"],
                    "revenue": None if pd.isna(r["revenue"]) else float(r["revenue"]),
                    "qty": int(r["qty"]),
                },
            )
    yield engine, table
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS public."{table}"'))
    engine.dispose()


def _run_both(pg, spec_dict):
    engine, table = pg
    pandas_catalog = build_catalog_pandas(_DF)
    sql_catalog = build_catalog_sql("public", table, _COLUMNS, len(_DF), preview_df=_DF)
    base_spec = VizSpec.model_validate(spec_dict)

    # Calculations resolve per engine (db types differ between catalogs).
    p_calcs, p_errs = resolve_calculations(base_spec.calculations(), pandas_catalog)
    assert p_errs == [], p_errs
    p_eff = extend_catalog(pandas_catalog, p_calcs)
    spec = normalize_spec(base_spec, p_eff)
    assert validate_spec(spec, p_eff) == []
    pandas_out = execute_pandas(_DF, build_plan(spec, calcs=p_calcs))

    s_calcs, s_errs = resolve_calculations(base_spec.calculations(), sql_catalog)
    assert s_errs == [], s_errs
    s_eff = extend_catalog(sql_catalog, s_calcs)
    s_spec = normalize_spec(base_spec, s_eff)
    sql = compile_spec_sql(build_plan(s_spec, calcs=s_calcs), s_eff, "public", table)
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        records = [dict(zip(result.keys(), row)) for row in result.fetchall()]
    sql_out = sql_records_to_vizdata(records, build_plan(s_spec, calcs=s_calcs))
    return pandas_out, sql_out


def _assert_rows_match(pandas_out, sql_out, unordered=False):
    assert [c.name for c in pandas_out.columns] == [c.name for c in sql_out.columns]
    p_rows, s_rows = pandas_out.rows, sql_out.rows
    if unordered:
        key = lambda r: tuple("~" if v is None else str(v) for v in r)  # noqa: E731
        p_rows, s_rows = sorted(p_rows, key=key), sorted(s_rows, key=key)
    assert len(p_rows) == len(s_rows), (p_rows, s_rows)
    for pr, sr in zip(p_rows, s_rows):
        for pv, sv in zip(pr, sr):
            if isinstance(pv, float) and isinstance(sv, (int, float)):
                assert sv == pytest.approx(pv, rel=1e-6, abs=1e-6), (pr, sr)
            else:
                assert pv == sv, (pr, sr)


def test_parity_bar_sum(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, s)
    assert p.rows[0] == ["NA", 290.5]


def test_parity_monthly_multiseries(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    _assert_rows_match(p, s)


@pytest.mark.parametrize("unit", ["year", "quarter", "week", "day"])
def test_parity_time_units(pg_table, unit):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": unit},
                "y": {"aggregate": "count", "type": "quantitative"},
            },
        },
    )
    _assert_rows_match(p, s)


def test_parity_count_only(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    _assert_rows_match(p, s)


def test_parity_histogram(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "histogram"},
            "encoding": {"x": {"field": "revenue", "type": "quantitative", "bin": {"maxbins": 5}}},
        },
    )
    _assert_rows_match(p, s)
    assert s.meta["bins"]["x"]["width"] == pytest.approx(p.meta["bins"]["x"]["width"])


def test_parity_topk_others_mean(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 1, "others": True}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "mean"},
            },
        },
    )
    _assert_rows_match(p, s)


def test_parity_multiple_topk_dimensions(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 2, "others": True}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "color": {
                    "field": "region",
                    "type": "nominal",
                    "topK": {"n": 2, "others": True},
                },
            },
        },
    )
    _assert_rows_match(p, s)


def test_parity_filters(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "qty", "predicate": "between", "value": [2, 6]},
                {"op": "filter", "field": "revenue", "predicate": "notnull"},
                {"op": "filter", "field": "region", "predicate": "in", "value": ["EU", "NA"]},
                {"op": "filter", "field": "item", "predicate": "contains", "value": "B"},
                {"op": "filter", "field": "order_date", "predicate": "gte", "value": "2024-01-10"},
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    _assert_rows_match(p, s)
    assert len(p.rows) > 0  # the filter stack actually selects something


@pytest.mark.parametrize("predicate", ["in", "notin"])
def test_parity_numeric_membership_filter(pg_table, predicate):
    # Float-formatted values against an int column: both engines must compare
    # numerically (float8::text "5" vs pandas "5.0" never agree as text).
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "qty", "predicate": predicate, "value": [5.0, 7, "2"]}
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    _assert_rows_match(p, s)
    assert len(p.rows) > 0


def test_parity_distinct_and_heatmap(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "heatmap"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "item", "type": "nominal"},
                "color": {"aggregate": "count", "type": "quantitative"},
            },
        },
    )
    _assert_rows_match(p, s)
    p2, s2 = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "item", "type": "nominal", "aggregate": "distinct"},
            },
        },
    )
    _assert_rows_match(p2, s2)


def test_parity_calc_measure_null_safe_division(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "calculate", "as": "rev_per_qty", "expr": "revenue / (qty - 2)"},
            ],
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "rev_per_qty", "type": "quantitative", "aggregate": "mean"},
            },
        },
    )
    # qty=2 row divides by zero → null in BOTH engines (NULLIF parity)
    _assert_rows_match(p, s)


def test_parity_calc_boolean_dimension(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "calculate", "as": "is_big", "expr": "revenue >= 60"},
            ],
            "encoding": {"x": {"field": "is_big", "type": "nominal"}},
        },
    )
    # boolean → 'true'/'false' text in both engines; null revenue rows dropped.
    _assert_rows_match(p, s)
    assert {r[0] for r in p.rows} == {"true", "false"}


def test_parity_calc_if_label_round_and_filter(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "calculate", "as": "band", "expr": "if(qty >= 5, 'high', 'low')"},
                {"op": "calculate", "as": "rounded", "expr": "round(revenue / 3, 1)"},
                {"op": "filter", "field": "rounded", "predicate": "gt", "value": 3},
            ],
            "encoding": {
                "x": {"field": "band", "type": "nominal"},
                "y": {"field": "rounded", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, s)
    assert len(p.rows) > 0


def test_parity_scatter_raw_unordered(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative"},
                "y": {"field": "revenue", "type": "quantitative"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    _assert_rows_match(p, s, unordered=True)


def test_parity_scatter_aggregated_by_color(pg_table):
    p, s = _run_both(
        pg_table,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    _assert_rows_match(p, s)
