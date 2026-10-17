from types import SimpleNamespace

import pytest

from datasources import sqlserver


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM dbo.orders",
        "WITH recent AS (SELECT * FROM dbo.orders) SELECT * FROM recent",
        "SELECT 'UPDATE orders' AS harmless_text",
    ],
)
def test_validate_custom_query_accepts_one_read_statement(query):
    assert sqlserver.validate_custom_query(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * INTO dbo.copy FROM dbo.orders",
        "WITH changed AS (UPDATE dbo.orders SET qty = 1 OUTPUT inserted.*) SELECT * FROM changed",
        "SELECT * FROM dbo.orders; DELETE FROM dbo.orders",
        "SELECT * FROM OPENQUERY(other, 'INSERT INTO t VALUES (1)')",
    ],
)
def test_validate_custom_query_rejects_writes_and_multiple_statements(query):
    with pytest.raises(sqlserver.SqlServerLoadError):
        sqlserver.validate_custom_query(query)


def test_sqlserver_engine_sets_login_and_query_timeouts(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(sqlserver, "create_engine", fake_create_engine)

    sqlserver._get_engine(connection_url="mssql+pymssql://user:pw@host/db")

    assert captured["kwargs"]["connect_args"] == {"login_timeout": 10, "timeout": 30}


def test_connection_test_sanitizes_engine_creation_errors(monkeypatch):
    def fail_engine(**_kwargs):
        raise ValueError("driver details with secret")

    monkeypatch.setattr(sqlserver, "_get_engine", fail_engine)

    success, message = sqlserver.test_sqlserver_connection("not-a-valid-url")

    assert success is False
    assert message == "Connection failed. Verify the server, credentials, database, and network."
    assert "secret" not in message
