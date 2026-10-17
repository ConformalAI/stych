"""Conformance and safety tests for the session-scoped DuckDB kernel."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq
import pytest

from execution.duckdb import (
    DuckDBExecutionError,
    DuckDBInvalidQuery,
    DuckDBInvalidRelationName,
    DuckDBKernel,
    DuckDBKernelClosed,
    DuckDBKernelConfig,
    DuckDBKernelManager,
    DuckDBQueryTimeout,
    DuckDBRelationExists,
    DuckDBUnauthorizedPath,
    DuckDBUnsafeQuery,
    source_relation_name,
)


@pytest.fixture()
def kernel(tmp_path):
    instance = DuckDBKernel(
        "session-1",
        tmp_path / "duckdb_tmp",
        config=DuckDBKernelConfig(max_result_rows=3, threads=1),
    )
    yield instance
    instance.close()


def test_materialize_query_parameters_and_arrow_result(kernel):
    relation = kernel.materialize(
        "orders",
        pd.DataFrame(
            {
                "id": [1, 2, 3],
                "amount": [10.5, None, 30.0],
                "ordered_at": pd.to_datetime(["2026-01-01", "2026-01-02", None]),
            }
        ),
        source="upload:f1",
    )
    assert relation.kind == "table"
    assert relation.rows == 3
    assert [column.name for column in relation.columns] == ["id", "amount", "ordered_at"]

    result = kernel.query(
        "SELECT id, amount FROM orders WHERE id >= $minimum ORDER BY id",
        {"minimum": 2},
    )
    assert isinstance(result.table, pa.Table)
    assert result.table.to_pydict() == {"id": [2, 3], "amount": [None, 30.0]}
    assert result.columns == ("id", "amount")
    assert result.row_count == 2
    assert result.truncated is False
    assert result.elapsed_ms >= 0


def test_result_limit_is_enforced_after_user_query(kernel):
    result = kernel.query("SELECT * FROM range(100) AS t(i) ORDER BY i", max_rows=5)
    assert result.table["i"].to_pylist() == [0, 1, 2, 3, 4]
    assert result.truncated is True

    default_cap = kernel.query("SELECT * FROM range(100) AS t(i) ORDER BY i")
    assert default_cap.row_count == 3
    assert default_cap.truncated is True


def test_materialized_table_is_stable_when_input_frame_changes(kernel):
    frame = pd.DataFrame({"id": [1, 2], "value": ["original", "original"]})
    kernel.materialize("stable", frame)
    frame.loc[:, "value"] = "mutated"
    assert kernel.query("SELECT DISTINCT value FROM stable").table["value"].to_pylist() == [
        "original"
    ]


def test_registered_view_is_read_only_and_replace_is_explicit(kernel):
    frame = pd.DataFrame({"id": [1], "value": ["first"]})
    relation = kernel.register_view("live_view", frame)
    assert relation.kind == "view"
    assert kernel.query("SELECT value FROM live_view").table["value"].to_pylist() == ["first"]

    with pytest.raises(DuckDBRelationExists):
        kernel.register_view("live_view", frame)
    replacement = pd.DataFrame({"id": [2], "value": ["replacement"]})
    kernel.register_view("live_view", replacement, replace=True)
    assert kernel.query("SELECT id FROM live_view").table["id"].to_pylist() == [2]


def test_parquet_registration(kernel, tmp_path):
    path = tmp_path / "events.parquet"
    pq.write_table(pa.table({"id": [1, 2], "tags": [["a", "b"], None]}), path)

    view = kernel.register_parquet("events_view", path)
    assert view.kind == "view"
    assert kernel.query("SELECT tags[1] AS first_tag FROM events_view ORDER BY id").table[
        "first_tag"
    ].to_pylist() == ["a", None]


def test_file_backed_inputs_cannot_escape_the_session_workspace(kernel, tmp_path):
    kernel.register_view("memory_dataset", pads.dataset(pa.table({"safe": [True]})))
    assert kernel.query("SELECT safe FROM memory_dataset").table["safe"].to_pylist() == [True]

    outside_dir = tmp_path.parent / "other-tenant"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "private.parquet"
    pq.write_table(pa.table({"secret": ["other-user"]}), outside)

    with pytest.raises(DuckDBUnauthorizedPath):
        kernel.register_parquet("outside", outside)
    with pytest.raises(DuckDBUnauthorizedPath):
        kernel.register_view("outside_dataset", pads.dataset(outside, format="parquet"))


def test_two_sources_can_be_joined_inside_one_kernel(kernel):
    kernel.materialize("customers", pa.table({"id": [1, 2], "name": ["A", "B"]}))
    kernel.materialize("orders", pa.table({"customer_id": [2, 2, 3], "amount": [5, 7, 9]}))
    result = kernel.query(
        """
        SELECT c.name, SUM(o.amount) AS total
        FROM customers c
        JOIN orders o ON o.customer_id = c.id
        GROUP BY c.name
        """
    )
    assert result.table.to_pydict() == {"name": ["B"], "total": [12]}


def test_arrow_record_batch_stream_can_be_materialized(kernel):
    first = pa.record_batch({"id": [1, 2], "value": ["a", "b"]})
    second = pa.record_batch({"id": [3], "value": ["c"]})
    reader = pa.RecordBatchReader.from_batches(first.schema, [first, second])
    relation = kernel.materialize("streamed", reader, source="connector:arrow-stream")
    assert relation.rows is None  # streaming cardinality is intentionally not pre-scanned
    assert kernel.query("SELECT * FROM streamed ORDER BY id").table.to_pydict() == {
        "id": [1, 2, 3],
        "value": ["a", "b", "c"],
    }


def test_failed_replacements_preserve_existing_relations(kernel):
    kernel.materialize("stable_table", pa.table({"value": ["original"]}))
    kernel.register_view("stable_view", pa.table({"value": ["original"]}))

    with pytest.raises(DuckDBExecutionError):
        kernel.materialize("stable_table", object(), replace=True)  # type: ignore[arg-type]
    with pytest.raises(DuckDBExecutionError):
        kernel.register_view("stable_view", object(), replace=True)  # type: ignore[arg-type]

    assert kernel.query("SELECT value FROM stable_table").table["value"].to_pylist() == ["original"]
    assert kernel.query("SELECT value FROM stable_view").table["value"].to_pylist() == ["original"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO anything VALUES (1)",
        "CREATE TABLE unsafe AS SELECT 1",
        "SET threads = 8",
        "ATTACH 'other.duckdb' AS other",
        "INSTALL httpfs",
        "SELECT 1; SELECT 2",
    ],
)
def test_only_one_read_only_statement_is_allowed(kernel, sql):
    with pytest.raises(DuckDBUnsafeQuery):
        kernel.query(sql)


def test_empty_and_invalid_sql_have_typed_errors(kernel):
    with pytest.raises(DuckDBInvalidQuery, match="empty"):
        kernel.query("  ")
    with pytest.raises(DuckDBInvalidQuery, match="parse"):
        kernel.query("SELECT FROM")


def test_select_cannot_read_files_or_auto_load_extensions(kernel):
    with pytest.raises(DuckDBExecutionError, match="Cannot access file"):
        kernel.query("SELECT * FROM read_csv_auto('/etc/passwd')")
    settings = kernel.query(
        """
        SELECT
          current_setting('enable_external_access') AS external_access,
          current_setting('autoinstall_known_extensions') AS autoinstall,
          current_setting('autoload_known_extensions') AS autoload,
          current_setting('lock_configuration') AS locked
        """
    ).table.to_pydict()
    assert settings == {
        "external_access": [False],
        "autoinstall": [False],
        "autoload": [False],
        "locked": [True],
    }


def test_query_timeout_interrupts_work_and_kernel_recovers(kernel):
    with pytest.raises(DuckDBQueryTimeout):
        kernel.query("SELECT SUM(SIN(i)) FROM range(1000000000) AS t(i)", timeout_seconds=0.01)
    assert kernel.query("SELECT 42 AS answer").table["answer"].to_pylist() == [42]


def test_connection_access_is_serialized_across_threads(kernel):
    kernel.materialize("numbers", pa.table({"i": list(range(100))}))

    def total(modulus: int) -> int:
        result = kernel.query(
            "SELECT SUM(i) AS total FROM numbers WHERE i % $modulus = 0",
            {"modulus": modulus},
        )
        return result.table["total"][0].as_py()

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(total, [2, 3, 4, 5, 6, 7, 8, 9]))
    assert values == [2450, 1683, 1200, 950, 816, 735, 624, 594]


def test_relation_removal_close_and_context_manager(tmp_path):
    with DuckDBKernel("s", tmp_path / "tmp") as instance:
        instance.materialize("data", pa.table({"x": [1]}))
        assert [relation.name for relation in instance.relations()] == ["data"]
        assert instance.remove_relation("data") is True
        assert instance.remove_relation("data") is False
    assert instance.closed is True
    with pytest.raises(DuckDBKernelClosed):
        instance.query("SELECT 1")


def test_manager_isolates_tenants_and_closes_lifecycles(tmp_path):
    manager = DuckDBKernelManager()
    a = manager.get_or_create("same-session", "user-a", workspace_root=tmp_path / "a")
    b = manager.get_or_create("same-session", "user-b", workspace_root=tmp_path / "b")
    assert a is manager.get_or_create("same-session", "user-a", workspace_root=tmp_path / "a")
    assert a is not b
    a.materialize("private_data", pa.table({"x": [1]}))
    with pytest.raises(DuckDBExecutionError, match="private_data"):
        b.query("SELECT * FROM private_data")
    assert manager.active_count == 2
    assert manager.close_session("same-session", "user-a") == 1
    assert a.closed and not b.closed
    assert manager.close_all() == 1
    assert manager.active_count == 0


def test_manager_close_interrupts_active_query(tmp_path):
    manager = DuckDBKernelManager()
    instance = manager.get_or_create(
        "busy-session",
        "user",
        workspace_root=tmp_path,
        config=DuckDBKernelConfig(query_timeout_seconds=10),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            instance.query,
            "SELECT SUM(SIN(i)) FROM range(1000000000) AS t(i)",
        )
        time.sleep(0.05)
        started = time.monotonic()
        assert manager.close_session("busy-session", "user") == 1
        assert time.monotonic() - started < 1
        with pytest.raises(DuckDBExecutionError):
            future.result()
    assert instance.closed


def test_manager_close_all_continues_after_one_kernel_fails():
    manager = DuckDBKernelManager()
    failing = Mock(session_id="failing", closed=False)
    failing.close.side_effect = RuntimeError("close failed")
    healthy = Mock(session_id="healthy", closed=False)
    manager._kernels = {("u", "failing"): failing, ("u", "healthy"): healthy}

    assert manager.close_all() == 2

    failing.close.assert_called_once()
    healthy.close.assert_called_once()
    assert manager.active_count == 0


def test_relation_names_are_validated_and_source_names_are_stable(kernel):
    with pytest.raises(DuckDBInvalidRelationName):
        kernel.materialize("bad-name; DROP TABLE x", pa.table({"x": [1]}))
    first = source_relation_name("postgres:public.orders:abc", "Public Orders")
    assert first == source_relation_name("postgres:public.orders:abc", "Public Orders")
    assert first != source_relation_name("postgres:public.orders:def", "Public Orders")
    assert first.startswith("src_public_orders_")


@pytest.mark.parametrize(
    "config",
    [
        DuckDBKernelConfig(threads=0),
        DuckDBKernelConfig(max_result_rows=0),
        DuckDBKernelConfig(memory_limit=""),
        DuckDBKernelConfig(max_temp_directory_size=""),
        DuckDBKernelConfig(query_timeout_seconds=0),
        DuckDBKernelConfig(ingest_timeout_seconds=0),
    ],
)
def test_invalid_resource_configuration_fails_before_open(tmp_path, config):
    with pytest.raises(ValueError):
        DuckDBKernel("s", tmp_path / "tmp", config=config)


async def test_workspace_archiving_excludes_duckdb_spill_files(tmp_path):
    from workspace.manager import WorkspaceManager, list_workspace_artifacts

    manager = WorkspaceManager(base_dir=str(tmp_path))
    workspace = manager.get_or_create("s1", user_id="u1")
    (workspace / "visible.csv").write_text("x\n1\n")
    spill = workspace / "duckdb_tmp"
    spill.mkdir()
    (spill / "spill.tmp").write_bytes(b"internal")

    assert [item["name"] for item in list_workspace_artifacts(workspace)] == ["visible.csv"]

    class Storage:
        def __init__(self):
            self.saved = []

        def save_workspace_file(self, user_id, session_id, name, content):
            self.saved.append(name)

    storage = Storage()
    assert await manager.archive_to_storage("s1", "u1", storage) == 1
    assert storage.saved == ["visible.csv"]
