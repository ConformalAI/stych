"""
Live-MongoDB parity tests: the aggregation compiler must produce the same tidy
table as the pandas compiler for the same spec + data. Skipped unless
STYCH_TEST_MONGODB_URL is set, e.g.:

    STYCH_TEST_MONGODB_URL=mongodb://localhost:27017 \
        pytest tests/integration/test_viz_parity_mongo.py

The dataset and the specs mirror ``test_viz_parity_pg.py`` deliberately: the
same cases proving pandas/SQL parity should prove pandas/Mongo parity, or the
third engine is not really a peer of the other two.
"""

from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest

from viz.catalog import build_catalog_pandas, build_catalog_sql
from viz.compile_pandas import execute_pandas
from viz.execute_mongo import run_mongo_plan
from viz.executor import sql_records_to_vizdata
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec, validate_spec

MONGO_URL = os.getenv("STYCH_TEST_MONGODB_URL")

pytestmark = pytest.mark.skipif(
    not MONGO_URL, reason="STYCH_TEST_MONGODB_URL not set — live MongoDB required"
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

# Shaped like MongoScratchRunner.get_table_columns: dtypes inferred from a sample.
_COLUMNS = [
    {"column_name": "order_date", "data_type": "datetime64[ns]"},
    {"column_name": "region", "data_type": "object"},
    {"column_name": "item", "data_type": "object"},
    {"column_name": "revenue", "data_type": "float64"},
    {"column_name": "qty", "data_type": "int64"},
]


@pytest.fixture(scope="module")
def mongo_collection():
    from pymongo import MongoClient

    client = MongoClient(MONGO_URL)
    database = f"viz_parity_{uuid.uuid4().hex[:8]}"
    collection = "orders"
    docs = []
    for _, r in _DF.iterrows():
        docs.append(
            {
                "order_date": None if pd.isna(r["order_date"]) else r["order_date"].to_pydatetime(),
                "region": r["region"] if isinstance(r["region"], str) else None,
                "item": r["item"],
                "revenue": None if pd.isna(r["revenue"]) else float(r["revenue"]),
                "qty": int(r["qty"]),
            }
        )
    client[database][collection].insert_many(docs)
    from agents.mongo_agent import MongoScratchRunner

    runner = MongoScratchRunner({"connection_url": MONGO_URL, "database": database})
    yield runner, database, collection
    client.drop_database(database)
    client.close()


def _run_mongo(runner, database, collection, plan, catalog):
    """The production path — including the pipeline guard the runner applies."""
    return run_mongo_plan(runner, plan, catalog, database=database, collection=collection)


def _run_both(mongo, spec_dict):
    runner, database, collection = mongo
    pandas_catalog = build_catalog_pandas(_DF)
    mongo_catalog = build_catalog_sql(database, collection, _COLUMNS, len(_DF), preview_df=_DF)
    base_spec = VizSpec.model_validate(spec_dict)

    spec = normalize_spec(base_spec, pandas_catalog)
    assert validate_spec(spec, pandas_catalog) == []
    pandas_out = execute_pandas(_DF, build_plan(spec))

    m_spec = normalize_spec(base_spec, mongo_catalog)
    plan = build_plan(m_spec)
    records = _run_mongo(runner, database, collection, plan, mongo_catalog)
    mongo_out = sql_records_to_vizdata(records, build_plan(m_spec), engine="mongodb")
    return pandas_out, mongo_out


def _assert_rows_match(pandas_out, mongo_out, unordered=False):
    assert [c.name for c in pandas_out.columns] == [c.name for c in mongo_out.columns]
    p_rows, m_rows = pandas_out.rows, mongo_out.rows
    if unordered:
        key = lambda r: tuple("~" if v is None else str(v) for v in r)  # noqa: E731
        p_rows, m_rows = sorted(p_rows, key=key), sorted(m_rows, key=key)
    assert len(p_rows) == len(m_rows), (p_rows, m_rows)
    for pr, mr in zip(p_rows, m_rows):
        for pv, mv in zip(pr, mr):
            if isinstance(pv, float) and isinstance(mv, (int, float)):
                assert mv == pytest.approx(pv, rel=1e-6, abs=1e-6), (pr, mr)
            else:
                assert pv == mv, (pr, mr)


def test_parity_bar_sum(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, m)
    assert p.rows[0] == ["NA", 290.5]


def test_parity_monthly_multiseries(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    _assert_rows_match(p, m)


@pytest.mark.parametrize("unit", ["year", "quarter", "week", "day"])
def test_parity_time_units(mongo_collection, unit):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": unit},
                "y": {"aggregate": "count", "type": "quantitative"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_count_only(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {"mark": {"type": "bar"}, "encoding": {"x": {"field": "region", "type": "nominal"}}},
    )
    _assert_rows_match(p, m)


def test_parity_histogram(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "histogram"},
            "encoding": {"x": {"field": "revenue", "type": "quantitative", "bin": {"maxbins": 5}}},
        },
    )
    _assert_rows_match(p, m)
    assert m.meta["bins"]["x"]["width"] == pytest.approx(p.meta["bins"]["x"]["width"])


def test_parity_topk_others_mean(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 1, "others": True}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "mean"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_topk_without_others(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 2, "others": False}},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_distinct_count(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "item", "type": "quantitative", "aggregate": "distinct"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_min_max(mongo_collection):
    for aggregate in ("min", "max"):
        p, m = _run_both(
            mongo_collection,
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "region", "type": "nominal"},
                    "y": {"field": "revenue", "type": "quantitative", "aggregate": aggregate},
                },
            },
        )
        _assert_rows_match(p, m)


