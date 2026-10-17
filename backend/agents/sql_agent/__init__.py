"""
SQL Agent — server-side data operations on temporary tables.

When the session's data comes from Postgres (or other DB), this agent:
- Creates a temporary table copy (never touches the user's table)
- Runs cleaning (dedup, fill nulls, type casts) via SQL on the temp table
- Runs analysis by generating and executing SELECT queries

All writes are confined to the stych_temp schema.
"""

from .agent import SQLAgent
from .runner import PostgresSQLRunner, SQLRunnerError

__all__ = ["SQLAgent", "PostgresSQLRunner", "SQLRunnerError"]
