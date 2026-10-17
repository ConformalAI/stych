"""Confine agent-authored write statements to the Stych scratch schema.

This is the boundary that protects the customer's own tables. It replaces an
earlier substring test that asked whether the scratch schema name *appeared*
anywhere in the statement — which any of these would satisfy while writing
somewhere else entirely::

    INSERT INTO public.customers SELECT * FROM stych_temp.t1
    UPDATE public.customers c SET n = s.n FROM stych_temp.t1 s WHERE c.id = s.id
    DROP TABLE public.customers -- stych_temp.
    SELECT * INTO public.newtable FROM stych_temp.t

Here the *targets* of the write are extracted and every one of them must live in
the allowed schema. Anything whose shape cannot be recognised is refused rather
than allowed through, so a statement form we have not modelled fails closed.
Exfiltration verbs (COPY/UNLOAD/EXPORT/PUT) are refused for the same reason:
they write outside the database entirely.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import Comment

# Verbs a scratch write may legitimately start with. Anything else fails closed.
ALLOWED_WRITE_VERBS = frozenset(
    {"INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE"}
)

# Verbs that move data out of the engine. Never permitted from the agent path.
EXFILTRATION_VERBS = frozenset({"COPY", "UNLOAD", "EXPORT", "PUT", "GET", "GRANT", "REVOKE"})

_IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
# BigQuery wraps the entire dotted path in one pair of backticks.
_BQ_PATH = r"`[A-Za-z0-9_$\-]+(?:\.[A-Za-z0-9_$\-]+)+`"
_RELATION = rf"(?:{_BQ_PATH}|(?:{_IDENT}\s*\.\s*)*{_IDENT})"

# Each pattern captures the relation a statement writes to.
_TARGET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^INSERT\s+(?:INTO|OVERWRITE)\s+(?:TABLE\s+)?({_RELATION})", re.I),
    re.compile(rf"^UPDATE\s+(?:ONLY\s+)?({_RELATION})", re.I),
    re.compile(rf"^DELETE\s+FROM\s+(?:ONLY\s+)?({_RELATION})", re.I),
    re.compile(rf"^MERGE\s+INTO\s+({_RELATION})", re.I),
    re.compile(rf"^TRUNCATE\s+(?:TABLE\s+)?({_RELATION})", re.I),
    re.compile(
        rf"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:EXTERNAL\s+)?"
        rf"(?:TABLE|VIEW|SCHEMA|DATASET)\s+(?:IF\s+NOT\s+EXISTS\s+)?({_RELATION})",
        re.I,
    ),
    re.compile(rf"^DROP\s+(?:TABLE|VIEW|SCHEMA|DATASET)\s+(?:IF\s+EXISTS\s+)?({_RELATION})", re.I),
    re.compile(rf"^ALTER\s+(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?({_RELATION})", re.I),
)

# ``SELECT ... INTO t`` writes without any of the verbs above.
_SELECT_INTO = re.compile(rf"\bINTO\s+({_RELATION})", re.I)
_SET_SCHEMA = re.compile(rf"\bSET\s+SCHEMA\s+({_IDENT})", re.I)


class WriteGuardError(ValueError):
    """A write statement is not confined to the allowed schema."""


def _strip_comments(sql: str) -> str:
    return sqlparse.format(sql or "", strip_comments=True).strip()


def _first_keyword(statement: Statement) -> str:
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in Comment:
            continue
        value = (token.normalized or token.value or "").strip()
        if value:
            return value.upper()
    return ""


def _unquote(part: str) -> str:
    part = part.strip()
    for open_q, close_q in (('"', '"'), ("`", "`"), ("[", "]")):
        if part.startswith(open_q) and part.endswith(close_q) and len(part) >= 2:
            return part[1:-1]
    return part


def relation_schema(relation: str) -> Optional[str]:
    """Schema of a possibly-qualified relation, or None when unqualified.

    For a three-part ``db.schema.table`` the middle part is the schema; for
    ``schema.table`` it is the first. An unqualified name has no schema, which
    the caller must treat as "not provably in the allowed schema".

    BigQuery commonly wraps the whole dotted path in one pair of backticks
    rather than quoting each part, so that form is unwrapped before splitting.
    """
    relation = relation.strip()
    if len(relation) >= 2 and relation[0] == "`" and relation[-1] == "`":
        inner = relation[1:-1]
        if "`" not in inner:
            relation = inner
    parts = [p for p in re.split(r"\.(?=(?:[^\"'`\]]|\"[^\"]*\"|`[^`]*`|\[[^\]]*\])*$)", relation)]
    parts = [_unquote(p) for p in parts if p.strip()]
    if len(parts) >= 3:
        return parts[-2]
    if len(parts) == 2:
        return parts[0]
    return None


def relation_table(relation: str) -> Optional[str]:
    """Unquoted table/collection name of a possibly-qualified relation."""
    relation = relation.strip()
    if len(relation) >= 2 and relation[0] == "`" and relation[-1] == "`":
        inner = relation[1:-1]
        if "`" not in inner:
            relation = inner
    parts = [p for p in re.split(r"\.(?=(?:[^\"'`\]]|\"[^\"]*\"|`[^`]*`|\[[^\]]*\])*$)", relation)]
    parts = [_unquote(p) for p in parts if p.strip()]
    if not parts:
        return None
    return parts[-1]


def write_targets(statement_sql: str) -> List[str]:
    """Relations a statement writes to. Empty means the shape was not recognised."""
    text = statement_sql.strip()
    targets = [m.group(1) for pattern in _TARGET_PATTERNS if (m := pattern.match(text))]
    if not targets and text.upper().startswith(("SELECT", "WITH")):
        # SELECT ... INTO target
        found = _SELECT_INTO.search(text)
        if found:
            targets = [found.group(1)]
    return targets


def validate_write_statements(
    statements: Sequence[str],
    *,
    allowed_schema: str,
    allowed_table: Optional[str] = None,
    fold: str = "lower",
) -> None:
    """Raise unless every statement writes only inside *allowed_schema*.

    ``fold`` picks how identifiers compare when unquoted — engines differ on
    which way bare names fold, and a quoted name is compared as written.
    When *allowed_table* is set, every write target's table name must match it
    (copy-table edits that share a schema with real customer tables).
    """
    allowed = allowed_schema if fold == "none" else _fold(allowed_schema, fold)
    allowed_tbl = (
        None
        if not allowed_table
        else (allowed_table if fold == "none" else _fold(allowed_table, fold))
    )
    for raw in statements:
        stripped = _strip_comments(raw)
        if not stripped:
            continue
        parsed = [s for s in sqlparse.parse(stripped) if str(s).strip().rstrip(";").strip()]
        if len(parsed) > 1:
            raise WriteGuardError("Multiple statements in a single SQL string are not allowed")
        if not parsed:
            continue
        statement = parsed[0]
        text = str(statement).strip().rstrip(";").strip()
        verb = _first_keyword(statement)

        if verb in EXFILTRATION_VERBS:
            raise WriteGuardError(f"{verb} moves data outside the database and is not permitted.")
        if verb not in ALLOWED_WRITE_VERBS:
            if _is_pure_read(verb, text):
                continue
            # SELECT ... INTO is how T-SQL materializes a table. It is checked
            # like any other write rather than waved through: the target still
            # has to live in the allowed schema.
            if verb in ("SELECT", "WITH") and write_targets(text):
                _check_targets(write_targets(text), allowed, allowed_schema, fold, allowed_tbl)
                continue
            raise WriteGuardError(
                f"Statements starting with {verb or 'this keyword'} are not permitted; "
                f"writes must target schema '{allowed_schema}'."
            )

        targets = write_targets(text)
        if not targets:
            raise WriteGuardError(
                "Could not determine what this statement writes to, so it is refused. "
                f"Writes must target schema '{allowed_schema}'."
            )
        _check_targets(targets, allowed, allowed_schema, fold, allowed_tbl)
        moved = _SET_SCHEMA.search(text)
        if moved:
            dest = _unquote(moved.group(1))
            dest_folded = dest if fold == "none" else _fold(dest, fold)
            if dest_folded != allowed:
                raise WriteGuardError(
                    f"ALTER ... SET SCHEMA cannot move a table out of '{allowed_schema}' "
                    f"(got {dest!r})."
                )


def _check_targets(
    targets: Sequence[str],
    allowed: str,
    allowed_schema: str,
    fold: str,
    allowed_table: Optional[str] = None,
) -> None:
    for target in targets:
        schema = relation_schema(target)
        if schema is None:
            raise WriteGuardError(
                f"Write target {target!r} is not schema-qualified; "
                f"writes must name schema '{allowed_schema}'."
            )
        if (schema if fold == "none" else _fold(schema, fold)) != allowed:
            raise WriteGuardError(
                f"Write statements must target schema '{allowed_schema}' only " f"(got {schema!r})."
            )
        if allowed_table:
            table = relation_table(target)
            table_folded = (
                None if table is None else (table if fold == "none" else _fold(table, fold))
            )
            if table_folded != allowed_table:
                raise WriteGuardError(
                    f"Write statements must target table '{allowed_table}' only "
                    f"(got {table!r})."
                )


def _fold(name: str, fold: str) -> str:
    return name.upper() if fold == "upper" else name.lower()


def _is_pure_read(verb: str, text: str) -> bool:
    """A bare SELECT/WITH with no INTO is harmless in a write batch."""
    if verb not in ("SELECT", "WITH"):
        return False
    return not _SELECT_INTO.search(text)


def iter_statements(sql: str) -> Iterable[str]:
    """Split a script into non-empty statements with comments stripped."""
    for statement in sqlparse.parse(_strip_comments(sql)):
        text = str(statement).strip().rstrip(";").strip()
        if text:
            yield text
