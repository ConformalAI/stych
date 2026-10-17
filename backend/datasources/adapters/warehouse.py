"""Shared SQL/query behavior for read-only relational warehouse adapters."""

from __future__ import annotations

import re
from abc import abstractmethod
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from datasources.warehouse import (
    WarehouseError,
    quote_ident,
    relation_ref,
    validate_read_only_query,
)

from .base import (
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
    cached_schema,
    escape_like,
)

_NAMED_PARAM = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


def named_to_qmark(
    sql: str, params: Dict[str, Any], *, placeholder: str = "?"
) -> tuple[str, tuple[Any, ...]]:
    """Convert ``:name`` binds to the adapter placeholder.

    ``?`` is the default used by most warehouse DB-API drivers, while
    Redshift's driver requires ``%s``.  Keep the named-bind API used by the
    shared SQL builders, but render the marker expected by the concrete
    adapter before execution.
    """
    values: list[Any] = []

    def _repl(match: re.Match[str]) -> str:
        values.append(params[match.group(1)])
        return placeholder

    return _NAMED_PARAM.sub(_repl, sql), tuple(values)


def build_source_ref(
    *,
    database: Optional[str],
    schema: str,
    table: Optional[str],
    custom_query: Optional[str],
    quote: str = '"',
) -> str:
    if custom_query:
        try:
            return f"({validate_read_only_query(custom_query)}) AS _src"
        except WarehouseError as exc:
            raise AdapterError(str(exc)) from exc
    if not table:
        raise AdapterError("Either table or custom_query is required")
    return relation_ref(database=database, schema=schema, table=table, quote=quote)


def _like_clause(
    ident: str,
    placeholder: str,
    *,
    string_type: str,
    like_operator: str,
    fold_like: bool,
    escape_clause: str = " ESCAPE '\\'",
) -> str:
    casted = f"CAST({ident} AS {string_type})"
    if fold_like:
        return f"LOWER({casted}) LIKE LOWER({placeholder}){escape_clause}"
    return f"{casted} {like_operator} {placeholder}{escape_clause}"


def build_where(
    column_names: List[str],
    *,
    placeholder: str,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    quote: str = '"',
    string_type: str = "VARCHAR",
    like_operator: str = "ILIKE",
    fold_like: bool = False,
    escape_clause: str = " ESCAPE '\\'",
) -> tuple[str, tuple[Any, ...]]:
    known = set(column_names)
    clauses: list[str] = []
    params: list[Any] = []
    like_kw = {
        "string_type": string_type,
        "like_operator": like_operator,
        "fold_like": fold_like,
        "escape_clause": escape_clause,
    }
    for name, needle in (filters or {}).items():
        if name not in known or not needle:
            continue
        clauses.append(_like_clause(quote_ident(name, quote), placeholder, **like_kw))
        params.append(f"%{escape_like(needle)}%")
    if search:
        ors = [
            _like_clause(quote_ident(name, quote), placeholder, **like_kw) for name in column_names
        ]
        if ors:
            clauses.append("(" + " OR ".join(ors) + ")")
            params.extend(f"%{escape_like(search)}%" for _ in column_names)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, tuple(params)


def build_select_sql(
    source_ref: str,
    column_names: List[str],
    *,
    placeholder: str,
    offset: int,
    limit: int,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    quote: str = '"',
    string_type: str = "VARCHAR",
    like_operator: str = "ILIKE",
    fold_like: bool = False,
    escape_clause: str = " ESCAPE '\\'",
) -> tuple[str, tuple[Any, ...]]:
    select_list = ", ".join(quote_ident(name, quote) for name in column_names)
    where, params = build_where(
        column_names,
        placeholder=placeholder,
        filters=filters,
        search=search,
        quote=quote,
        string_type=string_type,
        like_operator=like_operator,
        fold_like=fold_like,
        escape_clause=escape_clause,
    )
    order = ""
    if sort_by and sort_by in set(column_names):
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order = f" ORDER BY {quote_ident(sort_by, quote)} {direction} NULLS LAST"
    statement = (
        f"SELECT {select_list} FROM {source_ref}{where}{order} "
        f"LIMIT {placeholder} OFFSET {placeholder}"
    )
    return statement, (*params, int(limit), int(offset))


def build_count_sql(
    source_ref: str,
    column_names: List[str],
    *,
    placeholder: str,
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    quote: str = '"',
    string_type: str = "VARCHAR",
    like_operator: str = "ILIKE",
    fold_like: bool = False,
    escape_clause: str = " ESCAPE '\\'",
) -> tuple[str, tuple[Any, ...]]:
    where, params = build_where(
        column_names,
        placeholder=placeholder,
        filters=filters,
        search=search,
        quote=quote,
        string_type=string_type,
        like_operator=like_operator,
        fold_like=fold_like,
        escape_clause=escape_clause,
    )
    return f"SELECT COUNT(*) FROM {source_ref}{where}", params


