"""Selection → filters/stats and the deterministic parts of chart inquiry."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from viz.catalog import build_catalog_pandas
from viz.compile_pandas import execute_pandas
from viz.compile_sql import compile_spec_sql
from viz.plan import build_plan
from viz.selection import (
    Selection,
    _bucket_end,
    match_rows,
    selection_stats,
    selection_to_filters,
)
from viz.spec import VizSpec
from viz.validate import normalize_spec

_DF = pd.DataFrame(
    {
        "order_date": pd.to_datetime(
            ["2024-01-05", "2024-01-20", "2024-02-10", "2024-02-15", "2024-03-01", "2024-03-02"]
        ),
        "region": ["EU", "NA", "EU", "APAC", "NA", "EU"],
        "channel": ["web", "web", "store", "web", "store", "web"],
        "revenue": [100.0, 250.0, 80.0, 20.0, 40.0, 10.0],
    }
)

_BAR_SPEC = {
    "mark": {"type": "bar"},
    "encoding": {
        "x": {"field": "region", "type": "nominal"},
        "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
    },
}


def materialize(spec_dict):
    catalog = build_catalog_pandas(_DF)
    spec = normalize_spec(VizSpec.model_validate(spec_dict), catalog)
    return spec, execute_pandas(_DF, build_plan(spec))


def sel(payload):
    return Selection.model_validate(payload if isinstance(payload, dict) else {"items": payload})


# ---------------------------------------------------------------------------
# selection_to_filters
# ---------------------------------------------------------------------------


def test_values_selection_becomes_in_filter():
    spec, _ = materialize(_BAR_SPEC)
    filters, errors = selection_to_filters(spec, sel([{"channel": "x", "values": ["EU", "NA"]}]))
    assert errors == []
    assert len(filters) == 1
    assert filters[0].field == "region" and filters[0].predicate == "in"
    assert filters[0].value == ["EU", "NA"]


def test_timeunit_values_become_bucket_envelope():
    spec_dict = {
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
        },
    }
    spec, _ = materialize(spec_dict)
    filters, errors = selection_to_filters(
        spec, sel([{"channel": "x", "values": ["2024-01-01T00:00:00", "2024-02-01T00:00:00"]}])
    )
    assert errors == []
    assert [(f.predicate, f.value) for f in filters] == [
        ("gte", "2024-01-01T00:00:00"),
        ("lt", "2024-03-01T00:00:00"),
    ]


def test_disjoint_time_buckets_are_rejected_instead_of_broadened():
    spec_dict = {
        "mark": {"type": "line"},
        "encoding": {
            "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
        },
    }
    spec, _ = materialize(spec_dict)
    filters, errors = selection_to_filters(
        spec,
        sel([{"channel": "x", "values": ["2024-01-01T00:00:00", "2024-03-01T00:00:00"]}]),
    )
    assert filters == []
    assert errors and errors[0].code == "selection_not_exact"


def test_multi_channel_clause_becomes_exact_and_filters():
    spec_dict = {
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            "color": {"field": "channel", "type": "nominal"},
        },
    }
    spec, _ = materialize(spec_dict)
    filters, errors = selection_to_filters(
        spec,
        sel(
            {
                "clauses": [
                    {
                        "items": [
                            {"channel": "x", "values": ["EU"]},
                            {"channel": "color", "values": ["web"]},
                        ]
                    },
                    {
                        "items": [
                            {"channel": "x", "values": ["NA"]},
                            {"channel": "color", "values": ["store"]},
                        ]
                    },
                ]
            }
        ),
    )
    assert errors == []
    assert len(filters) == 1
    grouped = filters[0]
    assert grouped.op == "filter_any"
    assert [[f.value for f in clause] for clause in grouped.clauses] == [
        [["EU"], ["web"]],
        [["NA"], ["store"]],
    ]
    catalog = build_catalog_pandas(_DF)
    probe = normalize_spec(
        VizSpec.model_validate({**spec_dict, "transform": [item.model_dump() for item in filters]}),
        catalog,
    )
    out = execute_pandas(_DF, build_plan(probe))
    assert {tuple(row) for row in out.rows} == {("EU", "web", 110.0), ("NA", "store", 40.0)}
    sql = compile_spec_sql(build_plan(probe), catalog, "stych_temp", "selection_test")
    assert " OR " in sql and "CAST(\"region\" AS TEXT) IN ('EU')" in sql


def test_tuple_selection_matches_only_selected_cells():
    _, data = materialize(
        {
            "mark": {"type": "heatmap"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "channel", "type": "nominal"},
                "color": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        }
    )
    selected = match_rows(
        data,
        sel(
            {
                "clauses": [
                    {
                        "items": [
                            {"channel": "x", "values": ["EU"]},
                            {"channel": "y", "values": ["web"]},
                        ]
                    },
                    {
                        "items": [
                            {"channel": "x", "values": ["NA"]},
                            {"channel": "y", "values": ["store"]},
                        ]
                    },
                ]
            }
        ),
    )
    assert [data.rows[i][:2] for i in selected] == [["EU", "web"], ["NA", "store"]]


def test_range_selection_becomes_gte_lte():
    spec_dict = {
        "mark": {"type": "scatter"},
        "encoding": {
            "x": {"field": "revenue", "type": "quantitative"},
            "y": {"field": "revenue", "type": "quantitative"},
        },
    }
    spec, _ = materialize(spec_dict)
    filters, errors = selection_to_filters(
        spec, sel([{"channel": "x", "range": {"min": 50, "max": 200}}])
    )
    assert errors == []
    assert [(f.predicate, f.value) for f in filters] == [("gte", 50), ("lte", 200)]


def test_selection_on_measure_rejected():
    spec, _ = materialize(_BAR_SPEC)
    _, errors = selection_to_filters(spec, sel([{"channel": "y", "values": [100]}]))
    assert errors and errors[0].code == "selection_on_measure"


def test_selection_filters_round_trip_through_engine():
    """The compiled filters reproduce the selected slice on the raw data."""
    spec, data = materialize(_BAR_SPEC)
    filters, _ = selection_to_filters(spec, sel([{"channel": "x", "values": ["EU"]}]))
    catalog = build_catalog_pandas(_DF)
    probe = VizSpec.model_validate({**_BAR_SPEC, "transform": [f.model_dump() for f in filters]})
    probe = normalize_spec(probe, catalog)
    out = execute_pandas(_DF, build_plan(probe))
    assert out.rows == [["EU", 190.0]]


@pytest.mark.parametrize(
    ("start", "unit", "end"),
    [
        ("2024-01-01T00:00:00", "month", "2024-02-01T00:00:00"),
        ("2024-12-01T00:00:00", "month", "2025-01-01T00:00:00"),
        ("2024-10-01T00:00:00", "quarter", "2025-01-01T00:00:00"),
        ("2024-01-01T00:00:00", "year", "2025-01-01T00:00:00"),
        ("2024-02-26T00:00:00", "week", "2024-03-04T00:00:00"),
        ("2024-02-28T00:00:00", "day", "2024-02-29T00:00:00"),
        ("2024-02-28T23:00:00", "hour", "2024-02-29T00:00:00"),
        ("2024-02-28T09:00:00", "hour", "2024-02-28T10:00:00"),
    ],
)
def test_bucket_end(start, unit, end):
    assert _bucket_end(start, unit) == end


# ---------------------------------------------------------------------------
# match_rows + selection_stats
# ---------------------------------------------------------------------------


def test_match_rows_and_stats():
    _, data = materialize(_BAR_SPEC)
    # rows sorted by sum desc: NA 290, EU 190, APAC 20
    rows = match_rows(data, sel([{"channel": "x", "values": ["EU", "APAC"]}]))
    assert [data.rows[i][0] for i in rows] == ["EU", "APAC"]
    stats = selection_stats(data, rows)
    assert stats["selected_points"] == 2 and stats["total_points"] == 3
    m = stats["measures"][0]
    assert m["selected_sum"] == 210.0
    assert m["selected_share"] == pytest.approx(210.0 / 500.0, abs=1e-4)


def test_match_rows_range():
    spec_dict = {
        "mark": {"type": "scatter"},
        "encoding": {
            "x": {"field": "revenue", "type": "quantitative"},
            "y": {"field": "revenue", "type": "quantitative"},
        },
    }
    _, data = materialize(spec_dict)
    rows = match_rows(data, sel([{"channel": "x", "range": {"min": 50, "max": 150}}]))
    assert sorted(data.rows[i][0] for i in rows) == [80.0, 100.0]


# ---------------------------------------------------------------------------
# ask_selection plumbing (stubbed LLM, pandas engine, no persistence)
# ---------------------------------------------------------------------------


class _FakeDataContext:
    def __init__(self, df):
        self.file_id = "file-1"
        self.filename = "sales.csv"
        self.raw_dataframe = df
        self.working_dataframe = df.copy()
        self.uploaded_at = datetime.utcnow()
        self.metadata: dict = {}

    def is_sql_datasource(self) -> bool:
        return False

    def get_sql_target(self):
        return (None, None)


class _FakeState:
    def __init__(self, df):
        self.session_id = "s1"
        self.data_context = _FakeDataContext(df)
        self.metadata: dict = {}


class _StubGraphAgent:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    async def generate_response(self, messages, temperature=0.2):
        self.prompts.append(messages)
        return json.dumps(self.payload)


async def test_ask_selection_end_to_end_stubbed():
    from viz.inquiry import ask_selection

    agent = _StubGraphAgent(
        {"headline": "EU dominates web", "insight": "EU is 42% of revenue.", "drill_spec": None}
    )
    result = await ask_selection(
        _FakeState(_DF),
        _BAR_SPEC,
        {"items": [{"channel": "x", "values": ["EU"]}]},
        "explain",
        None,
        graph_agent=agent,
        sql_agent=None,
        user_id=None,  # no persistence path in unit tests
    )
    assert result["headline"] == "EU dominates web"
    assert result["stats"]["selected_points"] == 1
    assert result["drill"] is None
    # driver analysis ran over the unused nominal field 'channel'
    assert any(d["field"] == "channel" for d in result["drivers"])
    # the LLM saw the deterministic evidence
    prompt = agent.prompts[0][1]["content"]
    assert "driver_analysis" in prompt and "region in [EU]" in prompt


async def test_ask_selection_rejects_measure_selection():
    from viz.executor import SpecValidationError
    from viz.inquiry import ask_selection

    with pytest.raises(SpecValidationError):
        await ask_selection(
            _FakeState(_DF),
            _BAR_SPEC,
            {"items": [{"channel": "y", "values": [100]}]},
            "explain",
            None,
            graph_agent=_StubGraphAgent({}),
            sql_agent=None,
            user_id=None,
        )
