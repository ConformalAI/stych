"""BigQuery scratch-runner behaviour verified without a live project.

Covers the review findings on the runner: the INFORMATION_SCHEMA probe binds
the table name instead of splicing it, the client handle is released, and
per-attachment overrides never leak into the caller's config.
"""

from __future__ import annotations

import pytest

from agents.sql_agent import bigquery_runner as bq_runner
from agents.sql_agent.bigquery_runner import BigQuerySQLRunner

CONFIG = {"auth_mode": "oauth", "access_token": "tok", "project_id": "proj"}


@pytest.fixture()
def runner(monkeypatch):
    """A runner whose reads are captured instead of executed."""
    captured: dict = {}

    def _fake_query(config, statement, parameters=(), *, timeout_seconds=None, client=None):
        captured["statement"] = statement
        captured["parameters"] = parameters
        captured["client"] = client
        return ["column_name", "data_type", "is_nullable"], [("id", "INT64", "NO")]

    monkeypatch.setattr(bq_runner, "run_bigquery_query", _fake_query)
    r = BigQuerySQLRunner(CONFIG)
    return r, captured


def test_get_table_columns_binds_the_table_name(runner, monkeypatch):
    r, captured = runner
    monkeypatch.setattr(r, "_client", lambda: "client")
    hostile = "t' OR '1'='1"
    columns = r.get_table_columns("ds", hostile)

    assert hostile not in captured["statement"]
    assert "WHERE table_name = ?" in captured["statement"]
    assert captured["parameters"] == (hostile,)
    assert captured["client"] == "client"
    assert columns == [{"column_name": "id", "data_type": "INT64", "is_nullable": "NO"}]


def test_close_releases_the_client_once(monkeypatch):
    closed: list[int] = []

    class FakeClient:
        def close(self):
            closed.append(1)

    monkeypatch.setattr(bq_runner, "_client", lambda config: FakeClient())
    r = BigQuerySQLRunner(CONFIG)
    assert r._client() is r._client()  # cached, not re-opened per call
    r.close()
    r.close()
    assert closed == [1]
    assert r._client_handle is None


def test_close_is_safe_before_a_client_exists():
    r = BigQuerySQLRunner(CONFIG)
    r.close()  # nothing opened yet
    r.__del__()


def test_meta_overrides_do_not_mutate_the_caller_config():
    cfg = dict(CONFIG)
    meta = {"scratch_schema": "custom_tmp", "scratch_location": "EU", "schema": "src"}
    out = BigQuerySQLRunner._apply_meta_overrides(meta, cfg)

    assert cfg == CONFIG
    assert out["scratch_schema"] == "custom_tmp"
    assert out["scratch_location"] == "EU"
    assert out["source_dataset"] == "src"
