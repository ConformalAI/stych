"""Mongo analysis must not fall through to file-workspace tools."""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from agents.analysis.agent import STATUS_ERROR, DataAnalysisAgent
from agents.mongo_agent.runner import MongoRunnerError
from agents.state import AgentState, DataContext


def _mongo_state(**meta_extra):
    raw = pd.DataFrame({"_id": ["a"]})
    st = AgentState(session_id="mongo-sess")
    st.set_data_context(
        DataContext(
            file_id="f1",
            filename="orders",
            raw_dataframe=raw.copy(deep=True),
            working_dataframe=raw.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "mongodb",
                "connection_url": "mongodb://localhost:27017/analytics",
                "database": "analytics",
                "collection": "orders",
                **meta_extra,
            },
        )
    )
    return st


class _NoLLM:
    async def generate(self, request):
        raise AssertionError("LLM must not run when MongoDB context fails")


@pytest.mark.asyncio
async def test_mongo_runner_error_does_not_use_file_workspace_tools(monkeypatch):
    class Boom:
        @classmethod
        def from_meta(cls, _meta):
            raise MongoRunnerError("cluster unreachable")

    monkeypatch.setattr("agents.mongo_agent.MongoScratchRunner", Boom)

    agent = DataAnalysisAgent(_NoLLM(), config={"agents": {"use_unified_engine": True}})
    agent._unified_loop_setup = Mock(
        side_effect=AssertionError("must not start file-workspace loop")
    )
    agent._build_duckdb_session_context = AsyncMock(
        side_effect=AssertionError("must not fall through to DuckDB")
    )

    out = await agent.process_query(state=_mongo_state(), message="Count documents")
    assert out["status"] == STATUS_ERROR
    assert "MongoDB" in out["response"]
    assert "cluster unreachable" in out["response"]


@pytest.mark.asyncio
async def test_mongo_missing_collection_does_not_use_file_workspace_tools():
    agent = DataAnalysisAgent(_NoLLM(), config={"agents": {"use_unified_engine": True}})
    agent._unified_loop_setup = Mock(
        side_effect=AssertionError("must not start file-workspace loop")
    )

    state = _mongo_state()
    state.data_context.metadata.pop("collection")
    out = await agent.process_query(state=state, message="Count documents")
    assert out["status"] == STATUS_ERROR
    assert "collection" in out["response"].lower()
