"""
MongoDB collection adapter: full-collection preview pushdown.

Collections have no schema; the column set (dotted ``json_normalize`` paths)
comes from the connect-time sample and every page is re-normalized against it,
so row arrays always align with the declared columns. Mutations land in a
Stych-owned scratch collection — the source collection is never updated.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd

from datasources.mongodb import flatten_documents

from .base import (
    EXPORT_BATCH_ROWS,
    EXPORT_TIMEOUT_MS,
    PREVIEW_TIMEOUT_MS,
    SAMPLE_ROWS,
    AdapterColumn,
    AdapterError,
    AdapterTimeout,
    CountResult,
    SourceCacheInfo,
    TableAdapter,
)

logger = logging.getLogger(__name__)

_CLIENT_POOL: Dict[str, Any] = {}
_POOL_LOCK = threading.Lock()


def _get_shared_client(url: str):
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover
        raise AdapterError("pymongo is required for MongoDB support.") from exc
    if url in _CLIENT_POOL:
        return _CLIENT_POOL[url]
    with _POOL_LOCK:
        if url not in _CLIENT_POOL:
            # MongoClient is thread-safe and pools connections internally.
            _CLIENT_POOL[url] = MongoClient(url, serverSelectionTimeoutMS=10_000)
        return _CLIENT_POOL[url]


def _regex_clause(path: str, needle: str) -> Dict[str, Any]:
    """Case-insensitive substring match on any field, converting non-string
    values to text first (mirrors the CAST-to-text SQL pushdown)."""
    return {
        "$expr": {
            "$regexMatch": {
                "input": {
                    "$convert": {
                        "input": f"${path}",
                        "to": "string",
                        "onError": "",
                        "onNull": "",
                    }
                },
                "regex": re.escape(needle),
                "options": "i",
            }
        }
    }


def build_filter_doc(
    column_names: List[str],
    filters: Optional[Dict[str, str]] = None,
    search: Optional[str] = None,
    base_query: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """AND of: the connect-time base query, per-column filters, and a global
    search ORed across all columns. Unknown filter columns are dropped."""
    known = set(column_names)
    clauses: List[Dict[str, Any]] = []
    if base_query:
        clauses.append(base_query)
    for name, needle in (filters or {}).items():
        if name not in known or not needle:
            continue
        clauses.append(_regex_clause(name, needle))
    if search and column_names:
        clauses.append({"$or": [_regex_clause(c, search) for c in column_names]})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class MongoTableAdapter(TableAdapter):
    dialect = "mongodb"
    supports_stats = False

    def __init__(
        self,
        connection_url: str,
        *,
        database: str,
        collection: str,
        base_query: Optional[Dict[str, Any]] = None,
        columns: Optional[List[AdapterColumn]] = None,
        cache_info: Optional[SourceCacheInfo] = None,
    ):
        if not collection:
            raise AdapterError("A collection name is required.")
        self.connection_url = connection_url
        self.database = database
        self.collection = collection
        self.base_query = base_query or None
        self._columns = columns or []
        self.cache_info = cache_info or SourceCacheInfo(
            key=("mongo", connection_url, database, collection)
        )

    def _col(self):
        client = _get_shared_client(self.connection_url)
        return client[self.database][self.collection]

    def schema(self) -> List[AdapterColumn]:
        if not self._columns:
            try:
                doc = self._col().find_one(self.base_query or {})
            except Exception as exc:
                raise AdapterError(str(exc)) from exc
            if doc:
                frame = flatten_documents([doc])
                self._columns = [
                    AdapterColumn(name=str(name), db_type=str(dtype), kind="string")
                    for name, dtype in frame.dtypes.items()
                ]
        return self._columns

    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        from pymongo.errors import ExecutionTimeout, PyMongoError

        names = [c.name for c in self._columns]
        doc = build_filter_doc(names, filters, search, self.base_query)
        try:
            n = self._col().count_documents(doc, maxTimeMS=PREVIEW_TIMEOUT_MS)
            return CountResult(int(n))
        except ExecutionTimeout as exc:
            if not doc:
                return CountResult(int(self._col().estimated_document_count()), approx=True)
            raise AdapterTimeout("Count timed out.") from exc
        except PyMongoError as exc:
            raise AdapterError(str(exc)) from exc

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
        from pymongo.errors import ExecutionTimeout, PyMongoError

        names = [c.name for c in self._columns]
        doc = build_filter_doc(names, filters, search, self.base_query)
        try:
            cursor = self._col().find(doc).max_time_ms(PREVIEW_TIMEOUT_MS)
            if sort_by and sort_by in set(names):
                # Mongo sorts missing values first ascending — no NULLS LAST
                # equivalent without an aggregation pipeline; accepted caveat.
                cursor = cursor.sort(sort_by, -1 if sort_dir == "desc" else 1)
            docs = list(cursor.skip(int(offset)).limit(int(limit)))
        except ExecutionTimeout as exc:
            raise AdapterTimeout("Query timed out.") from exc
        except PyMongoError as exc:
            raise AdapterError(str(exc)) from exc
        if not docs:
            return []
        frame = flatten_documents(docs)
        frame = frame.reindex(columns=names)
        return [list(row) for row in frame.itertuples(index=False, name=None)]

    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        names = [c.name for c in self._columns]
        if columns:
            names = [n for n in names if n in set(columns)]
        doc = build_filter_doc([], None, None, self.base_query)
        docs = list(self._col().find(doc, limit=int(limit)).max_time_ms(PREVIEW_TIMEOUT_MS))
        if not docs:
            return pd.DataFrame(columns=names)
        frame = flatten_documents(docs)
        return frame.reindex(columns=names)

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Full scan through one cursor, snapshot-isolated where the deployment
        allows it.

        Two things make this a single consistent read rather than the paging
        default, which can tear:

        - **``readConcern: "snapshot"``** pins one point in time for the whole
          scan. It needs a replica set or a sharded cluster, so a standalone
          ``mongod`` falls back — the fallback is still one cursor, not many
          statements.
        - **Sorting on ``_id``** removes the classic scan hazard even without
          snapshot isolation: ``_id`` is immutable, so a document cannot move
          within the index and be returned twice. A document inserted or deleted
          mid-scan is still seen or missed, which is what "not snapshot" means.
        """
        from pymongo.errors import ExecutionTimeout, PyMongoError

        names = [c.name for c in self._columns]
        # ``columns=None`` means a full document export. MongoDB has no complete
        # catalog schema, so retaining only connect-time sample columns would
        # silently discard fields that first appear later in the collection.
        keep = None if columns is None else [n for n in columns if n in set(names)]
        doc = build_filter_doc([], None, None, self.base_query)
        remaining = None if max_rows is None else int(max_rows)

        client = _get_shared_client(self.connection_url)
        for read_concern in ("snapshot", None):
            session = None
            try:
                if read_concern is not None:
                    session = client.start_session(snapshot=True)
                cursor = (
                    client[self.database][self.collection]
                    .find(doc, session=session)
                    .sort("_id", 1)
                    .batch_size(int(batch_rows))
                    .max_time_ms(EXPORT_TIMEOUT_MS)
                )
                if remaining is not None:
                    cursor = cursor.limit(remaining)
                yield from _batched(cursor, keep, batch_rows, remaining)
                return
            except ExecutionTimeout as exc:
                raise AdapterTimeout("Export timed out.") from exc
            except PyMongoError as exc:
                # A standalone deployment refuses snapshot sessions; retry
                # without one rather than failing an otherwise valid export.
                if read_concern is None:
                    raise AdapterError(str(exc)) from exc
                logger.debug("[mongo] snapshot read unavailable, falling back: %s", exc)
            finally:
                if session is not None:
                    session.end_session()


def _batched(cursor, keep: Optional[List[str]], batch_rows: int, max_rows: Optional[int]):
    """Group a cursor's documents into flattened frames of at most *batch_rows*."""
    buffer: List[Dict[str, Any]] = []
    seen = 0
    for document in cursor:
        buffer.append(document)
        seen += 1
        if len(buffer) >= int(batch_rows):
            frame = flatten_documents(buffer)
            yield frame if keep is None else frame.reindex(columns=keep)
            buffer = []
        if max_rows is not None and seen >= max_rows:
            break
    if buffer:
        frame = flatten_documents(buffer)
        yield frame if keep is None else frame.reindex(columns=keep)
