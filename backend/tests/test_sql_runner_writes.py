"""Tests for SQL runner write-scope validation."""

import importlib.util
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _BACKEND / "agents" / "sql_agent" / "runner.py"
_spec = importlib.util.spec_from_file_location("sql_runner_under_test", _RUNNER_PATH)
assert _spec and _spec.loader
_runner_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner_mod)

PostgresSQLRunner = _runner_mod.PostgresSQLRunner
SQLRunnerError = _runner_mod.SQLRunnerError
TEMP_SCHEMA = _runner_mod.TEMP_SCHEMA


def test_execute_statements_rejects_writes_outside_temp_schema():
    runner = PostgresSQLRunner.__new__(PostgresSQLRunner)
    statements = ['UPDATE "public"."customers" SET name = \'x\' WHERE id = 1']

    with pytest.raises(SQLRunnerError, match="stych_temp"):
        runner._validate_write_statements(statements, allowed_schema=TEMP_SCHEMA)


def test_execute_statements_allows_temp_schema_writes():
    runner = PostgresSQLRunner.__new__(PostgresSQLRunner)
    statements = [f'UPDATE "{TEMP_SCHEMA}"."stych_abc" SET col = NULL']

    runner._validate_write_statements(statements, allowed_schema=TEMP_SCHEMA)
