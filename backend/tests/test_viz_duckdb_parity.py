"""Differential parity checks for pandas semantics and DuckDB execution."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from agents.state import AgentState, DataContext
from execution.duckdb import duckdb_kernel_manager
from viz.catalog import build_catalog_pandas, get_field_catalog
from viz.compile_pandas import execute_pandas
from viz.executor import run_spec
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec


class _WorkspaceManager:
    root = None

    def get_or_create(self, session_id, user_id=None):
        path = type(self).root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


@pytest.fixture()
def viz_state(tmp_path, monkeypatch):
    import execution.session_plane as session_plane

    _WorkspaceManager.root = tmp_path
    monkeypatch.setattr(session_plane, "WorkspaceManager", _WorkspaceManager)
    frame = pd.DataFrame(
        {
            "ordered_at": pd.to_datetime(
                ["2024-01-05", "2024-01-20", "2024-02-10", "2024-02-15", "2024-03-01"]
            ),
            "region": ["EU", "NA", "EU", "APAC", "NA"],
            "channel": ["web", "web", "store", "web", "store"],
            "revenue": [100.0, 250.0, 80.0, None, 40.0],
        }
    )
    state = AgentState(session_id="viz-duckdb", metadata={"user_id": "test-user"})
    state.set_data_context(
        DataContext(
            file_id="sales-1",
            filename="sales.csv",
            raw_dataframe=frame.copy(deep=True),
            working_dataframe=frame.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    yield state, frame
    duckdb_kernel_manager.close_session(state.session_id)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {
                    "field": "region",
                    "type": "nominal",
                    "sort": {"encoding": "y", "order": "desc"},
                },
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
            "transform": [{"op": "filter", "field": "channel", "predicate": "neq", "value": "x"}],
        },
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "ordered_at", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "mean"},
            },
        },
        {
            "mark": {"type": "heatmap"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "channel", "type": "nominal"},
                "color": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    ],
)
async def test_duckdb_run_spec_matches_reference_pandas_engine(viz_state, payload):
    state, frame = viz_state
    raw_spec = VizSpec.model_validate(payload)
    reference_catalog = build_catalog_pandas(frame)
    normalized = normalize_spec(raw_spec, reference_catalog)
    expected = execute_pandas(frame, build_plan(normalized))

    actual = await run_spec(state, raw_spec)

    assert actual.columns == expected.columns
    assert actual.rows == expected.rows
    assert actual.meta["engine"] == "duckdb"
    assert actual.meta["scope"] == "full"


async def test_session_catalog_exposes_snapshot_capability_metadata(viz_state):
    state, _frame = viz_state
    catalog = await get_field_catalog(state)
    assert catalog.engine == "duckdb"
    assert catalog.source == "sales-1"
    assert catalog.scope == "full"
    assert catalog.freshness == "snapshot"
    assert catalog.truncated is False


async def test_duckdb_numeric_types_preserve_scientific_notation(viz_state):
    state, _frame = viz_state
    frame = pd.DataFrame({"group": ["all", "all"], "metric": [1e20, 2e20]})
    state.data_context.working_dataframe = frame
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "group", "type": "nominal"},
                "y": {"field": "metric", "type": "quantitative", "aggregate": "sum"},
            },
        }
    )
    result = await run_spec(state, spec)
    assert result.rows == [["all", 3e20]]