class WarehouseTableAdapter(TableAdapter):
    """Common preview surface; subclasses own credentials, execution, and metadata SQL."""

    supports_stats = False
    placeholder = "?"
    ident_quote = '"'
    string_type = "VARCHAR"
    like_operator = "ILIKE"
    fold_like = False
    escape_clause = " ESCAPE '\\'"

    def __init__(
        self,
        connection_config: dict[str, Any],
        *,
        database: Optional[str],
        schema: str,
        table: Optional[str],
        custom_query: Optional[str],
        sample_kinds: Optional[Dict[str, str]] = None,
        cache_info: SourceCacheInfo,
    ):
        self.connection_config = dict(connection_config)
        self.database = database
        self.db_schema = schema
        self.table = table
        self.custom_query = custom_query
        self.is_table = custom_query is None
        self.source_ref = build_source_ref(
            database=database,
            schema=schema,
            table=table,
            custom_query=custom_query,
            quote=self.ident_quote,
        )
        self.sample_kinds = sample_kinds or {}
        self.cache_info = cache_info
        self._session = None

    def _sql_dialect(self) -> dict[str, Any]:
        return {
            "quote": self.ident_quote,
            "string_type": self.string_type,
            "like_operator": self.like_operator,
            "fold_like": self.fold_like,
            "escape_clause": self.escape_clause,
        }

    @abstractmethod
    def _open_session(self):
        """Open one backend session reused for schema/count/page in a request."""

    def _close_session(self, handle) -> None:
        closer = getattr(handle, "close", None)
        if closer is not None:
            closer()

    @contextmanager
    def bound_session(self) -> Iterator[None]:
        """Hold one warehouse session for every query in this request."""
        if self._session is not None:
            yield
            return
        handle = self._open_session()
        self._session = handle
        try:
            yield
        finally:
            self._session = None
            self._close_session(handle)

    def _session_kwargs(self) -> dict[str, Any]:
        if self._session is None:
            return {}
        return {"connection": self._session}

    @abstractmethod
    def _run(
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> tuple[list[str], list[tuple]]:
        """Execute one bounded read query."""

    def run(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: int = PREVIEW_TIMEOUT_MS,
    ) -> tuple[list[str], list[tuple]]:
        """Run one read query with named binds rendered for this adapter."""
        del timeout_ms
        statement, values = sql, ()
        if params:
            statement, values = named_to_qmark(sql, params, placeholder=self.placeholder)
        return self._run(statement, values)

    @abstractmethod
    def _load_table_schema(self) -> List[AdapterColumn]:
        """Return authoritative table columns for a selected relation."""

    def schema(self) -> List[AdapterColumn]:
        return cached_schema(
            ("schema", *self.cache_info.key), self.cache_info.ttl, self._load_schema
        )

    def _load_schema(self) -> List[AdapterColumn]:
        if self.is_table:
            columns = self._load_table_schema()
            if not columns:
                raise AdapterError(f"Table {self.db_schema}.{self.table} was not found.")
            return columns
        names, _ = self._run(f"SELECT * FROM {self.source_ref} LIMIT 0")
        return [
            AdapterColumn(name=name, db_type="", kind=self.sample_kinds.get(name, "string"))
            for name in names
        ]

    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        names = [column.name for column in self.schema()]
        statement, params = build_count_sql(
            self.source_ref,
            names,
            placeholder=self.placeholder,
            filters=filters,
            search=search,
            **self._sql_dialect(),
        )
        _, rows = self._run(statement, params)
        return CountResult(int(rows[0][0]))

    def fetch_page(
        self,
        *,
        offset: int,
        limit: int,
        sort_by: Optional[str] = None,
        sort_dir: str = "asc",
        filters: Optional[Dict[str, str]] = None,
        search: Optional[str] = None,
    ) -> List[List[Any]]:
        names = [column.name for column in self.schema()]
        statement, params = build_select_sql(
            self.source_ref,
            names,
            placeholder=self.placeholder,
            offset=offset,
            limit=limit,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filters=filters,
            search=search,
            **self._sql_dialect(),
        )
        _, rows = self._run(statement, params)
        return [list(row) for row in rows]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        known = [column.name for column in self.schema()]
        selected = [name for name in (columns or known) if name in set(known)]
        if not selected:
            selected = known
        statement = (
            f"SELECT {', '.join(quote_ident(name, self.ident_quote) for name in selected)} "
            f"FROM {self.source_ref} LIMIT {self.placeholder}"
        )
        _, rows = self._run(statement, (int(limit),))
        return pd.DataFrame.from_records(rows, columns=selected)
