"""Tests for the lean frontier workspace model: real dataset identity, directory-wide
artifact tracking, and explicit dataset promotion.

Covers the fixes for the reported bug where an agent-written file (e.g.
``titanic_engineered.csv``) was silently dropped and the agent saw ``data.csv`` while
the user saw ``titanic.csv``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from tools.workspace_toolset import WorkspaceToolset
from workspace.manager import (
    CANONICAL_DATA_FILE,
    dataset_parquet_name,
    dataset_stem,
    list_workspace_artifacts,
    resolve_active_dataset,
    seed_workspace_data,
)
from workspace.runner import WorkspaceSandboxRunner


def _toolset() -> WorkspaceToolset:
    return WorkspaceToolset(WorkspaceSandboxRunner(timeout_seconds=20))


class _DC:
    def __init__(self, df: pd.DataFrame, filename: str = "titanic.csv"):
        self.working_dataframe = df
        self.filename = filename

    def is_sql_datasource(self) -> bool:
        return False


class _State:
    def __init__(self, df: pd.DataFrame, filename: str = "titanic.csv", active: str | None = None):
        self.data_context = _DC(df, filename)
        self.metadata: dict = {}
        if active is not None:
            self.metadata["active_dataset"] = active
        self.session_id = "s1"


def _run(toolset: WorkspaceToolset, root: Path, code: str, state=None) -> dict:
    return json.loads(toolset.run_script(root, code, state))


# --------------------------------------------------------------------------- identity


def test_dataset_stem_sanitizes_and_defaults():
    assert dataset_stem("titanic.csv") == "titanic"
    assert dataset_stem("Q3 Sales (final).xlsx") == "Q3_Sales_final"
    assert dataset_stem(None) == "data"
    assert dataset_stem("") == "data"
    assert dataset_parquet_name("titanic.csv") == "titanic.parquet"
    assert dataset_parquet_name("Q3 Sales (final).xlsx") == "Q3_Sales_final.parquet"


def test_seed_uses_real_filename():
    df = pd.DataFrame({"x": [1, 2]})
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    assert (root / "titanic.parquet").is_file()
    assert not (root / "titanic.csv").exists()
    # generic alias is NOT created when a real name is given
    assert not (root / CANONICAL_DATA_FILE).exists()


def test_seed_without_filename_keeps_canonical_default():
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, pd.DataFrame({"x": [1]}))
    assert (root / CANONICAL_DATA_FILE).is_file()
    assert not (root / "data.csv").exists()


def test_resolve_active_dataset_csv_only_signals_missing_parquet():
    """A retrieved dataset arrives CSV-only (no parquet). The resolver still identifies
    it, and the missing-parquet path is what the seed gate keys on."""
    root = Path(tempfile.mkdtemp())
    pd.DataFrame({"x": [1, 2]}).to_csv(root / "titanic.csv", index=False)
    state = _State(pd.DataFrame({"x": [1, 2]}), filename="titanic.csv", active="titanic")
    parquet, csv, stem = resolve_active_dataset(root, state)
    assert stem == "titanic"
    assert csv.is_file()
    assert not parquet.is_file()  # seed gate (`not active[0].is_file()`) fires → creates it


def test_resolve_active_dataset_precedence():
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, pd.DataFrame({"x": [1]}), "titanic.csv")
    # explicit pointer wins
    st = _State(pd.DataFrame({"x": [1]}), active="titanic")
    parquet, csv, stem = resolve_active_dataset(root, st)
    assert stem == "titanic" and parquet.name == "titanic.parquet" and csv.name == "titanic.csv"
    # no pointer → discovers the on-disk dataset file
    parquet2, _csv2, stem2 = resolve_active_dataset(root, None)
    assert stem2 == "titanic" and parquet2.is_file()


# ------------------------------------------------------------------ artifact tracking


def test_new_file_is_captured_in_artifact_manifest():
    df = pd.DataFrame({"qty": [1, 2, 3]})
    ts, root = _toolset(), Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    state = _State(df, active="titanic")
    out = _run(
        ts,
        root,
        "import pandas as pd\n"
        "df = pd.read_parquet('titanic.parquet')\n"
        "df['big'] = df['qty'] * 10\n"
        "df.to_csv('titanic_engineered.csv', index=False)\n"
        "open('REPORT.md', 'w').write('# notes')\n"
        "print('done')",
        state,
    )
    assert out["success"], out
    names = {a["name"] for a in state.metadata.get("workspace_artifacts", [])}
    # the engineered file and the report are visible (no longer silently dropped)
    assert "titanic_engineered.csv" in names
    assert "REPORT.md" in names
    # internal scratch files stay hidden
    assert "sandbox_exec.py" not in names
    assert "run_artifact.json" not in names


def test_overwriting_active_named_dataset_syncs_working_dataframe():
    df = pd.DataFrame({"qty": [1, 2, 3]})
    ts, root = _toolset(), Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    state = _State(df, active="titanic")
    out = _run(
        ts,
        root,
        "import pandas as pd\n"
        "df = pd.read_parquet('titanic.parquet')\n"
        "df['doubled'] = df['qty'] * 2\n"
        "df.to_parquet('titanic.parquet', index=False)\n"
        "print('ok')",
        state,
    )
    assert out["success"], out
    assert "doubled" in list(state.data_context.working_dataframe.columns)
    assert state.metadata.get("workspace_canonical_file", "").endswith("titanic.parquet")


def test_writing_a_non_active_file_does_not_swap_working_dataframe():
    """A new dataset file is an artifact, not automatically the working set."""
    df = pd.DataFrame({"qty": [1, 2, 3]})
    ts, root = _toolset(), Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    state = _State(df, active="titanic")
    _run(
        ts,
        root,
        "import pandas as pd\n"
        "df = pd.read_parquet('titanic.parquet')\n"
        "df['eng'] = df['qty'] + 1\n"
        "df.to_parquet('titanic_engineered.parquet', index=False)\n",
        state,
    )
    # working dataframe is unchanged (still the active 'titanic', no 'eng' column)
    assert "eng" not in list(state.data_context.working_dataframe.columns)


# --------------------------------------------------------------------- promotion tool


def test_set_active_dataset_promotes_new_file():
    df = pd.DataFrame({"qty": [1, 2, 3]})
    ts, root = _toolset(), Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    state = _State(df, active="titanic")
    # create an engineered dataset
    _run(
        ts,
        root,
        "import pandas as pd\n"
        "df = pd.read_parquet('titanic.parquet')\n"
        "df['eng'] = df['qty'] + 1\n"
        "df.to_parquet('titanic_engineered.parquet', index=False)\n",
        state,
    )
    msg = ts.set_active_dataset(root, "titanic_engineered.parquet", state)
    assert "Active dataset is now" in msg
    assert state.metadata["active_dataset"] == "titanic_engineered"
    assert "eng" in list(state.data_context.working_dataframe.columns)
    # parquet is the stored table; no csv twin is written on promote
    assert not (root / "titanic_engineered.csv").exists()


def test_set_active_dataset_rejects_non_dataset_file():
    df = pd.DataFrame({"qty": [1]})
    ts, root = _toolset(), Path(tempfile.mkdtemp())
    seed_workspace_data(root, df, "titanic.csv")
    state = _State(df, active="titanic")
    ts.write_file(root, {"path": "notes.md", "content": "hi"})
    msg = ts.set_active_dataset(root, "notes.md", state)
    assert "ERROR" in msg
    assert state.metadata["active_dataset"] == "titanic"  # unchanged


def test_list_workspace_artifacts_hides_internal_files():
    root = Path(tempfile.mkdtemp())
    seed_workspace_data(root, pd.DataFrame({"x": [1]}), "titanic.csv")
    (root / "sandbox_exec.py").write_text("x = 1")
    (root / "run_artifact.json").write_text("{}")
    names = {a["name"] for a in list_workspace_artifacts(root)}
    assert "titanic.parquet" in names
    assert "titanic.csv" not in names
    assert "sandbox_exec.py" not in names and "run_artifact.json" not in names
