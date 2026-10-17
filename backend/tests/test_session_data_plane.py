"""Phase 3 session-plane routing, invalidation, and exact-join tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import pytest

from agents.state import AgentState, DataContext
from datasources.contracts import DataScope
from execution.duckdb import duckdb_kernel_manager
from execution.joins import execute_exact_snapshot_join
from execution.session_plane import PlaneKind, resolve_data_plane
from viz.catalog import get_field_catalog


class _WorkspaceManager:
    root = None

    def get_or_create(self, session_id, user_id=None):
        path = type(self).root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


@pytest.fixture()
def state_factory(tmp_path, monkeypatch):
    import execution.session_plane as session_plane

    _WorkspaceManager.root = tmp_path
    monkeypatch.setattr(session_plane, "WorkspaceManager", _WorkspaceManager)
    session_ids: list[str] = []

    def make(session_id: str, frame: pd.DataFrame, source: str = "upload"):
        metadata = {} if source == "upload" else {"source": source}
        if source == "postgres":
            metadata.update({"connection_url": "postgresql://example/test", "table": "orders"})
        state = AgentState(session_id=session_id, metadata={"user_id": "test-user"})
        state.set_data_context(
            DataContext(
                file_id=f"{source}-1",
                filename=f"{source}.csv",
                raw_dataframe=frame.copy(deep=True),
                working_dataframe=frame.copy(deep=True),
                uploaded_at=datetime.now(),
                metadata=metadata,
            )
        )
        session_ids.append(session_id)
        return state

    yield make
    for session_id in session_ids:
        duckdb_kernel_manager.close_session(session_id)


def test_upload_materializes_once_and_invalidates_on_frame_replacement(state_factory):
    state = state_factory("plane-upload", pd.DataFrame({"id": [1, 2], "value": [10, 20]}))
    first = resolve_data_plane(state)
    second = resolve_data_plane(state)

    assert first.kind == PlaneKind.DUCKDB
    assert first.scope == DataScope.FULL
    assert first.row_limit is None and first.truncated is False
    assert second.relation is first.relation
    assert state.data_context.metadata["analytical_snapshot"]["engine"] == "duckdb"

    state.data_context.working_dataframe = pd.DataFrame({"id": [3], "value": [99]})
    replaced = resolve_data_plane(state)
    assert replaced.snapshot_token != first.snapshot_token
    assert replaced.kernel.query(f'SELECT * FROM "{replaced.relation_name}"').table.to_pydict() == {
        "id": [3],
        "value": [99],
    }


def test_in_place_frame_edit_invalidates_the_snapshot(state_factory):
    state = state_factory("plane-in-place", pd.DataFrame({"id": [1], "value": [10]}))
    first = resolve_data_plane(state)
    state.data_context.working_dataframe.loc[0, "value"] = 99
    replaced = resolve_data_plane(state)
    assert replaced.snapshot_token != first.snapshot_token
    assert replaced.kernel.query(f'SELECT value FROM "{replaced.relation_name}"').table[
        "value"
    ].to_pylist() == [99]


def test_concurrent_first_access_materializes_one_relation(state_factory):
    state = state_factory("plane-concurrent", pd.DataFrame({"id": range(100)}))
    with ThreadPoolExecutor(max_workers=6) as pool:
        planes = list(pool.map(lambda _index: resolve_data_plane(state), range(12)))
    assert len({plane.relation_name for plane in planes}) == 1
    assert len(planes[0].kernel.relations()) == 1


async def test_mixed_document_values_are_normalized_only_when_arrow_requires_it(state_factory):
    frame = pd.DataFrame({"id": [1, 2, 3], "mixed": [["a"], "plain", {"k": 1}]})
    state = state_factory("plane-mixed", frame)
    plane = resolve_data_plane(state)
    values = (
        plane.kernel.query(f'SELECT mixed FROM "{plane.relation_name}" ORDER BY id')
        .table["mixed"]
        .to_pylist()
    )
    assert values == ['["a"]', "plain", '{"k": 1}']
    assert state.data_context.metadata["analytical_snapshot"]["normalized_columns"] == ["mixed"]
    catalog = await get_field_catalog(state)
    assert catalog.get("mixed").db_type == "string"


@pytest.mark.parametrize(
    ("source", "schema_complete"),
    [
        ("sqlserver", True),
        ("mongodb", False),
        ("snowflake", True),
        ("redshift", True),
        ("bigquery", True),
    ],
)
def test_current_remote_connectors_require_a_live_connection(
    state_factory, source, schema_complete
):
    del schema_complete
    state = state_factory(f"plane-{source}", pd.DataFrame({"id": [1, 2]}), source)
    with pytest.raises(ValueError, match="Reconnect"):
        resolve_data_plane(state)


def test_databricks_with_live_credentials_uses_the_source_plane(state_factory):
    from datasources.warehouse import encode_connection_config

    state = state_factory("plane-databricks", pd.DataFrame({"id": [1, 2]}), "databricks")
    state.data_context.metadata["connection_config"] = encode_connection_config(
        {
            "server_hostname": "adb.example.net",
            "http_path": "/sql/1.0/warehouses/abc",
            "access_token": "dapi-secret",
            "catalog": "main",
            "scratch_catalog": "main",
            "scratch_schema": "stych_temp",
        }
    )
    native = resolve_data_plane(state)
    assert native.kind == PlaneKind.SOURCE
    assert native.scope == DataScope.FULL
    with pytest.raises(ValueError, match="isn't available yet"):
        resolve_data_plane(state, snapshot_postgres=True)


def test_databricks_without_restored_credentials_requires_reconnect(state_factory):
    state = state_factory("plane-databricks-restored", pd.DataFrame({"id": [1]}), "databricks")
    with pytest.raises(ValueError, match="Reconnect"):
        resolve_data_plane(state)


def test_postgres_stays_native_and_refuses_a_local_snapshot(state_factory):
    state = state_factory("plane-postgres", pd.DataFrame({"id": [1, 2]}), "postgres")
    native = resolve_data_plane(state)
    assert native.kind == PlaneKind.SOURCE
    assert native.scope == DataScope.FULL
    with pytest.raises(ValueError, match="isn't available yet"):
        resolve_data_plane(state, snapshot_postgres=True)


def test_postgres_without_restored_credentials_requires_reconnect(state_factory):
    state = state_factory("plane-postgres-restored", pd.DataFrame({"id": [1]}), "postgres")
    state.data_context.metadata.pop("connection_url")
    with pytest.raises(ValueError, match="Reconnect"):
        resolve_data_plane(state)


def test_exact_cross_source_join_is_unavailable(state_factory):
    state = state_factory(
        "plane-join",
        pd.DataFrame({"id": [1, 2], "value": ["left-a", "left-b"]}),
    )
    mongo = DataContext(
        file_id="mongo-2",
        filename="mongo.csv",
        raw_dataframe=pd.DataFrame({"key": [2, 3], "value": ["right-b", "right-c"]}),
        working_dataframe=pd.DataFrame({"key": [2, 3], "value": ["right-b", "right-c"]}),
        uploaded_at=datetime.now(),
        metadata={"source": "mongodb"},
    )
    state.add_data_context(mongo)

    result = execute_exact_snapshot_join(
        state,
        [
            {
                "left": "upload.csv",
                "right": "mongo.csv",
                "left_on": "id",
                "right_on": "key",
                "join_type": "LEFT",
                "strategy": "exact",
            }
        ],
    )

    assert "isn't available yet" in result["error"]


def test_file_join_quotes_ordinary_column_names(state_factory):
    state = state_factory(
        "plane-join-spaces",
        pd.DataFrame({"Order ID": [1, 2], "left val": ["a", "b"]}),
    )
    state.add_data_context(
        DataContext(
            file_id="right-spaces",
            filename="right.csv",
            raw_dataframe=pd.DataFrame({"Order ID": [2], "2024-total": [9]}),
            working_dataframe=pd.DataFrame({"Order ID": [2], "2024-total": [9]}),
            uploaded_at=datetime.now(),
        )
    )
    result = execute_exact_snapshot_join(
        state,
        [
            {
                "left": "upload.csv",
                "right": "right.csv",
                "left_on": "Order ID",
                "right_on": "Order ID",
                "join_type": "INNER",
            }
        ],
    )
    assert "error" not in result
    assert result["rows"] == 1
    assert result["data"]["left val"] == ["b"]
    assert result["data"]["2024-total"] == [9]


def test_exact_join_fails_loudly_at_the_result_safety_limit(state_factory):
    state = state_factory("plane-join-cap", pd.DataFrame({"id": [1, 1, 1]}))
    state.add_data_context(
        DataContext(
            file_id="upload-2",
            filename="second.csv",
            raw_dataframe=pd.DataFrame({"id": [1, 1, 1]}),
            working_dataframe=pd.DataFrame({"id": [1, 1, 1]}),
            uploaded_at=datetime.now(),
        )
    )
    result = execute_exact_snapshot_join(
        state,
        [{"left": "upload.csv", "right": "second.csv", "left_on": "id", "right_on": "id"}],
        max_rows=4,
    )
    assert "exceeded the 4-row safety limit" in result["error"]


def test_full_outer_join_coalesces_a_same_named_key(state_factory):
    state = state_factory("plane-full-join", pd.DataFrame({"id": [1], "left": ["a"]}))
    state.add_data_context(
        DataContext(
            file_id="right-2",
            filename="right.csv",
            raw_dataframe=pd.DataFrame({"id": [2], "right": ["b"]}),
            working_dataframe=pd.DataFrame({"id": [2], "right": ["b"]}),
            uploaded_at=datetime.now(),
        )
    )
    result = execute_exact_snapshot_join(
        state,
        [
            {
                "left": "upload.csv",
                "right": "right.csv",
                "left_on": "id",
                "right_on": "id",
                "join_type": "FULL OUTER",
            }
        ],
    )
    assert result["data"]["id"] == [1, 2]


def test_exact_join_does_not_match_null_keys_and_cleans_intermediate_relations(
    state_factory,
):
    state = state_factory(
        "plane-null-join", pd.DataFrame({"id": [None, 1], "left": ["null", "one"]})
    )
    state.add_data_context(
        DataContext(
            file_id="null-right",
            filename="right.csv",
            raw_dataframe=pd.DataFrame({"id": [None, 2], "right": ["matched", "two"]}),
            working_dataframe=pd.DataFrame({"id": [None, 2], "right": ["matched", "two"]}),
            uploaded_at=datetime.now(),
        )
    )
    plan = [
        {
            "left": "upload.csv",
            "right": "right.csv",
            "left_on": "id",
            "right_on": "id",
            "join_type": "INNER",
        }
    ]
    first = execute_exact_snapshot_join(state, plan)
    assert first["rows"] == 0
    assert first["data"]["right"] == []
    kernel = resolve_data_plane(state).kernel
    assert len(kernel.relations()) == 2
    execute_exact_snapshot_join(state, plan)
    assert len(kernel.relations()) == 2


def test_exact_snapshot_join_honors_every_canonical_key(state_factory):
    state = state_factory(
        "plane-multi-key",
        pd.DataFrame(
            {
                "tenant_id": [1, 2],
                "customer_id": [10, 10],
                "left_value": ["a", "b"],
            }
        ),
    )
    state.add_data_context(
        DataContext(
            file_id="multi-right",
            filename="right.csv",
            raw_dataframe=pd.DataFrame(
                {
                    "tenant_id": [1, 2],
                    "customer_id": [10, 10],
                    "right_value": ["x", "y"],
                }
            ),
            working_dataframe=pd.DataFrame(
                {
                    "tenant_id": [1, 2],
                    "customer_id": [10, 10],
                    "right_value": ["x", "y"],
                }
            ),
            uploaded_at=datetime.now(),
        )
    )

    result = execute_exact_snapshot_join(
        state,
        [
            {
                "left": "upload.csv",
                "right": "right.csv",
                "keys": [
                    {"left": "tenant_id", "right": "tenant_id"},
                    {"left": "customer_id", "right": "customer_id"},
                ],
                "strategy": "exact",
                "join_type": "INNER",
            }
        ],
    )

    assert result["rows"] == 2
    assert result["data"]["right_value"] == ["x", "y"]
    assert len(result["step_log"][0]["keys"]) == 2


def test_exact_snapshot_star_plan_accumulates_the_prior_result(state_factory):
    state = state_factory("plane-star", pd.DataFrame({"id": [1], "a": ["a"]}))
    for file_id, filename, column in (
        ("star-b", "b.csv", "b"),
        ("star-c", "c.csv", "c"),
    ):
        frame = pd.DataFrame({"id": [1], column: [column]})
        state.add_data_context(
            DataContext(
                file_id=file_id,
                filename=filename,
                raw_dataframe=frame.copy(),
                working_dataframe=frame,
                uploaded_at=datetime.now(),
            )
        )

    result = execute_exact_snapshot_join(
        state,
        [
            {
                "left": "upload.csv",
                "right": "b.csv",
                "left_on": "id",
                "right_on": "id",
            },
            {
                "left": "upload.csv",
                "right": "c.csv",
                "left_on": "id",
                "right_on": "id",
            },
        ],
    )

    assert result["rows"] == 1
    assert set(result["columns"]) == {"id", "a", "b", "c"}


def test_snowflake_with_live_credentials_uses_the_source_plane(state_factory):
    """Snowflake graduated from the DuckDB overlay to source-side execution;
    with credentials it must resolve to the full live relation."""
    from datasources.warehouse import encode_connection_config

    state = state_factory("plane-snowflake", pd.DataFrame({"id": [1, 2]}), "snowflake")
    state.data_context.metadata["connection_config"] = encode_connection_config(
        {
            "account": "acme-xy123",
            "user": "stych",
            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
        }
    )
    native = resolve_data_plane(state)
    assert native.kind == PlaneKind.SOURCE
    assert native.scope == DataScope.FULL
    with pytest.raises(ValueError, match="isn't available yet"):
        resolve_data_plane(state, snapshot_postgres=True)


def test_bigquery_with_live_credentials_uses_the_source_plane(state_factory):
    from datasources.warehouse import encode_connection_config

    state = state_factory("plane-bigquery", pd.DataFrame({"id": [1, 2]}), "bigquery")
    state.data_context.metadata["connection_config"] = encode_connection_config(
        {
            "service_account_json": '{"type": "service_account", "project_id": "p"}',
            "project_id": "p",
        }
    )
    assert resolve_data_plane(state).kind == PlaneKind.SOURCE


def test_snowflake_without_restored_credentials_requires_reconnect(state_factory):
    """A restored session with redacted credentials must not claim full data."""
    state = state_factory("plane-snowflake-restored", pd.DataFrame({"id": [1]}), "snowflake")
    with pytest.raises(ValueError, match="Reconnect"):
        resolve_data_plane(state)
