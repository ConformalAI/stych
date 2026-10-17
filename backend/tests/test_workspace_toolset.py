"""Tests for the shared, sandboxed WorkspaceToolset and the canonical data file.

Covers the architectural fixes from the analysis-sandbox rework:
- imports of any installed library work inside run_script (no more __import__ stripping);
- scripts re-load data.parquet each call and original columns survive across steps
  (no more `result`-as-df clobber / KeyError cascade);
- dtypes survive the parquet seed→read round-trip;
- a mutating script syncs working_dataframe via the canonical data file;
- safeguards: blocked imports, path traversal, network denial.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from tools.workspace_toolset import WorkspaceToolset
from workspace.manager import CANONICAL_DATA_FILE, seed_workspace_data
from workspace.runner import WorkspaceSandboxRunner


def _toolset() -> WorkspaceToolset:
    return WorkspaceToolset(WorkspaceSandboxRunner(timeout_seconds=20))


def _seeded(df: pd.DataFrame) -> Path:
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, df)
    return root


class _DC:
    def __init__(self, df: pd.DataFrame, source: str = "upload"):
        self.working_dataframe = df
        self.filename = "data.parquet"
        self.file_id = "f1"
        self.metadata = {"source": source}

    def is_sql_datasource(self) -> bool:
        return False


class _State:
    def __init__(self, df: pd.DataFrame):
        self.data_context = _DC(df)
        self.metadata: dict = {}
        self.session_id = "s1"


def _run(toolset: WorkspaceToolset, root: Path, code: str, state=None) -> dict:
    return json.loads(toolset.run_script(root, code, state))


def test_seed_creates_canonical_files_and_preserves_dtypes():
    df = pd.DataFrame(
        {"price": [1.5, 2.0], "qty": [2, 3], "when": pd.to_datetime(["2024-01-01", "2024-02-01"])}
    )
    root = _seeded(df)
    assert (root / CANONICAL_DATA_FILE).is_file()
    assert not (root / "data.csv").exists()
    back = pd.read_parquet(root / CANONICAL_DATA_FILE)
    # dtypes survive the round-trip (CSV would have lost the datetime / int types)
    assert str(back["qty"].dtype).startswith("int")
    assert str(back["when"].dtype).startswith("datetime")


def test_run_script_imports_and_cross_step_columns_survive():
    # Reproduces the original failure: idiomatic imports + referencing original
    # columns after a filter in a later step.
    df = pd.DataFrame(
        {"Price Per Unit": [1.5, None], "Total Spent": [3.0, 4.0], "Quantity": [2, 3]}
    )
    ts, root = _toolset(), _seeded(df)
    step1 = _run(
        ts, root, "import pandas as pd\ndf=pd.read_parquet('data.parquet')\nprint(len(df))"
    )
    assert step1["success"], step1
    step2 = _run(
        ts,
        root,
        "import pandas as pd\n"
        "df=pd.read_parquet('data.parquet')\n"
        "sub=df.dropna(subset=['Price Per Unit','Total Spent','Quantity'])\n"
        "print(list(sub.columns))",
    )
    assert step2["success"], step2
    assert "Price Per Unit" in step2["stdout"]


def test_run_script_allows_third_party_libraries():
    ts, root = _toolset(), _seeded(pd.DataFrame({"x": [1, 2, 3]}))
    out = _run(ts, root, "import numpy as np\nprint(int(np.array([1,2,3]).sum()))")
    assert out["success"] and "6" in out["stdout"]


def test_mutating_script_syncs_working_dataframe():
    df = pd.DataFrame({"qty": [1, 2, 3]})
    ts, root = _toolset(), _seeded(df)
    state = _State(df)
    out = _run(
        ts,
        root,
        "import pandas as pd\n"
        "df=pd.read_parquet('data.parquet')\n"
        "df['doubled']=df['qty']*2\n"
        "df.to_parquet('data.parquet', index=False)\n"
        "print('ok')",
        state,
    )
    assert out["success"], out
    assert "doubled" in list(state.data_context.working_dataframe.columns)
    assert state.metadata.get("workspace_canonical_file")


def test_blocked_import_returns_clean_error():
    ts, root = _toolset(), _seeded(pd.DataFrame({"x": [1]}))
    out = _run(ts, root, "import os\nprint(os.getcwd())")
    assert out["success"] is False
    assert "not allowed" in out["error"]


def test_network_is_disabled():
    ts, root = _toolset(), _seeded(pd.DataFrame({"x": [1]}))
    # socket is a blocked import, so this is caught at validation; either way it must fail.
    out = _run(ts, root, "import socket\nsocket.create_connection(('1.1.1.1', 80))")
    assert out["success"] is False


def test_path_traversal_blocked():
    ts, root = _toolset(), _seeded(pd.DataFrame({"x": [1]}))
    msg = ts.read_file(root, {"path": "../../etc/passwd"})
    assert "ERROR" in msg or "not allowed" in msg


def test_file_crud_roundtrip():
    ts, root = _toolset(), _seeded(pd.DataFrame({"x": [1]}))
    assert "Wrote" in ts.write_file(root, {"path": "note.txt", "content": "hello"})
    assert "Edited" in ts.edit_file(
        root, {"path": "note.txt", "old_string": "hello", "new_string": "bye"}
    )
    assert "bye" in ts.read_file(root, "note.txt")
    assert "Deleted" in ts.delete_file(root, {"path": "note.txt"})
