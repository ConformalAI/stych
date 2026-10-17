"""FilterAgent toolset units + a scripted end-to-end engine smoke test."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.filter.agent import FilterAgent
from agents.filter.toolset import FilterToolset
from agents.state import AgentState, DataContext
from database.connection import Base
from llm.models import LLMResponse

SPEC = {
    "name": "negatives",
    "select": {"op": "lt", "column": "balance", "value": 0},
    "present": {"rules": [{"id": "neg", "severity": "danger"}]},
}


class _FakeWorkspaceManager:
    def __init__(self, root):
        self.root = root

    def get_or_create(self, session_id, user_id=None):
        return self.root


class _StubEmitter:
    def __init__(self):
        self.events = []
        self.parts = []

    async def emit(self, agent, message, event_type="thinking", **extra):
        self.events.append({"agent": agent, "message": message, "type": event_type, **extra})

    async def emit_part(self, part):
        self.parts.append(part)


def _state():
    raw = pd.DataFrame(
        {
            "id": range(4),
            "balance": [-5.0, 10.0, -1.0, 3.0],
            "name": ["Acme Corp", "Acme Corp", "Beta LLC", "Solo"],
        }
    )
    st = AgentState(session_id="sess-filter")
    st.metadata["user_id"] = "user-1"
    st.set_data_context(
        DataContext(
            file_id="f1",
            filename="d.csv",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    return st


@pytest.fixture()
def toolset(tmp_path, monkeypatch):
    import agents.filter.toolset as ts_mod
    import api.preview_service as ps
    from workspace.runner import WorkspaceSandboxRunner

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        ts_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )

    fake_ws = _FakeWorkspaceManager(tmp_path)
    monkeypatch.setattr(ps, "WorkspaceManager", lambda: fake_ws)

    emitter = _StubEmitter()
    ts = FilterToolset(
        _state(),
        "sess-filter",
        workspace_manager=fake_ws,
        workspace_runner=WorkspaceSandboxRunner(timeout_seconds=60),
        progress_emitter=emitter,
    )
    ts._emitter_stub = emitter
    return ts


async def test_get_schema_and_values(toolset):
    schema = json.loads(await toolset.call("get_schema", {}))
    assert schema["rows"] == 4
    names = [c["name"] for c in schema["columns"]]
    assert names == ["id", "balance", "name"]

    values = json.loads(await toolset.call("column_values", {"column": "name"}))
    assert values["values"][0] == {"value": "Acme Corp", "count": 2}

    rows = json.loads(await toolset.call("sample_rows", {"n": 2, "columns": ["balance"]}))
    assert rows["rows"] == [{"balance": -5.0}, {"balance": 10.0}]

    missing = json.loads(await toolset.call("column_values", {"column": "nope"}))
    assert "error" in missing


async def test_live_source_inspection_uses_adapter_not_empty_placeholder(toolset, monkeypatch):
    from datasources import adapters as table_adapters
    from datasources.adapters.base import AdapterColumn, CountResult

    class LiveAdapter:
        supports_stats = False

        @contextmanager
        def bound_session(self):
            yield

        def schema(self):
            return [
                AdapterColumn("id", "VARCHAR", "string"),
                AdapterColumn("item", "VARCHAR", "string"),
            ]

        def count(self, filters=None, search=None):
            return CountResult(3)

        def fetch_sample(self, columns=None, limit=10_000):
            frame = pd.DataFrame({"id": ["1", "2", "3"], "item": ["Coffee", "Tea", "Coffee"]})
            return frame[(columns or list(frame.columns))].head(limit)

    ctx = toolset.state.data_context
    ctx.metadata.update(
        {
            "source": "redshift",
            "storage_mode": "remote_reference",
            "source_columns": [
                {"name": "id", "db_type": "VARCHAR", "kind": "string"},
                {"name": "item", "db_type": "VARCHAR", "kind": "string"},
            ],
            "source_row_count": 3,
        }
    )
    ctx.raw_dataframe = pd.DataFrame(columns=["id", "item"])
    ctx.working_dataframe = pd.DataFrame(columns=["id", "item"])
    monkeypatch.setattr(table_adapters, "resolve_adapter", lambda _ctx, _variant: LiveAdapter())

    schema = json.loads(await toolset.call("get_schema", {}))
    assert schema["rows"] == 3
    assert schema["freshness"] == "live"
    assert schema["columns"][1]["examples"] == ["Coffee", "Tea", "Coffee"]

    sample = json.loads(await toolset.call("sample_rows", {"n": 2, "columns": ["item"]}))
    assert sample["rows"] == [{"item": "Coffee"}, {"item": "Tea"}]

    values = json.loads(await toolset.call("column_values", {"column": "item"}))
    assert values["scope"] == "full"
    assert values["values"][0] == {"value": "Coffee", "count": 2}

    inspect = json.loads(await toolset.call("inspect_data", {"code": "print('x')"}))
    assert "unavailable for live database sources" in inspect["error"]


def test_sql_filter_artifact_binds_to_active_working_copy(toolset):
    ctx = toolset.state.data_context
    ctx.metadata.update(
        {
            "source": "snowflake",
            "temp_schema": "STYCH_TEMP",
            "temp_table": "working_1",
        }
    )
    ctx.is_sql_datasource = lambda: True

    source, params, dataset_ref = toolset.resolve_source()

    assert source == "sql"
    assert params == {"source_id": "f1::temp"}
    assert dataset_ref == "f1::temp"


def test_sql_filter_artifact_uses_named_working_copy_identity(toolset):
    ctx = toolset.state.data_context
    ctx.metadata.update(
        {
            "source": "databricks",
            "scratch_schema": "stych_temp",
            "temp_schema": "stych_temp",
            "temp_table": "working_1",
            "working_copies": [
                {
                    "name": "databricks_space_columns",
                    "table": "working_1",
                    "parent": "source",
                    "role": "working",
                }
            ],
            "active_copy": "databricks_space_columns",
        }
    )
    ctx.is_sql_datasource = lambda: True

    source, params, dataset_ref = toolset.resolve_source()

    expected = "f1::copy:databricks_space_columns"
    assert source == "sql"
    assert params == {"source_id": expected}
    assert dataset_ref == expected


async def test_select_dataset_switches_target(toolset):
    second = pd.DataFrame({"id": [1, 2], "amount": [100.0, 200.0]})
    toolset.state.add_data_context(
        DataContext(
            file_id="f2",
            filename="second.csv",
            raw_dataframe=second.copy(deep=True),
            working_dataframe=second.copy(deep=True),
            uploaded_at=datetime.now(),
        )
    )
    block = toolset.sources_block()
    assert "dataset:second.csv" in block
    assert "d.csv" in block or "d" in block  # original still listed

    out = json.loads(await toolset.call("select_dataset", {"id": "dataset:second.csv"}))
    assert out["selected"] == "dataset:second.csv"
    assert out["rows"] == 2
    assert sorted(out["columns"]) == ["amount", "id"]

    # _df() and resolve_source() must now agree — the divergence this closes.
    assert list(toolset.state.data_context.working_dataframe.columns) == ["id", "amount"]
    assert toolset.state.metadata["active_dataset"] == "second"
    _source, _params, dataset_ref = toolset.resolve_source()
    assert dataset_ref == "second.parquet"

    schema = json.loads(await toolset.call("get_schema", {}))
    assert schema["rows"] == 2
    assert [c["name"] for c in schema["columns"]] == ["id", "amount"]


async def test_select_dataset_unknown_id(toolset):
    out = json.loads(await toolset.call("select_dataset", {"id": "dataset:nope.csv"}))
    assert "error" in out


async def test_run_analyzer_duplicate_values(toolset):
    out = json.loads(
        await toolset.call(
            "run_analyzer", {"kind": "duplicate_values", "id": "d", "columns": ["name"]}
        )
    )
    assert out["statuses"][0]["status"] == "ok"
    assert out["rows_annotated"] == 2
    assert out["outputs"]["d_count"]["non_null"] == 2
    assert len(out["sample"]) == 2


async def test_run_analyzer_invalid_step(toolset):
    out = json.loads(await toolset.call("run_analyzer", {"kind": "bogus", "id": "x"}))
    assert out["validation_errors"]


async def test_inspect_data_prints_and_is_scratch_confined(toolset, tmp_path):
    code = (
        "import pandas as pd\n"
        "df = pd.read_parquet('filter_input.parquet')\n"
        "print(int((df['balance'] < 0).sum()))\n"
    )
    out = json.loads(await toolset.call("inspect_data", {"code": code}))
    assert out["stdout"] == "2"
    # the scratch copy lives under the workspace, real dataset files untouched
    assert (tmp_path / "filter_scratch" / "filter_input.parquet").is_file()


async def test_preview_filter_counts(toolset):
    out = json.loads(await toolset.call("preview_filter", {"spec": SPEC}))
    assert out["summary"]["matched_rows"] == 2
    assert len(out["matching_sample"]) == 2


async def test_save_update_and_list(toolset):
    saved = json.loads(await toolset.call("save_filter", {"spec": SPEC}))
    fid = saved["saved"]["filter_id"]
    assert saved["saved"]["version"] == 1

    events = toolset._emitter_stub.events
    assert events and events[-1]["type"] == "filter" and events[-1]["action"] == "created"
    assert events[-1]["filter_id"] == fid

    updated = json.loads(
        await toolset.call("update_filter", {"filter_id": fid, "spec": {**SPEC, "name": "v2"}})
    )
    assert updated["saved"]["version"] == 2

    listed = json.loads(await toolset.call("list_filters", {}))
    assert [f["id"] for f in listed["filters"]] == [fid]
    assert toolset.saved[0]["action"] == "created" and toolset.saved[1]["action"] == "updated"

    missing = json.loads(await toolset.call("update_filter", {"filter_id": "nope", "spec": SPEC}))
    assert "error" in missing


async def test_unknown_tool_and_bad_args(toolset):
    # Unknown tools are rejected by the Toolkit before any handler runs.
    assert (await toolset.call("bogus_tool", {})).startswith("ERROR: Unknown tool")
    assert "error" in json.loads(await toolset.call("inspect_data", {"code": "  "}))
    # Missing required args never reach the handler — structured validation error.
    bad = json.loads(await toolset.call("column_values", {}))
    assert bad["validation_errors"][0]["path"] == "column"


# ------------------------------------------------------------------ engine smoke


class _ScriptedRouter:
    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    async def generate(self, request):
        self.requests.append([m.model_dump(exclude_none=True) for m in request.messages])
        return self._script.pop(0)


def _tool(name, args, call_id="c1"):
    return LLMResponse(
        content="",
        model="fake",
        metadata={"tool_calls": [{"id": call_id, "name": name, "arguments": args}]},
    )


def _final(text):
    return LLMResponse(content=text, model="fake", metadata={})


async def test_agent_end_to_end_scripted(tmp_path, monkeypatch):
    import agents.filter.toolset as ts_mod
    import api.preview_service as ps

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        ts_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    fake_ws = _FakeWorkspaceManager(tmp_path)
    monkeypatch.setattr(ps, "WorkspaceManager", lambda: fake_ws)

    script = [
        _tool("get_schema", {}, call_id="s1"),
        _tool("preview_filter", {"spec": SPEC}, call_id="p1"),
        _tool("save_filter", {"spec": SPEC}, call_id="f1"),
        _final("Saved the negatives filter — 2 of 4 rows match; see the Data tab."),
    ]
    agent = FilterAgent(_ScriptedRouter(script), config={"filter": {"react_max_steps": 6}})
    agent.workspace_manager = fake_ws
    emitter = _StubEmitter()
    state = _state()

    result = await agent.execute(state, "flag negative balances", progress_emitter=emitter)

    assert result["status"] == "completed"
    assert "negatives" in result["response"]
    assert len(result["metadata"]["filters"]) == 1
    filter_events = [e for e in emitter.events if e["type"] == "filter"]
    assert filter_events and filter_events[0]["action"] == "created"
    # ReAct steps were traced as parts
    assert emitter.parts
