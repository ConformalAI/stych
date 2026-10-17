"""Auth contracts for cloud warehouse connectors (no database passwords)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.cloud_datasource_schemas import RedshiftConnectionParams, SnowflakeConnectionParams
from datasources import redshift as redshift_source
from datasources import snowflake as snowflake_source
from datasources.redshift import RedshiftLoadError
from datasources.snowflake import SnowflakeLoadError


def _rsa_pem(*, passphrase: str | None = None) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    ).decode()


def test_snowflake_connect_kwargs_use_jwt_and_never_forward_password():
    pem = _rsa_pem()
    kwargs = snowflake_source.connect_kwargs(
        {
            "account": "org-account",
            "user": "analyst",
            "private_key": pem,
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
            "password": "should-never-be-sent",
        }
    )
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    assert isinstance(kwargs["private_key"], bytes)
    assert kwargs["account"] == "org-account"
    assert kwargs["user"] == "analyst"
    assert "password" not in kwargs
    assert "should-never-be-sent" not in str(kwargs)


def test_snowflake_encrypted_pem_requires_matching_passphrase():
    pem = _rsa_pem(passphrase="correct-horse")
    kwargs = snowflake_source.connect_kwargs(
        {
            "account": "org-account",
            "user": "analyst",
            "private_key": pem,
            "private_key_passphrase": "correct-horse",
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
        }
    )
    assert kwargs["authenticator"] == "SNOWFLAKE_JWT"
    with pytest.raises(SnowflakeLoadError, match="Invalid Snowflake private key"):
        snowflake_source.connect_kwargs(
            {
                "account": "org-account",
                "user": "analyst",
                "private_key": pem,
                "warehouse": "COMPUTE_WH",
                "database": "ANALYTICS",
            }
        )


def test_snowflake_schema_rejects_password_and_non_pem_keys():
    with pytest.raises(ValidationError, match="password"):
        SnowflakeConnectionParams(
            account="org-account",
            user="analyst",
            password="secret",
            warehouse="COMPUTE_WH",
            database="ANALYTICS",
        )
    with pytest.raises(ValidationError, match="PEM"):
        SnowflakeConnectionParams(
            account="org-account",
            user="analyst",
            private_key="not-a-key",
            warehouse="COMPUTE_WH",
            database="ANALYTICS",
        )


def test_redshift_connect_kwargs_use_iam_and_infer_cluster_from_host():
    kwargs = redshift_source.connect_kwargs(
        {
            "host": "analytics.abc123xyz.us-west-2.redshift.amazonaws.com",
            "database": "dev",
            "user": "analyst",
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "secret-access-key",
            "password": "should-never-be-sent",
        }
    )
    assert kwargs["iam"] is True
    assert kwargs["ssl"] is True
    assert kwargs["db_user"] == "analyst"
    assert kwargs["cluster_identifier"] == "analytics"
    assert kwargs["region"] == "us-west-2"
    assert kwargs["access_key_id"] == "AKIAEXAMPLE"
    assert "password" not in kwargs
    assert "user" not in kwargs


def test_redshift_serverless_host_sets_workgroup_not_cluster():
    kwargs = redshift_source.connect_kwargs(
        {
            "host": "default.123456789012.us-east-1.redshift-serverless.amazonaws.com",
            "database": "dev",
            "user": "analyst",
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "secret-access-key",
        }
    )
    assert kwargs["is_serverless"] is True
    assert kwargs["serverless_work_group"] == "default"
    assert kwargs["serverless_acct_id"] == "123456789012"
    assert kwargs["region"] == "us-east-1"
    assert "cluster_identifier" not in kwargs


def test_redshift_custom_host_requires_cluster_identifier():
    with pytest.raises(RedshiftLoadError, match="cluster_identifier"):
        redshift_source.connect_kwargs(
            {
                "host": "redshift.internal.example.com",
                "database": "dev",
                "user": "analyst",
                "access_key_id": "AKIAEXAMPLE",
                "secret_access_key": "secret-access-key",
            }
        )
    kwargs = redshift_source.connect_kwargs(
        {
            "host": "redshift.internal.example.com",
            "database": "dev",
            "user": "analyst",
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "secret-access-key",
            "cluster_identifier": "analytics",
            "region": "us-east-1",
        }
    )
    assert kwargs["cluster_identifier"] == "analytics"
    assert kwargs["region"] == "us-east-1"


def test_redshift_schema_rejects_password():
    with pytest.raises(ValidationError, match="password"):
        RedshiftConnectionParams(
            host="cluster.example",
            database="dev",
            user="analyst",
            password="secret",
        )


def test_snowflake_connect_forwards_der_key_to_driver(monkeypatch):
    captured: dict = {}

    class _Conn:
        def close(self):
            return None

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _Conn()

    monkeypatch.setattr("snowflake.connector.connect", fake_connect)
    pem = _rsa_pem()
    snowflake_source._connect(
        {
            "account": "org-account",
            "user": "analyst",
            "private_key": pem,
            "warehouse": "COMPUTE_WH",
            "database": "ANALYTICS",
        }
    )
    assert captured["authenticator"] == "SNOWFLAKE_JWT"
    assert isinstance(captured["private_key"], bytes)
    assert "password" not in captured


def test_redshift_connect_forwards_iam_keys_to_driver(monkeypatch):
    captured: dict = {}

    class _Conn:
        def close(self):
            return None

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return _Conn()

    monkeypatch.setattr("redshift_connector.connect", fake_connect)
    redshift_source._connect(
        {
            "host": "analytics.abc123xyz.us-west-2.redshift.amazonaws.com",
            "database": "dev",
            "user": "analyst",
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "secret-access-key",
        }
    )
    assert captured["iam"] is True
    assert captured["db_user"] == "analyst"
    assert captured["access_key_id"] == "AKIAEXAMPLE"
    assert "password" not in captured
    assert "user" not in captured


_SA = (
    '{"type":"service_account","project_id":"demo-proj","private_key":'
    '"-----BEGIN PRIVATE KEY-----\\nMIIB\\n-----END PRIVATE KEY-----\\n",'
    '"client_email":"stych@demo-proj.iam.gserviceaccount.com"}'
)


def test_bigquery_connect_kwargs_require_service_account_json():
    from api.cloud_datasource_schemas import BigQueryConnectionParams
    from datasources import bigquery as bigquery_source
    from datasources.bigquery import BigQueryLoadError

    kwargs = bigquery_source.connect_kwargs({"service_account_json": _SA})
    assert kwargs["project_id"] == "demo-proj"
    assert kwargs["service_account_info"]["client_email"].startswith("stych@")
    with pytest.raises(BigQueryLoadError, match="service_account_json"):
        bigquery_source.connect_kwargs({})
    with pytest.raises(ValidationError, match="service_account"):
        BigQueryConnectionParams(service_account_json='{"type":"user"}')
    with pytest.raises(ValidationError, match="password"):
        BigQueryConnectionParams(service_account_json=_SA, password="secret")
    oauth = BigQueryConnectionParams(auth_mode="oauth", project_id="demo-proj")
    assert oauth.auth_mode == "oauth"
    assert oauth.service_account_json is None
    kwargs = bigquery_source.connect_kwargs(
        {
            "auth_mode": "oauth",
            "access_token": "ya29.token",
            "refresh_token": "1//refresh",
            "project_id": "demo-proj",
        }
    )
    assert kwargs["auth_mode"] == "oauth"
    assert kwargs["access_token"] == "ya29.token"
    assert "password" not in kwargs
    with pytest.raises(BigQueryLoadError, match="Google account"):
        bigquery_source.connect_kwargs({"auth_mode": "oauth"})


def test_databricks_connect_kwargs_use_pat_and_strip_host_scheme():
    from api.cloud_datasource_schemas import DatabricksConnectionParams
    from datasources import databricks as databricks_source
    from datasources.databricks import DatabricksLoadError

    kwargs = databricks_source.connect_kwargs(
        {
            "server_hostname": "https://adb-123.azuredatabricks.net/extra",
            "http_path": "sql/1.0/warehouses/abc",
            "access_token": "dapi-secret",
            "catalog": "main",
            "password": "should-never-be-sent",
        }
    )
    assert kwargs["server_hostname"] == "adb-123.azuredatabricks.net"
    assert kwargs["http_path"] == "/sql/1.0/warehouses/abc"
    assert kwargs["access_token"] == "dapi-secret"
    assert kwargs["catalog"] == "main"
    assert "password" not in kwargs
    with pytest.raises(DatabricksLoadError, match="access_token"):
        databricks_source.connect_kwargs(
            {
                "server_hostname": "adb-123.azuredatabricks.net",
                "http_path": "/sql/1.0/warehouses/abc",
                "catalog": "main",
            }
        )
    parsed = DatabricksConnectionParams(
        server_hostname="https://adb-123.azuredatabricks.net",
        http_path="sql/1.0/warehouses/abc",
        access_token="dapi-secret",
        catalog="main",
    )
    assert parsed.server_hostname == "adb-123.azuredatabricks.net"
    assert parsed.http_path == "/sql/1.0/warehouses/abc"
    with pytest.raises(ValidationError, match="password"):
        DatabricksConnectionParams(
            server_hostname="host",
            http_path="/sql",
            access_token="tok",
            catalog="main",
            password="secret",
        )


def test_bigquery_query_uses_positional_params_and_bytes_cap(monkeypatch):
    from types import SimpleNamespace

    from datasources import bigquery as bigquery_source

    captured: dict = {}

    class FakeJob:
        def result(self, timeout=None):
            captured["result_timeout"] = timeout

            class FakeResult:
                schema = [SimpleNamespace(name="id")]

                def __iter__(self):
                    return iter([(1,)])

            return FakeResult()

    class FakeClient:
        def query(self, statement, job_config=None):
            captured["statement"] = statement
            captured["job_config"] = job_config
            return FakeJob()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(bigquery_source, "_client", lambda config: FakeClient())
    names, rows = bigquery_source.run_bigquery_query(
        {"service_account_json": _SA}, "SELECT ?", (2,)
    )
    assert names == ["id"]
    assert rows == [(1,)]
    assert captured["statement"] == "SELECT ?"
    assert captured["job_config"].use_legacy_sql is False
    assert captured["job_config"].maximum_bytes_billed == bigquery_source.DEFAULT_MAX_BYTES_BILLED
    assert (
        int(captured["job_config"].job_timeout_ms) == bigquery_source.QUERY_TIMEOUT_SECONDS * 1000
    )
    assert captured["result_timeout"] == bigquery_source.QUERY_TIMEOUT_SECONDS
    assert captured["job_config"].query_parameters[0].value == 2
    assert captured["closed"] is True


def test_bigquery_bytes_cap_and_discovery_timeout_come_from_config(monkeypatch):
    """A connection can lower the scan cap; discovery probes use the short budget."""
    from types import SimpleNamespace

    from datasources import bigquery as bigquery_source

    captured: dict = {}

    class FakeResult:
        schema = [SimpleNamespace(name="x")]

        def __iter__(self):
            return iter([(1,)])

    class FakeJob:
        def result(self, timeout=None):
            captured["result_timeout"] = timeout
            return FakeResult()

    class FakeClient:
        def query(self, statement, job_config=None):
            captured["job_config"] = job_config
            return FakeJob()

        def close(self):
            pass

    monkeypatch.setattr(bigquery_source, "_client", lambda config: FakeClient())
    ok, _ = bigquery_source.test_bigquery_connection(
        {"service_account_json": _SA, "max_bytes_billed": 1024**3}
    )
    assert ok
    assert captured["job_config"].maximum_bytes_billed == 1024**3
    assert (
        int(captured["job_config"].job_timeout_ms)
        == bigquery_source.DISCOVERY_TIMEOUT_SECONDS * 1000
    )
    assert captured["result_timeout"] == bigquery_source.DISCOVERY_TIMEOUT_SECONDS

    # Garbage falls back to the default rather than lifting the cap.
    assert bigquery_source.max_bytes_billed({"max_bytes_billed": "lots"}) == (
        bigquery_source.DEFAULT_MAX_BYTES_BILLED
    )
    assert bigquery_source.max_bytes_billed({"max_bytes_billed": -1}) == (
        bigquery_source.DEFAULT_MAX_BYTES_BILLED
    )


def test_databricks_connect_forwards_pat_and_never_password(monkeypatch):
    from datasources import databricks as databricks_source

    captured: dict = {}

    class FakeCursor:
        description = [("1",)]

        def execute(self, statement, parameters=None):
            captured["statement"] = statement
            captured["parameters"] = parameters
            return self

        def fetchall(self):
            return [(1,)]

        def close(self):
            captured["cursor_closed"] = True

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["closed"] = True

    def fake_connect(**kwargs):
        captured["connect"] = kwargs
        return FakeConn()

    def _connect(config, *, socket_timeout=60):
        return fake_connect(
            **databricks_source.connect_kwargs(config, socket_timeout=socket_timeout)
        )

    monkeypatch.setattr(databricks_source, "_connect", _connect)
    success, message = databricks_source.test_databricks_connection(
        {
            "server_hostname": "adb-123.azuredatabricks.net",
            "http_path": "/sql/1.0/warehouses/abc",
            "access_token": "dapi-secret",
            "catalog": "main",
            "password": "should-never-be-sent",
        }
    )
    assert success is True
    assert "Connection successful" in message
    assert "stych_temp" not in message
    assert captured["connect"]["access_token"] == "dapi-secret"
    assert captured["connect"]["catalog"] == "main"
    assert "password" not in captured["connect"]
    assert captured["closed"] is True


def _databricks_fake_connect(monkeypatch, *, connects, executes):
    from datasources import databricks as databricks_source

    class FakeCursor:
        description = [("1",)]

        def execute(self, statement, parameters=None):
            executes.append(statement)
            return self

        def fetchall(self):
            return [(1,)]

        def close(self):
            return None

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            connects["closed"] = connects.get("closed", 0) + 1

    def _connect(config, *, socket_timeout=60):
        del config, socket_timeout
        connects["n"] = connects.get("n", 0) + 1
        return FakeConn()

    monkeypatch.setattr(databricks_source, "_connect", _connect)
    return {
        "server_hostname": "adb-123.azuredatabricks.net",
        "http_path": "/sql/1.0/warehouses/abc",
        "access_token": "dapi-secret",
        "catalog": "main",
    }


def test_databricks_test_is_select_one_only(monkeypatch):
    from datasources import databricks as databricks_source

    connects: dict = {}
    executes: list[str] = []
    config = _databricks_fake_connect(monkeypatch, connects=connects, executes=executes)
    success, message = databricks_source.test_databricks_connection(config)
    assert success is True
    assert message == "Connection successful"
    assert connects["n"] == 1
    assert executes == ["SELECT 1"]


def test_databricks_probe_and_save_validation_share_one_session(monkeypatch):
    from datasources import databricks as databricks_source

    connects: dict = {}
    executes: list[str] = []
    config = _databricks_fake_connect(monkeypatch, connects=connects, executes=executes)
    databricks_source.probe_scratch_permissions(config)
    assert connects["n"] == 1
    assert len(executes) == 3
    assert any("CREATE SCHEMA" in sql for sql in executes)
    assert any("CREATE TABLE" in sql for sql in executes)
    assert any("DROP TABLE" in sql for sql in executes)

    connects.clear()
    executes.clear()
    databricks_source.validate_scratch_access(config)
    assert connects["n"] == 1
    assert executes[0] == "SELECT 1"
    assert len(executes) == 4


def test_bigquery_tables_request_requires_dataset():
    from api.cloud_datasource_schemas import BigQueryConnectionParams, BigQueryTablesRequest

    with pytest.raises(ValidationError, match="at least 1 character"):
        BigQueryTablesRequest(
            connection=BigQueryConnectionParams(service_account_json=_SA),
            schema="",
        )
