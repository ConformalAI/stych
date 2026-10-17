"""Read-only SQL classification for agent query tools.

Parses with sqlparse so leading comments, parenthesized SELECTs, and
semicolon-separated batches are handled. Each statement must be a query
(SELECT / WITH / VALUES / TABLE / FROM) and must not contain write tokens
(including ``SELECT INTO`` and mutating CTEs). The driver still receives one
statement at a time; callers split, validate the whole batch, then execute.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Iterable, Optional

import sqlparse
from sqlparse.sql import Function, Identifier, IdentifierList, Parenthesis, Statement, TokenList
from sqlparse.tokens import DDL, DML, Comment, Punctuation

# Query-family openers. FROM covers Databricks pipe syntax (`FROM t |> SELECT`).
QUERY_START_KEYWORDS = frozenset({"SELECT", "WITH", "VALUES", "TABLE", "FROM"})

# Token values that mean a write even when they appear inside a SELECT/WITH.
_DISALLOWED_KEYWORDS = {
    "ALTER",
    "CALL",
    "CACHE",
    "CLONE",
    "COPY",
    "CREATE",
    "DELETE",
    "DROP",
    "EXEC",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "INTO",
    "MERGE",
    "OPTIMIZE",
    "PUT",
    "REFRESH",
    "REMOVE",
    "REPLACE",
    "RESTORE",
    "REVOKE",
    "TRUNCATE",
    "UNCACHE",
    "UNLOAD",
    "UPDATE",
    "VACUUM",
}

_READ_DML = frozenset({"SELECT"})

# Functions that embed a second SQL string the token scanner cannot see.
# A write inside OPENQUERY(srv, 'INSERT …') or dblink('…','INSERT …') would
# otherwise look like a SELECT. Blocking the carrier is the read-only boundary.
_REMOTE_SQL_FUNCTIONS = frozenset(
    {
        "OPENQUERY",
        "OPENROWSET",
        "OPENDATASOURCE",
        "DBLINK",
        "DBLINK_EXEC",
        "DBLINK_CONNECT",
        "SP_EXECUTESQL",
    }
)


class ReadOnlySQLError(ValueError):
    """SQL is empty, not a query, or contains a write."""


_CTE_RE = re.compile(r"(?is)(?:\bWITH\s+|,)\s*([A-Za-z_][A-Za-z0-9_$]*)\s+AS\s*\(")
_ATTACHED_SENTINEL = "__stych_attached_asset__"


def _relation_name(token) -> Optional[str]:
    if isinstance(token, Identifier):
        if any(isinstance(child, Parenthesis) for child in token.tokens):
            return None
        return token.get_real_name() or token.get_name()
    if isinstance(token, Function):
        return token.get_name() or str(token)
    value = str(token).strip()
    return value or None


def _referenced_relations(token_list: TokenList) -> list[str]:
    """Relations following FROM/JOIN/TABLE, including comma joins and subqueries."""
    found: list[str] = []
    tokens = token_list.tokens
    for index, token in enumerate(tokens):
        if isinstance(token, TokenList):
            found.extend(_referenced_relations(token))
        normalized = (getattr(token, "normalized", "") or "").upper()
        if normalized != "FROM" and normalized != "TABLE" and not normalized.endswith("JOIN"):
            continue
        for candidate in tokens[index + 1 :]:
            if candidate.is_whitespace or candidate.ttype in Comment:
                continue
            if isinstance(candidate, IdentifierList):
                for identifier in candidate.get_identifiers():
                    name = _relation_name(identifier)
                    if name:
                        found.append(name)
            else:
                name = _relation_name(candidate)
                if name:
                    found.append(name)
            break
    return found


def require_attached_asset_placeholder(sql: str, placeholder: str = "{table}") -> None:
    """Confine an agent-authored read to its attached relation.

    The placeholder is replaced only after this check. CTEs derived from the
    attached relation remain usable, but direct reads or joins to any other
    relation are refused. Connector-internal SQL does not pass through here.
    """
    if placeholder not in (sql or ""):
        raise ReadOnlySQLError(
            f"Queries must reference the attached dataset with the {placeholder} placeholder."
        )
    parsed_sql = sql.replace(placeholder, _ATTACHED_SENTINEL)
    ctes = {match.group(1).lower() for match in _CTE_RE.finditer(parsed_sql)}
    statements = list(_iter_statements(parsed_sql))
    relations = [
        relation for statement in statements for relation in _referenced_relations(statement)
    ]
    if _ATTACHED_SENTINEL not in {relation.lower() for relation in relations}:
        raise ReadOnlySQLError(
            f"Queries must read the attached dataset through the {placeholder} placeholder."
        )
    for relation in relations:
        if relation.lower() == _ATTACHED_SENTINEL:
            continue
        unquoted = relation.strip('`"[]').split(".")[-1].strip('`"[]').lower()
        if unquoted in ctes:
            continue
        raise ReadOnlySQLError(
            "Queries may only read the attached dataset and CTEs derived from it; "
            f"relation {relation!r} is outside this chat. Attach it first."
        )


def first_query_keyword(sql: str) -> str:
    """Return the first non-comment keyword of ``sql``, or ``""``."""
    statements = list(_iter_statements(sql))
    if not statements:
        return ""
    return _first_keyword(statements[0])


def parse_read_only_statements(
    sql: str,
    *,
    allowed_starts: Optional[FrozenSet[str]] = None,
) -> list[str]:
    """Split ``sql`` into read-only statements.

    Raises ``ReadOnlySQLError`` if the script is empty, any statement is not a
    query, or any statement embeds a write. Validates the whole batch before
    returning so a trailing ``INSERT`` cannot ride behind a leading ``SELECT``.
    """
    starts = allowed_starts if allowed_starts is not None else QUERY_START_KEYWORDS
    stripped = (sql or "").strip()
    if not stripped:
        raise ReadOnlySQLError("SQL cannot be empty")
    statements = list(_iter_statements(stripped))
    if not statements:
        raise ReadOnlySQLError("Only read-only queries are allowed (got empty/comment-only).")
    out: list[str] = []
    for index, statement in enumerate(statements, start=1):
        out.append(_validate_statement(statement, starts, index=index, total=len(statements)))
    return out


def _iter_statements(sql: str) -> Iterable[Statement]:
    for statement in sqlparse.parse(sql or ""):
        if _statement_sql(statement):
            yield statement


def _statement_sql(statement: Statement) -> str:
    return str(statement).strip().rstrip(";").strip()


def _first_keyword(statement: Statement) -> str:
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in Comment or token.ttype in Punctuation:
            continue
        value = (token.normalized or token.value or "").strip()
        if not value:
            continue
        return value
    return ""


def _ident_name(token) -> str:
    raw = (token.normalized or token.value or "").strip()
    return raw.strip('[]`"()').upper()


def _write_keyword(statement: Statement) -> str:
    for token in statement.flatten():
        if token.ttype in Comment:
            continue
        name = token.normalized or ""
        ident = _ident_name(token)
        if ident in _REMOTE_SQL_FUNCTIONS:
            return ident
        if token.ttype in DDL:
            return name
        if token.ttype in DML and name not in _READ_DML:
            return name
        if name in _DISALLOWED_KEYWORDS:
            return name
    return ""


def _validate_statement(
    statement: Statement,
    allowed_starts: FrozenSet[str],
    *,
    index: int,
    total: int,
) -> str:
    text = _statement_sql(statement)
    prefix = f"statement {index}/{total}: " if total > 1 else ""
    first = _first_keyword(statement)
    if first not in allowed_starts:
        got = first or "empty/comment-only"
        raise ReadOnlySQLError(
            f"{prefix}Only read-only queries are allowed (got {got}). "
            "Use sql_write_temp for INSERT/UPDATE/DELETE/DDL."
        )
    write = _write_keyword(statement)
    if write:
        raise ReadOnlySQLError(
            f"{prefix}Only read-only queries are allowed (got {write}). "
            "Use sql_write_temp for INSERT/UPDATE/DELETE/DDL."
        )
    return text
