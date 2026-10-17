"""Dialect-aware statement shapes used by source-side joins.

The general :mod:`datasources.sql_dialect` layer owns expressions and generic
warehouse DDL.  Joins need three additional shapes in one place: a quoted
relation, materialization of an already-built joined ``SELECT``, and a
bounded preview.  Keeping these shapes here prevents the LinkingAgent from
reimplementing SQL Server's ``SELECT TOP``/``SELECT ... INTO`` differences or
BigQuery's backtick qualification.

This module intentionally does not build join predicates.  The LinkingAgent
still proposes the join semantics; callers pass its validated SELECT text to
these connector-owned statement builders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .sql_dialect import SqlDialect, get_dialect


def _clean_query(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        raise ValueError("A join query is required.")
    return text.rstrip(";").rstrip()


def _validate_limit(limit: Optional[int]) -> Optional[int]:
    if limit is None:
        return None
    value = int(limit)
    if value < 0:
        raise ValueError("A preview limit cannot be negative.")
    return value


@dataclass(frozen=True)
class JoinDialect:
    """Small, connector-neutral API for source-side join statement shapes."""

    sql: SqlDialect

    @property
    def name(self) -> str:
        return self.sql.name

    def quote_identifier(self, name: str) -> str:
        return self.sql.quote(name)

    def quote_relation(self, schema: str, table: str, *, database: Optional[str] = None) -> str:
        return self.sql.relation(schema, table, database=database)

    def materialize_join(self, target: str, select_sql: str) -> str:
        """Materialize *select_sql* into an already-validated scratch target.

        Wrapping the SELECT as a derived table gives every dialect the same
        interface while delegating the target DDL shape to ``SqlDialect``;
        notably SQL Server emits ``SELECT ... INTO`` rather than invalid CTAS.
        """
        query = _clean_query(select_sql)
        return self.sql.create_table_as(target, "*", f"({query}) AS _stych_join")

    def preview_query(self, select_sql: str, limit: Optional[int] = 500) -> str:
        """Return a bounded preview query for an arbitrary joined SELECT."""
        query = _clean_query(select_sql)
        cap = _validate_limit(limit)
        prefix = self.sql.select_prefix(cap)
        suffix = self.sql.limit_suffix(cap)
        return f"SELECT {prefix}* FROM ({query}) AS _stych_preview{suffix}"

    def preview_relation(self, relation: str, limit: Optional[int] = 500) -> str:
        """Return a bounded preview query for an existing relation."""
        cap = _validate_limit(limit)
        return f"SELECT {self.sql.select_prefix(cap)}* FROM {relation}{self.sql.limit_suffix(cap)}"


def get_join_dialect(name: Optional[str]) -> JoinDialect:
    """Resolve the join statement API for a connector engine."""
    return JoinDialect(get_dialect(name))
