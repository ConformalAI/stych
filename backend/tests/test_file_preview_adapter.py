"""DuckDB file-preview adapter: full-file LIMIT/OFFSET without a pandas load."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from datasources.adapters.base import clear_caches
from datasources.adapters.file_preview import open_file_preview_adapter
from datasources.adapters.preview_sql import build_select_sql


def _write_parquet(path: Path, n: int = 1_250, needle_at: int | None = 1_200) -> Path:
    names = [f"row_{i}" for i in range(n)]
    if needle_at is not None and 0 <= needle_at < n:
        names[needle_at] = "needle_unique"
    pd.DataFrame({"i": range(n), "name": names}).to_parquet(path, index=False)
    return path


def test_preview_sql_limit_offset_and_like_escape():
    sql, params = build_select_sql(
        '"preview"',
        ["i", "name"],
        offset=100,
        limit=50,
        sort_by="i",
        filters={"name": "50%"},
        search="foo",
    )
    assert "LIMIT :limit OFFSET :offset" in sql
    assert params["limit"] == 50 and params["offset"] == 100
    assert params["f0"] == "%50\\%%"
    assert "ILIKE :f0 ESCAPE '\\'" in sql


def test_file_preview_pages_past_100k_without_pandas(tmp_path, monkeypatch):
    clear_caches()
    path = _write_parquet(tmp_path / "big.parquet", n=120_050, needle_at=120_000)

    def _boom(*_args, **_kwargs):
        raise AssertionError("pandas must not load the preview parquet")

    monkeypatch.setattr(pd, "read_parquet", _boom)

    with open_file_preview_adapter("s1", path, allowed_root=tmp_path) as adapter:
        assert adapter.count().value == 120_050
        page = adapter.fetch_page(offset=100, limit=100)
        assert page[0][0] == 100
        assert len(page) == 100
        found = adapter.fetch_page(offset=0, limit=10, search="needle_unique")
        assert len(found) == 1 and found[0][0] == 120_000
        filtered = adapter.count(search="needle_unique")
        assert filtered.value == 1
        stats = adapter.stats()
        assert stats is not None
        assert stats["i"]["min"] == 0
        assert stats["i"]["max"] == 120_049
        assert stats["i"]["nulls"] == 0
    clear_caches()


def test_csv_without_parquet_sibling_pages(tmp_path):
    clear_caches()
    path = tmp_path / "only.csv"
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_csv(path, index=False)
    with open_file_preview_adapter("s1", path, allowed_root=tmp_path) as adapter:
        rows = adapter.fetch_page(offset=1, limit=1, sort_by="a")
        assert rows == [[2, "y"]]
        assert adapter.count().value == 3
    clear_caches()
