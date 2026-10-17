"""Cloud warehouse API orchestration tests without external credentials."""

from __future__ import annotations

import json

from api import cloud_datasource_routes as routes
from api import remote_source
from api.cloud_datasource_schemas import (
    RedshiftConnectionParams,
    RedshiftLoadRequest,
    SnowflakeConnectionParams,
    SnowflakeLoadRequest,
)
from datasources.adapters import AdapterColumn

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"


class _Session:
    def __init__(self):
        self.contexts = []

    def add_data_context(self, context):
        self.contexts.append(context)

    def get_all_data_contexts(self):
        return list(self.contexts)

    @property
    def dataset_count(self):
        return len(self.contexts)


class _StateManager:
    def __init__(self):
        self.session = _Session()
        self.dirty_sessions = []
        self.snapshots = []

    def get_or_create_session(self, session_id, app_user_id=None):
        return self.session

    def mark_dirty(self, session_id):
        self.dirty_sessions.append(session_id)

    async def snapshot_data_async(self, session):
        self.snapshots.append(session)


async def test_snowflake_load_records_encrypted_ready_snapshot_metadata(monkeypatch):
    manager = _StateManager()
    monkeypatch.setattr(remote_source, "state_manager", manager)
    monkeypatch.setattr(remote_source, "get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        remote_source,
        "_probe_source",
        lambda context: ([AdapterColumn("id", "NUMBER", "number")], 2),
    )
    request = SnowflakeLoadRequest(
        session_id="session-1",
        connection=SnowflakeConnectionParams(
            account="org-account",
            user="analyst",
            private_key=_PEM,
            warehouse="COMPUTE_WH",
            database="DB",
        ),
        schema="PUBLIC",
        table="T",
    )
    response = await routes.snowflake_load(
        request,
        config={"datasources": {"snowflake": {"enabled": True}}},
        user="user-1",
    )

    assert response.source_type == "snowflake"
    context = manager.session.contexts[0]
    assert context.metadata["storage_mode"] == "remote_reference"
    assert context.raw_dataframe is context.working_dataframe
    assert context.working_dataframe.empty
    stored = json.loads(context.metadata["connection_config"])
    assert stored["private_key"] == _PEM
    assert "password" not in stored
    assert "BEGIN PRIVATE KEY" not in response.message
    assert manager.dirty_sessions == ["session-1"]
    assert manager.snapshots == [manager.session]


async def test_redshift_load_records_duckdb_snapshot_scope(monkeypatch):
    manager = _StateManager()
    monkeypatch.setattr(remote_source, "state_manager", manager)
    monkeypatch.setattr(remote_source, "get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        remote_source,
        "_probe_source",
        lambda context: ([AdapterColumn("id", "BIGINT", "number")], 1),
    )
    request = RedshiftLoadRequest(
        session_id="session-2",
        connection=RedshiftConnectionParams(
            host="cluster.example",
            database="dev",
            user="analyst",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret-access-key",
            cluster_identifier="analytics",
        ),
        schema="analytics",
        table="events",
    )
    response = await routes.redshift_load(
        request,
        config={"datasources": {"redshift": {"enabled": True}}},
        user="user-1",
    )

    assert response.source_type == "redshift"
    context = manager.session.contexts[0]
    assert context.metadata["source"] == "redshift"
    assert context.metadata["storage_mode"] == "remote_reference"
    assert context.metadata["database"] == "dev"
    assert manager.dirty_sessions == ["session-2"]
    assert manager.snapshots == [manager.session]


_SA = (
    '{"type":"service_account","project_id":"demo-proj","private_key":'
    '"-----BEGIN PRIVATE KEY-----\\nMIIB\\n-----END PRIVATE KEY-----\\n",'
    '"client_email":"stych@demo-proj.iam.gserviceaccount.com"}'
)