def test_parity_raw_scatter_with_tooltip(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative"},
                "y": {"field": "revenue", "type": "quantitative"},
                "tooltip": [{"field": "region", "type": "nominal"}],
            },
        },
    )
    _assert_rows_match(p, m)


@pytest.mark.parametrize(
    "flt",
    [
        {"field": "region", "predicate": "eq", "value": "EU"},
        {"field": "region", "predicate": "neq", "value": "EU"},
        {"field": "revenue", "predicate": "gt", "value": 50},
        {"field": "revenue", "predicate": "lte", "value": 80},
        {"field": "revenue", "predicate": "between", "value": [10, 100]},
        {"field": "revenue", "predicate": "isnull"},
        {"field": "revenue", "predicate": "notnull"},
        {"field": "region", "predicate": "in", "value": ["EU", "NA"]},
        {"field": "region", "predicate": "notin", "value": ["EU"]},
        {"field": "item", "predicate": "contains", "value": "A"},
    ],
)
def test_parity_filters(mongo_collection, flt):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "transform": [{"op": "filter", **flt}],
            "encoding": {
                "x": {"field": "item", "type": "nominal"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_filter_any_or_clause(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "transform": [
                {
                    "op": "filter_any",
                    "clauses": [
                        [{"op": "filter", "field": "region", "predicate": "eq", "value": "EU"}],
                        [{"op": "filter", "field": "qty", "predicate": "gte", "value": 7}],
                    ],
                }
            ],
            "encoding": {
                "x": {"field": "item", "type": "nominal"},
                "y": {"aggregate": "count", "type": "quantitative"},
            },
        },
    )
    _assert_rows_match(p, m)


def test_parity_sort_by_encoding(mongo_collection):
    p, m = _run_both(
        mongo_collection,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {
                    "field": "item",
                    "type": "nominal",
                    "sort": {"encoding": "y", "order": "asc"},
                },
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    _assert_rows_match(p, m)


# ---------------------------------------------------------------------------
# End to end through viz.executor — proves the dispatch, not just the compiler
# ---------------------------------------------------------------------------


async def test_run_spec_dispatches_a_mongo_session_to_the_aggregation_engine(mongo_collection):
    """A MongoDB session resolves to the SOURCE plane, where the SQL compiler
    has no target. The executor must route it to the aggregation engine."""
    from datetime import datetime

    from agents.state import AgentState, DataContext
    from viz.executor import run_spec

    _runner, database, collection = mongo_collection
    state = AgentState(session_id="viz-mongo", metadata={"user_id": "test-user"})
    state.set_data_context(
        DataContext(
            file_id="orders-1",
            filename=f"MongoDB: {database}.{collection}",
            raw_dataframe=_DF.copy(deep=True),
            working_dataframe=_DF.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "mongodb",
                "connection_url": MONGO_URL,
                "database": database,
                "collection": collection,
            },
        )
    )

    data = await run_spec(
        state,
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "region", "type": "nominal"},
                    "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
                },
            }
        ),
    )

    assert data.meta["engine"] == "mongodb"
    assert [c.name for c in data.columns] == ["x", "y"]
    # Null region is dropped; measure sorts descending with nulls last. APAC's
    # only revenue is null, so its sum is null rather than a misleading 0.
    assert data.rows == [["NA", 290.5], ["EU", 250.0], ["APAC", None]]


async def test_run_spec_rejects_a_calculated_field_rather_than_ignoring_it(mongo_collection):
    """Calculated columns have no MongoDB materializer yet. Silently dropping
    the calculation would render a chart of the wrong thing."""
    from datetime import datetime

    from agents.state import AgentState, DataContext
    from viz.compile_mongo import UnsupportedVizFeature
    from viz.executor import run_spec

    _runner, database, collection = mongo_collection
    state = AgentState(session_id="viz-mongo-calc", metadata={"user_id": "test-user"})
    state.set_data_context(
        DataContext(
            file_id="orders-2",
            filename=f"MongoDB: {database}.{collection}",
            raw_dataframe=_DF.copy(deep=True),
            working_dataframe=_DF.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "mongodb",
                "connection_url": MONGO_URL,
                "database": database,
                "collection": collection,
            },
        )
    )

    with pytest.raises(UnsupportedVizFeature):
        await run_spec(
            state,
            VizSpec.model_validate(
                {
                    "mark": {"type": "bar"},
                    "transform": [
                        {"op": "calculate", "expr": "revenue * 2", "as": "double_revenue"}
                    ],
                    "encoding": {
                        "x": {"field": "region", "type": "nominal"},
                        "y": {
                            "field": "double_revenue",
                            "type": "quantitative",
                            "aggregate": "sum",
                        },
                    },
                }
            ),
        )
