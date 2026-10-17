"""Export planning, capping, and streaming encoders."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pandas as pd
import pytest

from api import export_service
from datasources.adapters.base import AdapterColumn, SourceCacheInfo, TableAdapter
from datasources.contracts import DataScope


class _FakeAdapter(TableAdapter):
    """Paging adapter with no streaming override — exercises the shared default."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.cache_info = SourceCacheInfo(key=("fake",))
        self.pages = 0

    def schema(self):
        return [AdapterColumn(name=str(c), db_type="text", kind="string") for c in self.frame]

    def count(self, filters=None, search=None):
        from datasources.adapters.base import CountResult

        return CountResult(len(self.frame))

    def fetch_page(self, *, offset, limit, **_):
        self.pages += 1
        return self.frame.iloc[offset : offset + limit].values.tolist()

    def fetch_sample(self, columns=None, limit=10_000):
        return self.frame.head(limit)


def _ctx(source: str, rows: int = 3, **meta):
    frame = pd.DataFrame({"a": range(rows), "b": [f"r{i}" for i in range(rows)]})
    return SimpleNamespace(
        filename=f"{source} table",
        dataframe=frame,
        working_dataframe=frame,
        metadata={"source": source, **meta},
    )


# -- encoders ---------------------------------------------------------------


def test_csv_writes_one_header_across_batches():
    batches = [pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [3]})]
    out = b"".join(export_service.csv_chunks(iter(batches))).decode()
    assert out.count("a\n") == 1
    assert out.splitlines() == ["a", "1", "2", "3"]


def test_parquet_streams_forward_only_and_round_trips():
    batches = [
        pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}),
        pd.DataFrame({"a": [4, 5], "b": ["p", "q"]}),
    ]
    data = b"".join(export_service.parquet_chunks(iter(batches)))
    back = pd.read_parquet(io.BytesIO(data))
    assert back.shape == (5, 2)
    assert list(back["a"]) == [1, 2, 3, 4, 5]


def test_parquet_emits_a_valid_file_for_an_empty_relation():
    data = b"".join(export_service.parquet_chunks(iter([])))
    assert pd.read_parquet(io.BytesIO(data)).empty


def test_unknown_format_is_rejected():
    with pytest.raises(export_service.ExportError):
        list(export_service.encode(iter([]), "xlsx"))


# -- planning ---------------------------------------------------------------


def test_full_scope_connector_plans_a_source_stream():
    ctx = _ctx("postgres", connection_url="postgresql://u:p@h:5432/db")
    plan = export_service.plan_export(ctx, {})
    assert plan.streamed is True
    assert plan.scope == DataScope.FULL


def test_sample_scope_connector_falls_back_to_the_frame(monkeypatch):
    """Every shipped connector now exports at full scope, so the sample branch
    has no live connector to exercise it. The rule still holds if one is ever
    added, so the scope is forced rather than the coverage dropped."""
    import datasources.registry as registry
    from datasources.contracts import Capability

    manifest = registry.get_manifest("mongodb")
    support = manifest.capabilities[Capability.EXPORT_SNAPSHOT]
    monkeypatch.setitem(
        manifest.capabilities,
        Capability.EXPORT_SNAPSHOT,
        support.model_copy(update={"scope": DataScope.SAMPLE}),
    )
    plan = export_service.plan_export(_ctx("mongodb"), {})
    assert plan.streamed is False
    assert plan.scope == DataScope.SAMPLE


def test_uploads_export_from_the_frame():
    assert export_service.plan_export(_ctx("upload"), {}).streamed is False


def test_missing_credentials_fail_loudly_instead_of_exporting_a_stale_sample():
    """A full-scope connector whose creds were redacted must not silently
    hand back the 10K connect-time frame as if it were the table."""
    ctx = _ctx("postgres", connection_url="[REDACTED]")
    with pytest.raises(export_service.ExportError, match="reconnect"):
        export_service.plan_export(ctx, {})


def test_row_cap_prefers_env_then_config(monkeypatch):
    monkeypatch.delenv("EXPORT_MAX_ROWS", raising=False)
    assert export_service.export_max_rows({}) == export_service.DEFAULT_EXPORT_MAX_ROWS
    assert export_service.export_max_rows({"datasources": {"export": {"max_rows": 7}}}) == 7
    monkeypatch.setenv("EXPORT_MAX_ROWS", "11")
    assert export_service.export_max_rows({"datasources": {"export": {"max_rows": 7}}}) == 11


def test_invalid_row_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("EXPORT_MAX_ROWS", "not-a-number")
    assert export_service.export_max_rows({}) == export_service.DEFAULT_EXPORT_MAX_ROWS


# -- batching ---------------------------------------------------------------


def test_default_paging_scan_covers_the_relation_without_overlap():
    frame = pd.DataFrame({"a": range(250), "b": range(250)})
    adapter = _FakeAdapter(frame)
    rows = pd.concat(list(adapter.stream_batches(batch_rows=100)), ignore_index=True)
    assert list(rows["a"]) == list(range(250))
    assert adapter.pages == 3


def test_paging_scan_stops_at_max_rows():
    adapter = _FakeAdapter(pd.DataFrame({"a": range(250)}))
    rows = pd.concat(list(adapter.stream_batches(batch_rows=100, max_rows=120)), ignore_index=True)
    assert len(rows) == 120


def test_paging_scan_projects_requested_columns():
    adapter = _FakeAdapter(pd.DataFrame({"a": range(10), "b": range(10)}))
    batch = next(iter(adapter.stream_batches(columns=["b"], batch_rows=5)))
    assert batch.columns.tolist() == ["b"]


def test_frame_batches_respect_the_cap():
    frame = pd.DataFrame({"a": range(50)})
    out = list(export_service.iter_frame_batches(frame, row_cap=10))
    assert len(out) == 1 and len(out[0]) == 10
