"""MongoDB cleaning uses native document operations, never the SQL tool path."""

import importlib
from types import SimpleNamespace

import pytest

# Initialize the agents package before importing the shared tool registry; the
# package's public __init__ wires the agents and tool modules together.
from agents.state import AgentState  # noqa: F401
from tools.cleaning_tools import CleaningReactToolRegistry

cleaning_tools = importlib.import_module("tools.cleaning_tools")


class _Runner:
    def __init__(self):
        self.pipeline_call = None
        self.update_call = None
        self.delete_call = None
        self.dedup_call = None

    def run_pipeline(self, collection, pipeline, **kwargs):
        self.pipeline_call = (collection, pipeline, kwargs)
        return [{"_id": 1, "item": "Tea"}]

    def update_many(self, collection, filter_doc, update):
        self.update_call = (collection, filter_doc, update)
        return 4

    def delete_many(self, collection, filter_doc):
        self.delete_call = (collection, filter_doc)
        return 2

    def deduplicate(self, collection, fields):
        self.dedup_call = (collection, fields)
        return 3


class _Agent:
    def __init__(self, runner):
        self.runner = runner

    async def ensure_temp_table(self, state):
        return "stych_temp", "stych_working"

    def _runner_for(self, state):
        return self.runner


def _state():
    return SimpleNamespace(
        data_context=SimpleNamespace(metadata={"source": "mongodb"}, working_dataframe=None),
        metadata={},
    )


def _registry(runner):
    return CleaningReactToolRegistry(
        sql_agent=_Agent(runner),
        workspace_manager=None,
        workspace_runner=None,
        is_mongo_available=True,
    )


def test_mongo_mode_exposes_only_native_and_working_copy_tools():
    names = _registry(_Runner()).available_tools
    assert {"mongo_read", "mongo_update", "mongo_delete", "mongo_deduplicate"} <= names
    assert "sql_read" not in names
    assert "run_script" not in names


@pytest.mark.asyncio
async def test_mongo_working_copy_tools_are_not_rejected_as_sql_tools(monkeypatch):
    async def fake_execute(_agent, _state, tool_name, _tool_input):
        return f"executed:{tool_name}"

    monkeypatch.setattr(cleaning_tools, "execute_working_copy_tool", fake_execute)

    out = await _registry(_Runner()).call("list_working_copies", {}, _state())

    assert out == "executed:list_working_copies"


@pytest.mark.asyncio
async def test_mongo_read_is_bounded_read_only_and_collection_scoped():
    runner = _Runner()
    result = await _registry(runner).call(
        "mongo_read", {"pipeline": '[{"$match": {"item": "Tea"}}]'}, _state()
    )
    collection, pipeline, kwargs = runner.pipeline_call
    assert collection == "stych_working"
    assert pipeline[-1] == {"$limit": 21}
    assert kwargs["database"] == "stych_temp"
    assert kwargs["allow_other_collections"] is False
    assert kwargs["allow_writes"] is False
    assert "Tea" in result


@pytest.mark.asyncio
async def test_mongo_mutations_target_working_copy_and_bump_generation():
    runner = _Runner()
    state = _state()
    registry = _registry(runner)

    update = await registry.call(
        "mongo_update",
        {"filter": '{"item": "Tea"}', "update": '{"$set": {"item": "tea"}}'},
        state,
    )
    delete = await registry.call("mongo_delete", {"filter": '{"bad": true}'}, state)
    dedup = await registry.call("mongo_deduplicate", {"fields": ["item", "price"]}, state)

    assert runner.update_call == (
        "stych_working",
        {"item": "Tea"},
        {"$set": {"item": "tea"}},
    )
    assert runner.delete_call == ("stych_working", {"bad": True})
    assert runner.dedup_call == ("stych_working", ["item", "price"])
    assert state.data_context.metadata["temp_table_generation"] == 3
    assert state.data_context.metadata["schema_sample_documents"] == 1_000
    assert list(state.data_context.working_dataframe.columns) == ["_id", "item"]
    assert "modified 4" in update
    assert "removed 2" in delete
    assert "removed 3" in dedup
