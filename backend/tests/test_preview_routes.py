"""Tests for the data preview service and endpoints.

Service units run the pipeline directly on DataFrames; endpoint tests mount the
preview router on a minimal FastAPI app with a stubbed user and state manager.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import preview_service
from api.dependencies import get_current_user
from api.preview_routes import router as preview_router

# ------------------------------------------------------------------- pipeline


def _df(n: int = 10_050) -> pd.DataFrame:
    return pd.DataFrame({"i": range(n), "name": [f"row_{k}" for k in range(n)]})


def test_pagination_math():
    page = preview_service.build_table_page(_df(), page=1, page_size=100)
    assert page["total_rows"] == 10_050
    assert page["filtered_rows"] == 10_050
    assert page["total_pages"] == 101
    assert len(page["rows"]) == 100
    assert page["rows"][0][0] == 0

    last = preview_service.build_table_page(_df(), page=101, page_size=100)
    assert len(last["rows"]) == 50
    assert last["rows"][-1][0] == 10_049


def test_page_clamps_out_of_range():
    page = preview_service.build_table_page(_df(), page=9_999, page_size=100)
    assert page["page"] == 101
    assert len(page["rows"]) == 50


def test_empty_frame_has_one_page():
    page = preview_service.build_table_page(pd.DataFrame({"a": []}), page=1, page_size=100)
    assert page["total_pages"] == 1
    assert page["rows"] == []


def test_sort_asc_desc():
    df = pd.DataFrame({"x": [3, 1, 2]})
    asc = preview_service.apply_query(df, sort_by="x", sort_dir="asc")
    desc = preview_service.apply_query(df, sort_by="x", sort_dir="desc")
    assert list(asc["x"]) == [1, 2, 3]
    assert list(desc["x"]) == [3, 2, 1]


def test_mixed_dtype_object_sort_does_not_raise():
    df = pd.DataFrame({"x": ["b", 2, None, "a", 1.5]})
    out = preview_service.apply_query(df, sort_by="x", sort_dir="asc")
    # lexicographic on str(); None last
    assert pd.isna(out["x"].iloc[-1])


def test_column_filter_substring_case_insensitive():
    df = pd.DataFrame({"city": ["Boston", "Austin", "boise"], "n": [1, 2, 3]})
    out = preview_service.apply_query(df, filters={"city": "bo"})
    assert list(out["city"]) == ["Boston", "boise"]


def test_filter_unknown_column_is_skipped():
    df = pd.DataFrame({"a": [1, 2]})
    out = preview_service.apply_query(df, filters={"nope": "x"})
    assert len(out) == 2


def test_global_search_across_columns():
    df = pd.DataFrame({"a": ["apple", "pear"], "b": ["cherry", "apricot"]})
    out = preview_service.apply_query(df, search="ap")
    assert len(out) == 2  # apple (a), apricot (b)
    out2 = preview_service.apply_query(df, search="cherry")
    assert len(out2) == 1


def test_filter_search_sort_composed():
    df = pd.DataFrame(
        {
            "team": ["red", "red", "blue", "red"],
            "player": ["zoe", "amy", "zoe", "zed"],
            "score": [3, 1, 9, 2],
        }
    )
    page = preview_service.build_table_page(
        df, filters={"team": "red"}, search="z", sort_by="score", sort_dir="desc"
    )
    assert page["filtered_rows"] == 2  # zoe + zed on red
    assert [r[2] for r in page["rows"]] == [3, 2]
    assert page["total_rows"] == 4


def test_serialization_json_safe():
    df = pd.DataFrame(
        {
            "f": [1.5, np.nan, np.inf],
            "t": [pd.Timestamp("2026-01-02"), pd.NaT, pd.Timestamp("2026-01-03 04:05")],
            "n": np.array([1, 2, 3], dtype=np.int64),
        }
    )
    page = preview_service.build_table_page(df)
    rows = page["rows"]
    assert rows[0] == [1.5, "2026-01-02T00:00:00", 1]
    assert rows[1][0] is None and rows[1][1] is None
    assert rows[2][0] is None  # inf -> null


def test_column_kinds():
    df = pd.DataFrame(
        {
            "b": [True, False],
            "n": [1, 2],
            "d": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "s": ["x", "y"],
        }
    )
    kinds = {c["name"]: c["kind"] for c in preview_service.column_specs(df)}
    assert kinds == {"b": "bool", "n": "number", "d": "date", "s": "string"}


def test_stats():
    df = pd.DataFrame(
        {
            "n": [1.0, 2.0, np.nan, 3.0],
            "s": ["a", "a", "b", None],
            "d": pd.to_datetime(["2026-01-01", "2026-01-05", None, "2026-01-03"]),
        }
    )
    stats = preview_service.compute_stats(df)
    assert stats["n"] == {"nulls": 1, "unique": 3, "min": 1.0, "max": 3.0, "mean": 2.0}
    assert stats["s"]["nulls"] == 1 and stats["s"]["unique"] == 2
    assert stats["d"]["min"] == "2026-01-01T00:00:00"
    assert "mean" not in stats["d"]


def test_source_frame_not_mutated():
    df = pd.DataFrame({"x": [3, 1, 2], "y": ["c", "a", "b"]})
    original = df.copy(deep=True)
    preview_service.build_table_page(
        df, filters={"y": "a"}, search="a", sort_by="x", sort_dir="desc", stats=None
    )
    pd.testing.assert_frame_equal(df, original)


# ------------------------------------------------------------------ file I/O


def test_parquet_and_csv_loads_are_not_row_capped(tmp_path):
    n = 12_000
    pd.DataFrame({"a": range(n)}).to_csv(tmp_path / "big.csv", index=False)
    pd.DataFrame({"a": range(n)}).to_parquet(tmp_path / "big.parquet", index=False)
    csv_df, csv_trunc = preview_service._read_table_file(tmp_path / "big.csv")
    pq_df, pq_trunc = preview_service._read_table_file(tmp_path / "big.parquet")
    assert len(csv_df) == n and csv_trunc is False
    assert len(pq_df) == n and pq_trunc is False


def test_load_csv_tsv_parquet_xlsx(tmp_path):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    df.to_csv(tmp_path / "d.csv", index=False)
    df.to_csv(tmp_path / "d.tsv", sep="\t", index=False)
    df.to_parquet(tmp_path / "d.parquet", index=False)
    df.to_excel(tmp_path / "d.xlsx", index=False)
    for name in ("d.csv", "d.tsv", "d.parquet", "d.xlsx"):
        entry = preview_service.load_table_file(tmp_path / name)
        assert list(entry["df"].columns) == ["a", "b"], name
        assert len(entry["df"]) == 2
        assert entry["truncated"] is False


def test_load_missing_file_404(tmp_path):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        preview_service.load_table_file(tmp_path / "nope.csv")
    assert exc.value.status_code == 404


def test_pandas_fallback_xlsx_row_cap_still_truncates(tmp_path, monkeypatch):
    """xlsx without a parquet sibling still uses the pandas loader and stays capped."""
    monkeypatch.setattr(preview_service, "MAX_LOAD_ROWS", 10)
    pd.DataFrame({"a": range(25)}).to_excel(tmp_path / "big.xlsx", index=False)
    df, truncated = preview_service._read_table_file(tmp_path / "big.xlsx")
    assert len(df) == 10 and truncated is True


def test_text_file_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_service, "MAX_TEXT_BYTES", 16)
    p = tmp_path / "notes.txt"
    p.write_text("hello " * 10)
    out = preview_service.read_text_file(p)
    assert out["truncated"] is True
    assert len(out["text"].encode()) <= 16
    assert out["size_bytes"] == 60


def test_cache_hit_and_mtime_invalidation(tmp_path):
    preview_service._frame_cache.clear()
    p = tmp_path / "c.csv"
    pd.DataFrame({"a": [1]}).to_csv(p, index=False)
    e1 = preview_service.load_table_file(p)
    e2 = preview_service.load_table_file(p)
    assert e1 is e2  # same cache entry
    import os

    pd.DataFrame({"a": [1, 2]}).to_csv(p, index=False)
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 1_000_000))
    e3 = preview_service.load_table_file(p)
    assert e3 is not e1
    assert len(e3["df"]) == 2


# ------------------------------------------------------------------ endpoints


class _FakeContext:
    def __init__(self, df: pd.DataFrame, file_id: str = "f1", filename: str = "titanic.csv"):
        self.dataframe = df
        self.raw_dataframe = df
        self.working_dataframe = df
        self.file_id = file_id
        self.filename = filename
        self.metadata = {"source": "postgres"}


class _FakeSession:
    def __init__(self, ctxs):
        self._ctxs = ctxs
        self.data_context = ctxs[0] if ctxs else None
        self.metadata = {"active_dataset": "titanic"}

    def get_all_data_contexts(self):
        return list(self._ctxs)


class _FakeStateManager:
    def __init__(self, sessions):
        self.sessions = sessions

    def get_session(self, session_id, app_user_id=None):
        return self.sessions.get((session_id, app_user_id))


class _FakeWorkspaceManager:
    root: object = None

    def get_or_create(self, session_id, user_id=None):
        return type(self).root


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import api.preview_routes as pr

    df = pd.DataFrame({"a": range(250), "b": [f"v{i}" for i in range(250)]})
    # Seeded mirror of the active dataset (same stem) — must be deduped away.
    df.head(3).to_csv(tmp_path / "titanic.csv", index=False)
    pd.DataFrame({"z": [9, 8, 7]}).to_parquet(tmp_path / "titanic.parquet", index=False)
    # A genuine, separate artifact — must still be listed.
    pd.DataFrame({"z": [9, 8, 7]}).to_csv(tmp_path / "cleaned.csv", index=False)
    (tmp_path / "report.txt").write_text("all clean")
    (tmp_path / "script.py").write_text("print('no')")

    _FakeWorkspaceManager.root = tmp_path
    fake_state = _FakeStateManager({("s1", "user-1"): _FakeSession([_FakeContext(df)])})
    monkeypatch.setattr(pr, "state_manager", fake_state)
    monkeypatch.setattr(pr, "WorkspaceManager", _FakeWorkspaceManager)
    monkeypatch.setattr(pr, "list_workspace_artifacts", _list_artifacts)
    # Source resolution now lives in preview_service (shared with filters).
    import api.preview_service as ps

    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(preview_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    return TestClient(app)


def _list_artifacts(root):
    from workspace.manager import list_workspace_artifacts as real

    return real(root)


def _build_client(tmp_path, monkeypatch, ctxs, metadata):
    """A preview client over a custom session (contexts + metadata)."""
    import api.preview_routes as pr

    _FakeWorkspaceManager.root = tmp_path
    session = _FakeSession(ctxs)
    session.metadata = metadata
    fake_state = _FakeStateManager({("s1", "user-1"): session})
    monkeypatch.setattr(pr, "state_manager", fake_state)
    monkeypatch.setattr(pr, "WorkspaceManager", _FakeWorkspaceManager)
    monkeypatch.setattr(pr, "list_workspace_artifacts", _list_artifacts)
    # Source resolution now lives in preview_service (shared with filters).
    import api.preview_service as ps

    monkeypatch.setattr(ps, "WorkspaceManager", _FakeWorkspaceManager)

    app = FastAPI()
    app.include_router(preview_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    return TestClient(app)


def test_sources_backed_dataset_shown_as_file(client):
    r = client.get("/api/preview/sources/s1")
    assert r.status_code == 200
    sources = r.json()["sources"]
    by_kind = {}
    for s in sources:
        by_kind.setdefault(s["kind"], []).append(s)
    # The in-memory dataset's identity (titanic) is backed by a workspace file,
    # so it is represented by that file — not the mutable frame.
    assert "dataset" not in by_kind
    by_name = {f["name"]: f for f in by_kind["file"]}
    # titanic.csv/.parquet mirror one identity -> parquet (the bytes on disk) is shown;
    # cleaned.csv is a separate artifact; report.txt is text; script.py excluded.
    assert set(by_name) == {"titanic.parquet", "cleaned.csv", "report.txt"}
    assert by_name["titanic.parquet"]["is_active"] is True
    assert by_name["cleaned.csv"]["is_active"] is False
    assert by_name["titanic.parquet"]["renderer"] == "table"
    assert by_name["titanic.parquet"]["capability_summary"] == "Snapshot · Full-data analysis"
    assert by_name["report.txt"]["renderer"] == "text"


def test_sources_unbacked_dataset_listed(tmp_path, monkeypatch):
    """A connector/unsaved dataset (no matching workspace file) still appears as a
    dataset source, active when it is the primary and nothing else is named."""
    df = pd.DataFrame({"a": range(3)})
    df.to_csv(tmp_path / "export.csv", index=False)  # separate identity
    ctx = _FakeContext(df, file_id="pg1", filename="orders")
    ctx.metadata = {"source": "postgres"}
    client = _build_client(tmp_path, monkeypatch, [ctx], {})  # no active_dataset

    sources = client.get("/api/preview/sources/s1").json()["sources"]
    ds = [s for s in sources if s["kind"] == "dataset"]
    assert len(ds) == 1
    assert ds[0]["origin"] == "postgres" and ds[0]["rows"] == 3
    assert ds[0]["capability_summary"] == "Snapshot · Loaded sample (up to 10K rows)"
    assert ds[0]["is_active"] is True


def test_sources_repointed_frame_does_not_mask_original_file(tmp_path, monkeypatch):
    """Regression: set_active_dataset repoints the in-memory frame to a derived
    file while ctx.filename keeps the original upload name. The original workspace
    file must stay listed (served from disk) instead of being hidden behind the
    mutated frame — otherwise an original-vs-derived diff shows no changes."""
    original = pd.DataFrame({"id": range(5), "x": [1.0, None, 3.0, None, 5.0]})
    filled = original.copy()
    filled["x"] = filled["x"].fillna(0.0)
    for df, stem in ((original, "Cafe_Sales_Data"), (filled, "Cafe_Sales_Data_filled")):
        df.to_parquet(tmp_path / f"{stem}.parquet", index=False)
        df.to_csv(tmp_path / f"{stem}.csv", index=False)  # mirror the user created

    # The in-memory frame has been repointed to the FILLED data, yet is still
    # named after the original upload (the exact bug condition).
    ctx = _FakeContext(filled, file_id="f1", filename="Cafe Sales Data.csv")
    ctx.metadata = {"source": "upload"}
    client = _build_client(
        tmp_path, monkeypatch, [ctx], {"active_dataset": "Cafe_Sales_Data_filled"}
    )

    sources = client.get("/api/preview/sources/s1").json()["sources"]
    # No dataset entry: the frame's identity is backed by the original file; each
    # dataset's copies collapse to one parquet entry.
    assert all(s["kind"] == "file" for s in sources)
    by_name = {s["name"]: s for s in sources}
    assert set(by_name) == {"Cafe_Sales_Data.parquet", "Cafe_Sales_Data_filled.parquet"}
    # Exactly the derived file is active; the original is selectable and inactive.
    assert by_name["Cafe_Sales_Data_filled.parquet"]["is_active"] is True
    assert by_name["Cafe_Sales_Data.parquet"]["is_active"] is False

    # Diffing the original entry against the filled entry now surfaces the two
    # imputations (previously the "original" entry served the repointed frame,
    # yielding zero changes).
    diff = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": by_name["Cafe_Sales_Data.parquet"]["id"],
            "target_source": "file",
            "target_path": by_name["Cafe_Sales_Data_filled.parquet"]["id"],
        },
    ).json()
    assert diff["summary"]["cells_changed"] == 2


def test_diff_reads_lossless_parquet_for_dtype_recast(tmp_path, monkeypatch):
    """A numeric column cast to string is a real change that a CSV round-trip
    hides (pd.read_csv re-infers the strings back to numbers on both sides). The
    diff must read the .parquet mirror so the recast is detected, not shown as
    unchanged."""
    base = pd.DataFrame({"id": range(4), "x": [1, 2, 3, 4]})  # int64
    target = pd.DataFrame({"id": range(4), "x": ["1", "2", "3", "4"]})  # object/str
    for df, stem in ((base, "d"), (target, "d_str")):
        df.to_parquet(tmp_path / f"{stem}.parquet", index=False)
        df.to_csv(tmp_path / f"{stem}.csv", index=False)  # lossy mirror
    ctx = _FakeContext(base, file_id="f1", filename="d.csv")
    ctx.metadata = {"source": "upload"}
    client = _build_client(tmp_path, monkeypatch, [ctx], {"active_dataset": "d"})

    # The user-facing entries are the .csv paths; the read upgrades to parquet.
    diff = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "file",
            "base_path": "d.csv",
            "target_source": "file",
            "target_path": "d_str.csv",
        },
    ).json()
    x_spec = next(c for c in diff["columns"] if c["name"] == "x")
    assert x_spec["note"] == "recast"
    assert x_spec["changed_cells"] == 4


def test_sources_wrong_user_404(client):
    r = client.get("/api/preview/sources/other-session")
    assert r.status_code == 404


def test_page_from_dataset(client):
    r = client.get(
        "/api/preview/page/s1",
        params={"source": "dataset", "file_id": "f1", "page": 3, "page_size": 100},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "table"
    assert body["total_rows"] == 250 and body["total_pages"] == 3
    assert len(body["rows"]) == 50
    assert body["rows"][0][0] == 200


def test_page_from_file_with_stats(client):
    r = client.get(
        "/api/preview/page/s1",
        params={"source": "file", "path": "titanic.parquet", "include_stats": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert [c["name"] for c in body["columns"]] == ["z"]
    assert body["stats"]["z"]["min"] == 7 and body["stats"]["z"]["max"] == 9
    assert body["truncated"] is False
    assert body["total_rows"] == 3


def test_page_from_csv_without_parquet_sibling(client):
    r = client.get(
        "/api/preview/page/s1",
        params={"source": "file", "path": "cleaned.csv", "page": 1, "page_size": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is False
    assert body["total_rows"] == 3
    assert len(body["rows"]) == 2


def test_page_file_full_relation_past_100k(tmp_path, monkeypatch):
    from datasources.adapters import clear_caches

    clear_caches()
    n = 120_050
    names = ["x"] * n
    names[120_000] = "needle_unique"
    pd.DataFrame({"i": range(n), "name": names}).to_parquet(tmp_path / "wide.parquet", index=False)
    ctx = _FakeContext(pd.DataFrame({"i": [0]}), file_id="f1", filename="wide.csv")
    ctx.metadata = {"source": "upload"}
    client = _build_client(tmp_path, monkeypatch, [ctx], {"active_dataset": "wide"})

    def _boom(*_args, **_kwargs):
        raise AssertionError("pandas must not load the preview parquet")

    monkeypatch.setattr(pd, "read_parquet", _boom)

    page = client.get(
        "/api/preview/page/s1",
        params={"source": "file", "path": "wide.parquet", "page": 2, "page_size": 100},
    ).json()
    assert page["truncated"] is False
    assert page["total_rows"] == n
    assert page["total_pages"] == 1_201
    assert page["page"] == 2
    assert page["rows"][0][0] == 100

    hit = client.get(
        "/api/preview/page/s1",
        params={"source": "file", "path": "wide.parquet", "search": "needle_unique"},
    ).json()
    assert hit["filtered_rows"] == 1
    assert hit["total_rows"] == n
    assert hit["rows"][0][0] == 120_000
    clear_caches()


def test_page_text_file(client):
    r = client.get("/api/preview/page/s1", params={"source": "file", "path": "report.txt"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "text" and body["text"] == "all clean"


def test_page_traversal_rejected(client):
    r = client.get("/api/preview/page/s1", params={"source": "file", "path": "../secrets.csv"})
    assert r.status_code == 400


def test_page_disallowed_suffix(client):
    r = client.get("/api/preview/page/s1", params={"source": "file", "path": "script.py"})
    assert r.status_code == 400


def test_page_unknown_dataset_404(client):
    r = client.get("/api/preview/page/s1", params={"source": "dataset", "file_id": "nope"})
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "source_removed"


def test_page_invalid_filters_400(client):
    r = client.get(
        "/api/preview/page/s1",
        params={"source": "dataset", "file_id": "f1", "filters": "not-json"},
    )
    assert r.status_code == 400


def test_page_filter_and_search(client):
    r = client.get(
        "/api/preview/page/s1",
        params={
            "source": "dataset",
            "file_id": "f1",
            "filters": '{"b": "v24"}',
            "search": "v249",
        },
    )
    body = r.json()
    assert body["filtered_rows"] == 1
    assert body["rows"][0][1] == "v249"
    assert body["total_rows"] == 250


def test_workspace_archive_download(client, monkeypatch):
    import api.cleaning_export_routes as cer

    monkeypatch.setattr(
        cer,
        "state_manager",
        _FakeStateManager(
            {("s1", "user-1"): _FakeSession([_FakeContext(pd.DataFrame({"a": [1]}))])}
        ),
    )
    monkeypatch.setattr(cer, "WorkspaceManager", _FakeWorkspaceManager)

    from fastapi import FastAPI

    from api.cleaning_export_routes import router as export_router

    app = FastAPI()
    app.include_router(export_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    archive_client = TestClient(app)

    r = archive_client.get("/api/export/cleaning-files-archive/s1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    import io as _io
    import zipfile as _zipfile

    names = set(_zipfile.ZipFile(_io.BytesIO(r.content)).namelist())
    assert "titanic.parquet" in names and "report.txt" in names
    assert "script.py" not in names  # scripts never leave the workspace


def test_export_parquet_as_csv_and_native(client, monkeypatch):
    import io as _io

    import api.cleaning_export_routes as cer

    monkeypatch.setattr(
        cer,
        "state_manager",
        _FakeStateManager(
            {("s1", "user-1"): _FakeSession([_FakeContext(pd.DataFrame({"a": [1]}))])}
        ),
    )
    monkeypatch.setattr(cer, "WorkspaceManager", _FakeWorkspaceManager)

    from fastapi import FastAPI

    from api.cleaning_export_routes import router as export_router

    app = FastAPI()
    app.include_router(export_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    export_client = TestClient(app)

    native = export_client.get(
        "/api/export/cleaning-file/s1",
        params={"path": "titanic.parquet", "format": "parquet"},
    )
    assert native.status_code == 200
    assert "titanic.parquet" in native.headers.get("content-disposition", "")

    csv = export_client.get(
        "/api/export/cleaning-file/s1",
        params={"path": "titanic.parquet", "format": "csv"},
    )
    assert csv.status_code == 200
    assert "text/csv" in csv.headers.get("content-type", "")
    assert "titanic.csv" in csv.headers.get("content-disposition", "")
    exported = pd.read_csv(_io.StringIO(csv.text))
    assert list(exported.columns) == ["z"]
    assert len(exported) == 3


# ------------------------------------------------------- sql (pushdown) sources


class _FakeTableAdapter:
    """Fake pushdown adapter: fixed schema/rows, records the query args it
    receives so tests can assert the endpoint forwarded them."""

    dialect = "postgres"
    supports_stats = True

    def __init__(self, total=1000, approx=False):
        from datasources.adapters import AdapterColumn, CountResult, SourceCacheInfo

        self._columns = [
            AdapterColumn("id", "integer", "number"),
            AdapterColumn("name", "text", "string"),
        ]
        self._count = CountResult(total, approx=approx)
        self.cache_info = SourceCacheInfo(key=("fake", id(self)), ttl=60)
        self.calls = []

    def schema(self):
        return self._columns

    def count(self, filters=None, search=None):
        self.calls.append(("count", filters, search))
        return self._count

    def fetch_page(self, **kwargs):
        self.calls.append(("page", kwargs))
        return [[i, f"n{i}"] for i in range(kwargs["offset"], kwargs["offset"] + 3)]

    def stats(self):
        return {"id": {"nulls": 0, "unique": 1000}}


def _pg_ctx(df, file_id="pg1", temp=False):
    ctx = _FakeContext(df, file_id=file_id, filename="public.orders")
    ctx.metadata = {
        "source": "postgres",
        "connection_url": "postgresql://u:p@h/db",
        "schema": "public",
        "table": "orders",
    }
    if temp:
        ctx.metadata.update({"temp_schema": "stych_temp", "temp_table": "w1"})
    return ctx


def test_sources_db_context_listed_as_sql_base(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df)], {})
    sources = client.get("/api/preview/sources/s1").json()["sources"]
    sql = [s for s in sources if s["kind"] == "sql"]
    assert len(sql) == 1
    entry = sql[0]
    assert entry["id"] == "pg1::base" and entry["role"] == "base"
    assert entry["origin"] == "postgres" and entry["supports_stats"] is True
    assert entry["capability_summary"] == "Live · Full-data analysis"
    assert entry["rows"] is None  # counting belongs to the page fetch
    assert entry["is_active"] is True  # no working copy yet
    # The 10K sample must NOT also appear as a dataset entry.
    assert not [s for s in sources if s["kind"] == "dataset"]


def test_sources_working_copy_listed_and_active(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df, temp=True)], {})
    sources = client.get("/api/preview/sources/s1").json()["sources"]
    by_role = {s["role"]: s for s in sources if s["kind"] == "sql"}
    assert set(by_role) == {"base", "working"}
    assert by_role["working"]["id"] == "pg1::temp"
    assert "working copy" in by_role["working"]["name"]
    assert by_role["working"]["is_active"] is True
    assert by_role["working"]["capability_summary"] == "Working copy · Full-data analysis"
    assert by_role["base"]["is_active"] is False


def test_sources_redacted_db_context_falls_back_to_dataset(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    ctx = _pg_ctx(df)
    ctx.metadata["connection_url"] = "[REDACTED]"
    client = _build_client(tmp_path, monkeypatch, [ctx], {})
    sources = client.get("/api/preview/sources/s1").json()["sources"]
    assert not [s for s in sources if s["kind"] == "sql"]
    assert [s for s in sources if s["kind"] == "dataset"]  # sample still previewable


def test_page_sql_pushdown_forwarded_and_approx(tmp_path, monkeypatch):
    import api.preview_routes as pr
    from datasources.adapters import clear_caches

    clear_caches()
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df)], {})
    fake = _FakeTableAdapter(total=100_000, approx=True)
    monkeypatch.setattr(pr, "_resolve_sql_adapter", lambda ctx, variant: fake)

    r = client.get(
        "/api/preview/page/s1",
        params={
            "source": "sql",
            "source_id": "pg1::base",
            "page": 3,
            "page_size": 100,
            "sort_by": "name",
            "sort_dir": "desc",
            "filters": '{"name": "x"}',
            "search": "q",
            "include_stats": "true",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "table"
    assert body["total_rows"] == 100_000 and body["approx"] is True
    assert body["truncated"] is False
    assert body["stats"]["id"]["unique"] == 1000
    assert [c["name"] for c in body["columns"]] == ["id", "name"]
    page_call = next(c for c in fake.calls if c[0] == "page")[1]
    assert page_call["offset"] == 200 and page_call["limit"] == 100
    assert page_call["sort_by"] == "name" and page_call["sort_dir"] == "desc"
    assert page_call["filters"] == {"name": "x"} and page_call["search"] == "q"
    # Filtered count pushed down with the same narrowing.
    assert ("count", {"name": "x"}, "q") in fake.calls
    clear_caches()


def test_page_sql_missing_or_bad_source_id(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [1]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df)], {})
    assert client.get("/api/preview/page/s1", params={"source": "sql"}).status_code == 400
    r = client.get("/api/preview/page/s1", params={"source": "sql", "source_id": "pg1::nope"})
    assert r.status_code == 400
    r = client.get("/api/preview/page/s1", params={"source": "sql", "source_id": "ghost::base"})
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "source_removed"


def test_page_sql_adapter_errors_mapped(tmp_path, monkeypatch):
    import api.preview_routes as pr
    from datasources.adapters import AdapterUnavailable, TempTableUnavailable

    df = pd.DataFrame({"id": [1]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df)], {})

    def raise_unavailable(ctx, variant):
        raise AdapterUnavailable("reconnect the table")

    monkeypatch.setattr(pr, "_resolve_sql_adapter", raise_unavailable)
    r = client.get("/api/preview/page/s1", params={"source": "sql", "source_id": "pg1::base"})
    assert r.status_code == 409

    def raise_no_temp(ctx, variant):
        raise TempTableUnavailable("no working table yet")

    monkeypatch.setattr(pr, "_resolve_sql_adapter", raise_no_temp)
    r = client.get("/api/preview/page/s1", params={"source": "sql", "source_id": "pg1::temp"})
    assert r.status_code == 404


def test_diff_mixed_sql_and_file_rejected(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [1]})
    df.to_csv(tmp_path / "export.csv", index=False)
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df)], {})
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "sql",
            "base_source_id": "pg1::base",
            "target_source": "file",
            "target_path": "export.csv",
        },
    )
    assert r.status_code == 400
    assert "database table with a file" in r.json()["detail"]


def test_diff_refuses_a_source_with_no_working_copy_plane(tmp_path, monkeypatch):
    """Every shipped connector diffs copy-vs-copy in its own source, so the
    refusal is for a source that has no such plane at all."""
    df = pd.DataFrame({"id": [1]})
    ctx = _pg_ctx(df, temp=True)
    ctx.metadata["source"] = "oracle"
    client = _build_client(tmp_path, monkeypatch, [ctx], {})
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "sql",
            "base_source_id": "pg1::base",
            "target_source": "sql",
            "target_source_id": "pg1::temp",
        },
    )
    assert r.status_code == 400
    assert "not supported for oracle" in r.json()["detail"]


def test_diff_sql_pair_routed_to_sql_diff_service(tmp_path, monkeypatch):
    import api.preview_routes as pr

    df = pd.DataFrame({"id": [1]})
    client = _build_client(tmp_path, monkeypatch, [_pg_ctx(df, temp=True)], {})
    fake = _FakeTableAdapter()
    monkeypatch.setattr(pr, "_resolve_sql_adapter", lambda ctx, variant: fake)

    captured = {}

    def fake_build(base, target, **kwargs):
        captured.update(kwargs, base=base, target=target)
        return {
            "kind": "diff",
            "columns": [],
            "rows": [],
            "cell_meta": [],
            "total_rows": 0,
            "filtered_rows": 0,
            "page": 1,
            "total_pages": 1,
            "summary": {
                "rows_added": 0,
                "rows_removed": 0,
                "rows_changed": 0,
                "rows_unchanged": 0,
                "cells_changed": 0,
                "similarity": 1.0,
                "alignment": "key:id",
            },
        }

    monkeypatch.setattr(pr.sql_diff_service, "build_sql_diff_page", fake_build)
    r = client.get(
        "/api/preview/diff/s1",
        params={
            "base_source": "sql",
            "base_source_id": "pg1::base",
            "target_source": "sql",
            "target_source_id": "pg1::temp",
            "changes_only": "true",
            "change_kind": "imputed",
        },
    )
    assert r.status_code == 200
    assert captured["changes_only"] is True and captured["change_kind"] == "imputed"
    assert captured["base"] is fake and captured["target"] is fake
