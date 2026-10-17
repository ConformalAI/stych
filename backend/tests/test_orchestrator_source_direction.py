"""Phase 2: the orchestrator's call_agent `input` deterministically re-points
the active source a sub-agent operates on (workspace/sources + set_active_dataset).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from agents.orchestrator.orchestrator_react import _activate_named_source
from workspace.manager import seed_workspace_data


class _DC:
    def __init__(self, df):
        self.working_dataframe = df

    def is_sql_datasource(self):
        return False


class _State:
    def __init__(self, df, active):
        self.data_context = _DC(df)
        self.metadata = {"active_dataset": active}
        self.session_id = "s1"


def _workspace(monkeypatch):
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, pd.DataFrame({"a": [1, 2]}), "data.csv")  # active: data
    pd.DataFrame({"z": [9, 9, 9]}).to_csv(root / "engineered.csv", index=False)
    from workspace.manager import WorkspaceManager

    monkeypatch.setattr(WorkspaceManager, "get_or_create", lambda self, sid, user_id=None: root)
    return root


def test_named_file_becomes_active(monkeypatch):
    _workspace(monkeypatch)
    state = _State(pd.DataFrame({"a": [1, 2]}), active="data")
    _activate_named_source(state, "s1", "engineered.csv")
    assert state.metadata["active_dataset"] == "engineered"
    assert list(state.data_context.working_dataframe.columns) == ["z"]
    assert len(state.data_context.working_dataframe) == 3


def test_sql_handle_is_left_alone(monkeypatch):
    _workspace(monkeypatch)
    original = pd.DataFrame({"a": [1, 2]})
    state = _State(original, active="data")
    _activate_named_source(state, "s1", "sql:t_orders")
    assert state.metadata["active_dataset"] == "data"
    assert state.data_context.working_dataframe is original


def test_already_active_is_noop(monkeypatch):
    _workspace(monkeypatch)
    original = pd.DataFrame({"z": [9, 9, 9]})
    state = _State(original, active="engineered")
    _activate_named_source(state, "s1", "engineered.csv")
    # Guard short-circuits before any reload — same object, untouched.
    assert state.data_context.working_dataframe is original


def test_bad_id_does_not_raise(monkeypatch):
    _workspace(monkeypatch)
    original = pd.DataFrame({"a": [1, 2]})
    state = _State(original, active="data")
    _activate_named_source(state, "s1", "does_not_exist.csv")
    # Best-effort: the loop must survive; active dataset unchanged.
    assert state.metadata["active_dataset"] == "data"
    assert state.data_context.working_dataframe is original