async def test_bigquery_load_records_project_as_database(monkeypatch):
    from api.cloud_datasource_schemas import BigQueryConnectionParams, BigQueryLoadRequest

    manager = _StateManager()
    monkeypatch.setattr(remote_source, "state_manager", manager)
    monkeypatch.setattr(remote_source, "get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        remote_source,
        "_probe_source",
        lambda context: ([AdapterColumn("id", "INT64", "number")], 1),
    )
    request = BigQueryLoadRequest(
        session_id="session-3",
        connection=BigQueryConnectionParams(service_account_json=_SA),
        schema="analytics",
        table="events",
    )
    response = await routes.bigquery_load(
        request,
        config={"datasources": {"bigquery": {"enabled": True}}},
        user="user-1",
        db=None,
    )
    assert response.source_type == "bigquery"
    context = manager.session.contexts[0]
    stored = json.loads(context.metadata["connection_config"])
    assert context.metadata["database"] == "demo-proj"
    assert stored["project_id"] == "demo-proj"
    assert "private_key" in stored["service_account_json"]
    assert "BEGIN PRIVATE KEY" not in response.message
    assert manager.dirty_sessions == ["session-3"]
    assert manager.snapshots == [manager.session]


async def test_databricks_load_records_catalog_as_database(monkeypatch):
    from api.cloud_datasource_schemas import DatabricksConnectionParams, DatabricksLoadRequest

    manager = _StateManager()
    monkeypatch.setattr(remote_source, "state_manager", manager)
    monkeypatch.setattr(remote_source, "get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        remote_source,
        "_probe_source",
        lambda context: ([AdapterColumn("id", "BIGINT", "number")], 1),
    )
    request = DatabricksLoadRequest(
        session_id="session-4",
        connection=DatabricksConnectionParams(
            server_hostname="adb-123.azuredatabricks.net",
            http_path="/sql/1.0/warehouses/abc",
            access_token="dapi-secret",
            catalog="main",
        ),
        schema="gold",
        table="orders",
    )
    response = await routes.databricks_load(
        request,
        config={"datasources": {"databricks": {"enabled": True}}},
        user="user-1",
    )
    assert response.source_type == "databricks"
    context = manager.session.contexts[0]
    stored = json.loads(context.metadata["connection_config"])
    assert context.metadata["database"] == "main"
    assert context.metadata["scratch_schema"] == "stych_temp"
    assert context.metadata["scratch_catalog"] == "main"
    assert stored["scratch_schema"] == "stych_temp"
    assert stored["scratch_catalog"] == "main"
    assert stored["access_token"] == "dapi-secret"
    assert "dapi-secret" not in response.message
    assert manager.dirty_sessions == ["session-4"]
    assert manager.snapshots == [manager.session]


async def test_bigquery_oauth_load_hydrates_google_tokens(monkeypatch):
    from api.cloud_datasource_schemas import BigQueryConnectionParams, BigQueryLoadRequest

    manager = _StateManager()
    monkeypatch.setattr(remote_source, "state_manager", manager)
    monkeypatch.setattr(remote_source, "get_langfuse_tracker", lambda: None)
    monkeypatch.setattr(
        remote_source,
        "_probe_source",
        lambda context: ([AdapterColumn("id", "INT64", "number")], 1),
    )
    monkeypatch.setattr(
        routes,
        "_hydrate_bigquery",
        lambda connection, user, db: {
            **connection,
            "auth_mode": "oauth",
            "access_token": "ya29.token",
            "refresh_token": "1//refresh",
            "project_id": connection.get("project_id") or "demo-proj",
        },
    )
    request = BigQueryLoadRequest(
        session_id="session-oauth",
        connection=BigQueryConnectionParams(auth_mode="oauth", project_id="demo-proj"),
        schema="analytics",
        table="events",
    )
    response = await routes.bigquery_load(
        request,
        config={"datasources": {"bigquery": {"enabled": True}}},
        user="user-1",
        db=None,
    )
    assert response.source_type == "bigquery"
    stored = json.loads(manager.session.contexts[0].metadata["connection_config"])
    assert stored["auth_mode"] == "oauth"
    assert stored["refresh_token"] == "1//refresh"
    assert "ya29.token" not in response.message
