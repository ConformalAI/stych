"""
Shared ``sql_write_temp`` path for SQL sessions.

Every subagent that can mutate the session's ``stych_temp`` working copy
(cleaning, analysis, …) routes writes through this module so validation,
execution, and preview/diff cache invalidation behave identically everywhere.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from datasources.sql_readonly import QUERY_START_KEYWORDS, first_query_keyword

logger = logging.getLogger(__name__)

# Tool schema shared by the cleaning and analysis ReAct registries.
SQL_WRITE_TEMP_TOOL = {
    "type": "function",
    "function": {
        "name": "sql_write_temp",
        "description": (
            "Apply data-modifying statements (UPDATE/DELETE/ALTER/INSERT) to the session's "
            "working table. Reference the table with {table} (its full quoted name also "
            "works). Changes persist and are what the user sees in the data preview and "
            "diff; the user's original table is never touched. SELECT-only SQL is "
            "rejected — use a read tool (run_sql / sql_read) for reads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "One or more DML/DDL statements using the {table} placeholder.",
                },
            },
            "required": ["sql"],
        },
    },
}


async def execute_sql_write_temp(sql_agent: Any, state: Any, sql: str) -> str:
    """Split, validate, and run mutating statements against the temp table.

    Returns a plain-text observation for the ReAct loop. Bumps
    ``temp_table_generation`` on success so preview/diff caches refresh.
    """
    # Imported here, not at module level: the agents package imports the tool
    # registries at init, so a top-level import would be circular.
    from agents.sql_agent.cleaning import SQLCleaningService  # noqa: PLC0415

    if not sql_agent or not state or not state.data_context:
        return "ERROR: SQL datasource is not available."
    if not sql or not sql.strip():
        return (
            "ERROR: No SQL provided to sql_write_temp. Pass "
            '{"sql": "UPDATE {table} SET ..."} (a DML/DDL statement using {table}).'
        )

    temp_schema, temp_table = await sql_agent.ensure_temp_table(state)
    runner = sql_agent._runner_for(state)
    quoted_table = runner.quote_relation(temp_schema, temp_table)
    statements = SQLCleaningService.split_sql_statements(sql)
    exec_statements = [s.replace("{table}", quoted_table) for s in statements]

    # Guardrail: SELECT-only scripts don't modify the temp table. This was a
    # frequent failure mode where the agent "executes" a SELECT and then
    # expects the table to have changed.
    if exec_statements and all(
        first_query_keyword(s) in QUERY_START_KEYWORDS for s in exec_statements
    ):
        return (
            "ERROR: sql_write_temp expects DDL/DML that modifies {table} "
            "(e.g. UPDATE/DELETE/ALTER/INSERT). You provided SELECT-only SQL — use a "
            "read tool for SELECTs."
        )

    runner = sql_agent._runner_for(state)
    try:
        await asyncio.to_thread(
            runner.execute_statements_transaction, exec_statements, allowed_schema=temp_schema
        )
    except Exception as exc:  # noqa: BLE001 — observation string for the LLM
        return f"SQL ERROR: {str(exc)[:2000]}"

    SQLCleaningService._bump_temp_generation(state)
    logger.info("[sql_write_temp] statements=%d table=%s", len(exec_statements), quoted_table)
    return f"Executed {len(exec_statements)} statement(s) on the working table {quoted_table}."
