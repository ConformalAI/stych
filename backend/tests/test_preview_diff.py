"""Tests for the spreadsheet diff engine and endpoint.

Engine units exercise alignment and the change detectors directly; endpoint
tests mount the preview router with stubbed auth/state like test_preview_routes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import diff_service
from api.dependencies import get_current_user
from api.preview_routes import router as preview_router


def _meta_at(result, row, col_name):
    j = [c["name"] for c in result.columns].index(col_name)
    return result.kinds[row, j], result.labels[row, j]


# ------------------------------------------------------------------ alignment


def test_key_alignment_detects_added_and_removed_rows():
    base = pd.DataFrame({"id": [1, 2, 3, 4], "v": [10, 20, 30, 40]})
    target = pd.DataFrame({"id": [4, 2, 5], "v": [40, 25, 99]})  # reordered, 1&3 gone, 5 new
    r = diff_service.compute_diff(base, target)
    assert r.summary["alignment"] == "key:id"
    assert r.summary["rows_added"] == 1
    assert r.summary["rows_removed"] == 2
    # row 0 (id=4) unchanged despite reordering; row 1 (id=2) changed 20->25
    assert _meta_at(r, 0, "v") == (None, None)
    kind, _ = _meta_at(r, 1, "v")
    assert kind == "changed"
    assert _meta_at(r, 2, "v")[0] == "added"


def test_positional_alignment_without_key():
    base = pd.DataFrame({"v": [1, 1, 1]})
    target = pd.DataFrame({"v": [1, 2, 1, 9]})
    r = diff_service.compute_diff(base, target)
    assert r.summary["alignment"] == "positional"
    assert r.summary["rows_added"] == 1
    assert _meta_at(r, 1, "v")[0] == "changed"


def test_renamed_column_detected():
    base = pd.DataFrame({"id": range(6), "price": [1.0, 2, 3, 4, 5, 6]})
    target = pd.DataFrame({"id": range(6), "cost": [1.0, 2, 3, 4, 5, 6]})
    r = diff_service.compute_diff(base, target)
    assert r.summary["columns_renamed"] == {"cost": "price"}
    assert r.summary["columns_added"] == [] and r.summary["columns_removed"] == []
    spec = next(c for c in r.columns if c["name"] == "cost")
    assert spec["status"] == "renamed" and "price" in spec["note"]


def test_added_and_removed_columns():
    base = pd.DataFrame({"id": range(4), "old": [1, 2, 3, 4]})
    target = pd.DataFrame({"id": range(4), "brand_new": ["a", "b", "c", "d"]})
    r = diff_service.compute_diff(base, target)
    assert r.summary["columns_added"] == ["brand_new"]
    assert r.summary["columns_removed"] == ["old"]
    assert _meta_at(r, 0, "brand_new")[0] == "added"
    # added column alone must not flag rows as changed
    assert r.summary["rows_changed"] == 0


# ------------------------------------------------------------------ detectors


def test_mean_median_mode_zero_imputation():
    base = pd.DataFrame({"id": range(8), "x": [1.0, 3.0, None, None, None, None, 1.0, 1.0]})
    x = base["x"]
    target = base.copy()
    target.loc[2, "x"] = x.mean()  # 1.5
    target.loc[3, "x"] = x.median()  # 1.0 == mode too; mean checked first
    target.loc[4, "x"] = 0.0
    target.loc[5, "x"] = 77.0  # arbitrary fill
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 2, "x") == ("imputed", "mean imputed")
    assert _meta_at(r, 3, "x")[0] == "imputed"
    assert _meta_at(r, 4, "x") == ("imputed", "zero filled")
    assert _meta_at(r, 5, "x") == ("imputed", "imputed")


def test_missing_token_fill_classified_as_imputed():
    """Base 'missing' stored as literal tokens ("" / "UNKNOWN" / "NA"), as
    Postgres/Excel sources keep them, must classify a fill as imputed — parity
    with a NaN-backed CSV so both diff engines agree."""
    base = pd.DataFrame({"id": range(6), "x": [1.0, 3.0, "", "UNKNOWN", "n/a", 5.0]}, dtype=object)
    target = base.copy()
    target.loc[[2, 3, 4], "x"] = 7.0  # fill the three missing-token cells
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 2, "x")[0] == "imputed"
    assert _meta_at(r, 3, "x")[0] == "imputed"
    assert _meta_at(r, 4, "x")[0] == "imputed"
    assert _meta_at(r, 0, "x") == (None, None)  # untouched real value


def test_mode_fill_over_empty_strings_is_mode_imputed_not_remapped():
    """The reported bug: mode-filling empty-string 'nulls' on a categorical
    column was showing as a purple 'remapped' transform instead of imputed."""
    base = pd.DataFrame(
        {"id": range(6), "c": ["a", "", "a", "", "a", "b"]}  # "" = missing, mode is "a"
    )
    target = base.copy()
    target.loc[[1, 3], "c"] = "a"  # mode imputation
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 1, "c") == ("imputed", "mode imputed")
    assert _meta_at(r, 3, "c") == ("imputed", "mode imputed")


def test_dropped_value():
    base = pd.DataFrame({"id": range(3), "x": [1.0, 2.0, 3.0]})
    target = base.copy()
    target.loc[1, "x"] = None
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 1, "x") == ("removed", "dropped")


def test_standardized_column():
    rng = np.random.default_rng(7)
    vals = rng.normal(50, 10, 40)
    base = pd.DataFrame({"id": range(40), "x": vals})
    target = base.copy()
    target["x"] = (vals - vals.mean()) / vals.std()
    r = diff_service.compute_diff(base, target)
    spec = next(c for c in r.columns if c["name"] == "x")
    assert spec["note"] == "standardized"
    assert _meta_at(r, 0, "x") == ("transformed", "standardized")


def test_minmax_scaled_column():
    vals = np.array([10.0, 20, 30, 40, 55])
    base = pd.DataFrame({"id": range(5), "x": vals})
    target = base.copy()
    target["x"] = (vals - vals.min()) / (vals.max() - vals.min())
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "x")["note"] == "min-max scaled"


def test_log_transform_column():
    vals = np.array([1.0, 10, 100, 1000, 5.0])
    base = pd.DataFrame({"id": range(5), "x": vals})
    target = base.copy()
    target["x"] = np.log(vals)
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "x")["note"] == "log transform"


def test_constant_scale_column():
    vals = np.array([1.0, 2, 3, 4, 5])
    base = pd.DataFrame({"id": range(5), "x": vals})
    target = base.copy()
    target["x"] = vals * 2.54
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "x")["note"] == "scaled ×2.54"


def test_rounded_column():
    vals = np.array([1.234, 2.567, 3.891, 4.005, 5.499])
    base = pd.DataFrame({"id": range(5), "x": vals})
    target = base.copy()
    target["x"] = np.round(vals, 1)
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "x")["note"] == "rounded"


def test_label_encoding_detected():
    cats = ["red", "blue", "green", "red", "blue", "green", "red", "blue"]
    base = pd.DataFrame({"id": range(8), "color": cats})
    target = base.copy()
    target["color"] = [{"red": 0, "blue": 1, "green": 2}[c] for c in cats]
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "color")["note"] == "label encoded"


def test_one_hot_expansion_detected():
    base = pd.DataFrame({"id": range(4), "color": ["red", "blue", "red", "blue"]})
    target = pd.DataFrame({"id": range(4), "color_red": [1, 0, 1, 0], "color_blue": [0, 1, 0, 1]})
    r = diff_service.compute_diff(base, target)
    spec = next(c for c in r.columns if c["name"] == "color_red")
    assert spec["note"] == "one-hot of color"
    assert "color" in r.summary["columns_removed"]


def test_case_and_trim_changes():
    base = pd.DataFrame({"id": range(3), "s": ["Hello", " padded ", "same"]})
    target = pd.DataFrame({"id": range(3), "s": ["hello", "padded", "same"]})
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 0, "s") == ("transformed", "case change")
    assert _meta_at(r, 1, "s") == ("transformed", "trimmed")
    assert _meta_at(r, 2, "s") == (None, None)


def test_consistent_remap_detected():
    base = pd.DataFrame({"id": range(6), "s": ["NY", "CA", "NY", "CA", "NY", "TX"]})
    target = pd.DataFrame(
        {
            "id": range(6),
            "s": ["New York", "California", "New York", "California", "New York", "TX"],
        }
    )
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 0, "s") == ("transformed", "remapped")
    assert _meta_at(r, 5, "s") == (None, None)


def test_recast_column():
    base = pd.DataFrame({"id": range(4), "x": ["1", "2", "3", "4"]})
    target = pd.DataFrame({"id": range(4), "x": [1, 2, 3, 4]})
    r = diff_service.compute_diff(base, target)
    note = next(c for c in r.columns if c["name"] == "x")["note"]
    assert note in ("recast", "label encoded")  # both are fair readings


def test_generic_change_fallback_keeps_old_value():
    base = pd.DataFrame({"id": range(3), "s": ["a", "b", "c"]})
    target = pd.DataFrame({"id": range(3), "s": ["a", "wild", "c"]})
    r = diff_service.compute_diff(base, target)
    kind, label = _meta_at(r, 1, "s")
    assert kind == "changed" and label is None
    j = [c["name"] for c in r.columns].index("s")
    assert r.olds[1, j] == "b"


# --------------------------------------------------------- similarity warning


def test_unrelated_sources_warn():
    base = pd.DataFrame({"a": [1], "b": [2]})
    target = pd.DataFrame({"x": [1], "y": [2]})
    r = diff_service.compute_diff(base, target)
    assert r.summary["similarity"] == 0
    assert "do not appear" in r.summary["warning"]


def test_low_overlap_warns():
    base = pd.DataFrame({f"c{i}": range(6) for i in range(6)})
    target = pd.DataFrame(
        {
            "c0": range(6),
            "z1": range(6),
            "z2": range(6),
            "z3": range(6),
            "z4": range(6),
            "z5": range(6),
        }
    )
    r = diff_service.compute_diff(base, target)
    assert r.summary["warning"] is not None and "Low overlap" in r.summary["warning"]


def test_similar_sources_no_warning():
    base = pd.DataFrame({"id": range(5), "x": range(5)})
    target = base.copy()
    r = diff_service.compute_diff(base, target)
    assert r.summary["warning"] is None
    assert r.summary["cells_changed"] == 0


# -------------------------------------------------------------------- paging


def test_diff_page_changes_only():
    base = pd.DataFrame({"id": range(300), "x": range(300)})
    target = base.copy()
    target.loc[5, "x"] = -1
    target.loc[250, "x"] = -2
    r = diff_service.compute_diff(base, target)
    page = diff_service.build_diff_page(r, page=1, page_size=100, changes_only=True)
    assert page["filtered_rows"] == 2 and page["total_rows"] == 300
    assert page["rows"][0][0] == 5 and page["rows"][1][0] == 250
    meta = page["cell_meta"][0][1]
    assert meta["k"] == "changed" and meta["o"] == 5

    full = diff_service.build_diff_page(r, page=3, page_size=100)
    assert len(full["rows"]) == 100 and full["total_pages"] == 3


# ------------------------------------------------------------------ endpoint


class _FakeContext:
    def __init__(self, df, file_id="f1", filename="titanic.csv"):
        self.dataframe = df
        self.file_id = file_id
        self.filename = filename
        self.metadata = {}


class _FakeSession:
    def __init__(self, ctxs):
        self._ctxs = ctxs
        self.data_context = ctxs[0] if ctxs else None
        self.metadata = {}

    def get_all_data_contexts(self):
        return list(self._ctxs)


class _FakeStateManager:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, session_id, app_user_id=None):
        return self.sessions.get((session_id, app_user_id))


class _FakeWorkspaceManager:
    root = None

    def get_or_create(self, session_id, user_id=None):
        return type(self).root


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import api.preview_routes as pr

    base = pd.DataFrame({"id": range(10), "x": [float(i) for i in range(10)]})
    cleaned = base.copy()
    cleaned.loc[3, "x"] = 99.0
    base.to_parquet(tmp_path / "raw.parquet", index=False)
    cleaned.to_csv(tmp_path / "cleaned.csv", index=False)
    (tmp_path / "notes.txt").write_text("not tabular")

    _FakeWorkspaceManager.root = tmp_path
    fake_state = _FakeStateManager({("s1", "user-1"): _FakeSession([_FakeContext(cleaned)])})
    monkeypatch.setattr(pr, "state_manager", fake_state)
    monkeypatch.setattr(pr, "WorkspaceManager", _FakeWorkspaceManager)
    # Source resolution now lives in preview_service (shared with filters).
    import api.preview_service as ps

    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(preview_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    return TestClient(app)


def test_diff_endpoint_file_vs_file(client):
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "raw.parquet",
            "target_source": "file",
            "target_path": "cleaned.csv",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "diff"
    assert body["summary"]["cells_changed"] == 1
    assert body["cell_meta"][3][1]["k"] == "changed"
    assert body["cell_meta"][3][1]["o"] == 3.0


def test_diff_endpoint_sees_changes_past_100k(tmp_path, monkeypatch):
    """Compare must see the same rows as Data-tab DuckDB paging, not a 100k cap."""
    import api.preview_routes as pr
    import api.preview_service as ps

    n = 120_050
    base = pd.DataFrame({"id": range(n), "x": [0] * n})
    target = base.copy()
    target.loc[120_000, "x"] = 1
    base.to_parquet(tmp_path / "base.parquet", index=False)
    target.to_parquet(tmp_path / "target.parquet", index=False)

    _FakeWorkspaceManager.root = tmp_path
    fake_state = _FakeStateManager({("s1", "user-1"): _FakeSession([_FakeContext(base)])})
    monkeypatch.setattr(pr, "state_manager", fake_state)
    monkeypatch.setattr(pr, "WorkspaceManager", _FakeWorkspaceManager)
    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(preview_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    client = TestClient(app)

    body = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "base.parquet",
            "target_source": "file",
            "target_path": "target.parquet",
            "changes_only": "true",
        },
    ).json()
    assert body["summary"]["cells_changed"] == 1
    assert body["filtered_rows"] == 1
    assert body["rows"][0][0] == 120_000
    assert body["cell_meta"][0][1]["k"] == "changed"


def test_diff_endpoint_dataset_vs_file(client):
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "raw.parquet",
            "target_source": "dataset",
            "target_file_id": "f1",
            "changes_only": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filtered_rows"] == 1
    assert body["rows"][0][0] == 3


def test_diff_endpoint_rejects_text_file(client):
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "notes.txt",
            "target_source": "file",
            "target_path": "cleaned.csv",
        },
    )
    assert r.status_code == 400


def test_diff_endpoint_wrong_session_404(client):
    r = client.get(
        "/api/preview/diff/nope",
        params={
            "base_source": "file",
            "base_path": "raw.parquet",
            "target_source": "file",
            "target_path": "cleaned.csv",
        },
    )
    assert r.status_code == 404


def test_standardized_survives_row_drops():
    """Scaler fit on the full frame, then rows dropped: exact stats no longer
    reproduce from surviving pairs, but the affine detector still names it."""
    rng = np.random.default_rng(11)
    vals = rng.normal(60000, 12000, 50)
    base = pd.DataFrame({"id": range(50), "x": vals})
    z = (vals - vals.mean()) / vals.std()
    target = pd.DataFrame({"id": range(50), "x": np.round(z, 6)})
    target = target[~target["id"].isin([3, 27])].reset_index(drop=True)  # drop after scaling
    r = diff_service.compute_diff(base, target)
    assert next(c for c in r.columns if c["name"] == "x")["note"] == "standardized"


def test_constant_shift_column():
    vals = np.array([10.0, 20, 30, 40, 55])
    base = pd.DataFrame({"id": range(5), "x": vals})
    target = base.copy()
    target["x"] = vals - 273.15
    r = diff_service.compute_diff(base, target)
    note = next(c for c in r.columns if c["name"] == "x")["note"]
    assert note is not None and "shifted" in note


def test_mean_imputation_survives_rounding():
    base = pd.DataFrame({"id": range(6), "x": [10.0, 20.0, 40.0, None, None, 70.0]})
    target = base.copy()
    target.loc[3, "x"] = round(base["x"].mean(), 6)
    target.loc[4, "x"] = round(base["x"].median(), 6)
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 3, "x") == ("imputed", "mean imputed")
    assert _meta_at(r, 4, "x") == ("imputed", "median imputed")


def test_constant_fill_when_stats_do_not_reproduce():
    base = pd.DataFrame({"id": range(6), "x": [10.0, 20.0, None, None, None, 70.0]})
    target = base.copy()
    target.loc[[2, 3, 4], "x"] = 41.7  # same fill everywhere, matches no base stat
    r = diff_service.compute_diff(base, target)
    assert _meta_at(r, 2, "x") == ("imputed", "constant fill")
    assert _meta_at(r, 4, "x") == ("imputed", "constant fill")


# ----------------------------------------------------------- ghosts + filters


def test_ghost_rows_appended_for_removed_records():
    base = pd.DataFrame({"id": [1, 2, 3, 4], "v": ["a", "b", "c", "d"]})
    target = pd.DataFrame({"id": [1, 3], "v": ["a", "c"]})
    r = diff_service.compute_diff(base, target)
    assert len(r.values) == 4  # 2 target rows + 2 ghosts
    assert list(r.ghost_rows) == [False, False, True, True]
    assert list(r.values["id"]) == [1, 3, 2, 4]
    j = [c["name"] for c in r.columns].index("v")
    assert r.kinds[2, j] == "removed" and r.kinds[3, j] == "removed"
    page = diff_service.build_diff_page(r)
    assert page["total_rows"] == 4
    ghost_meta = page["cell_meta"][2][j]
    assert ghost_meta["k"] == "removed" and "o" not in ghost_meta


def test_ghost_rows_blank_added_columns():
    base = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
    target = pd.DataFrame({"id": [1], "v": [10], "extra": ["x"]})
    r = diff_service.compute_diff(base, target)
    j = [c["name"] for c in r.columns].index("extra")
    assert pd.isna(r.values.iloc[1, j])  # ghost row has no value for new column


def test_change_kind_filter():
    base = pd.DataFrame({"id": [1, 2, 3, 4], "x": [1.0, None, 3.0, 4.0]})
    target = pd.DataFrame(
        {"id": [1, 2, 5], "x": [9.0, 7.0, 5.0]}
    )  # 1 changed, 2 imputed, 5 added, 3&4 removed
    r = diff_service.compute_diff(base, target)
    for kind, expected_ids in (
        ("changed", [1]),
        ("imputed", [2]),
        ("added", [5]),
        ("removed", [3, 4]),
    ):
        page = diff_service.build_diff_page(r, change_kind=kind)
        assert [row[0] for row in page["rows"]] == expected_ids, kind


def test_diff_page_filters_search_sort():
    base = pd.DataFrame({"id": range(6), "name": ["ant", "bee", "cat", "dog", "eel", "fox"]})
    target = base.copy()
    target.loc[1, "name"] = "wasp"
    r = diff_service.compute_diff(base, target)
    page = diff_service.build_diff_page(r, filters={"name": "a"}, sort_by="name", sort_dir="desc")
    assert [row[1] for row in page["rows"]] == ["wasp", "cat", "ant"]
    page = diff_service.build_diff_page(r, search="wasp")
    assert page["filtered_rows"] == 1 and page["rows"][0][0] == 1


def test_diff_endpoint_kind_and_sort(client):
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "raw.parquet",
            "target_source": "file",
            "target_path": "cleaned.csv",
            "change_kind": "changed",
            "sort_by": "x",
            "sort_dir": "desc",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filtered_rows"] == 1 and body["rows"][0][0] == 3
