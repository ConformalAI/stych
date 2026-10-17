"""
Table adapters: pushdown query layer over foreign database sources.

A ``TableAdapter`` answers the preview endpoints' questions (schema, counts,
one page of rows) by generating queries that run inside the client's database,
so the grid can page/filter/sort over the full table instead of the 10K
connect-time sample. Semantics mirror ``preview_service.apply_query``:
case-insensitive substring filters ANDed across columns, global search ORed
across all columns, NULLs last on sort.

Pure query builders live in each backend module so they can be unit-tested
without a database; only the adapter classes touch connections.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd

# Short interactive timeout for preview queries — deliberately separate from
# the SQL agent runner's 5-minute statement_timeout: a preview keystroke must
# fail fast, an agent cleaning pass may legitimately run long.
PREVIEW_TIMEOUT_MS = 8_000
# Diff summary/probe queries scan whole tables; give them more headroom.
DIFF_TIMEOUT_MS = 30_000

# Bounded sample size for operations that need real values (LLM context,
# diff transform classification).
SAMPLE_ROWS = 10_000

# Rows per batch when streaming a full relation out (export). Large enough that
# per-round-trip overhead is amortized, small enough that one batch is a
# reasonable memory ceiling for wide tables.
EXPORT_BATCH_ROWS = 50_000
# Exports legitimately run for minutes; the interactive preview timeout must not
# leak onto them.
EXPORT_TIMEOUT_MS = 30 * 60 * 1_000


class AdapterError(Exception):
    """Base error for table adapter operations."""


class AdapterUnavailable(AdapterError):
    """Connection info is missing/redacted — the user must reconnect the table."""


class TempTableUnavailable(AdapterError):
    """No working (temp) table exists yet for this source."""


class AdapterTimeout(AdapterError):
    """The database cancelled the query after the preview timeout."""


@dataclass
class AdapterColumn:
    name: str
    db_type: str  # raw DB type string; shown as the grid's dtype
    kind: str  # "number" | "string" | "date" | "bool" | "other"


@dataclass
class CountResult:
    value: int
    approx: bool = False


@dataclass
class SourceCacheInfo:
    """Identity + volatility of a source, used to key the count/schema caches.

    ``generation`` is bumped by the SQL agent whenever it mutates the temp
    table, so cached counts/schemas for the working copy invalidate instantly;
    base tables can change outside our control, hence the short TTL.
    """

    key: Tuple[Any, ...] = field(default_factory=tuple)
    ttl: float = 60.0


class TableAdapter(ABC):
    """Pushdown query interface over one foreign table/collection/query."""

    dialect: str = ""
    supports_stats: bool = False
    cache_info: SourceCacheInfo

    @abstractmethod
    def schema(self) -> List[AdapterColumn]:
        """Column names/types in source order."""

    @abstractmethod
    def count(
        self, filters: Optional[Dict[str, str]] = None, search: Optional[str] = None
    ) -> CountResult:
        """Row count matching the filters/search (exact, or flagged approximate)."""

    @abstractmethod
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
        """One page of rows (lists aligned to ``schema()`` order)."""

    @abstractmethod
    def fetch_sample(
        self, columns: Optional[List[str]] = None, limit: int = SAMPLE_ROWS
    ) -> pd.DataFrame:
        """Bounded sample of real rows as a DataFrame."""

    def stats(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Per-column stats, or None when unsupported / timed out."""
        return None

    # -- full scan ----------------------------------------------------------

    def stream_batches(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Yield the whole relation in batches, newest schema order preserved.

        The default pages through :meth:`fetch_page`, which reads the relation
        across many statements: a table written concurrently can yield torn
        results (a row shifting across a page boundary is seen twice or not at
        all). Adapters that can read the relation in one statement — and so get
        a single consistent snapshot — override this. A connector may only
        declare ``export_snapshot`` at ``full`` scope once it does.
        """
        return self._stream_by_paging(columns=columns, batch_rows=batch_rows, max_rows=max_rows)

    def _stream_by_paging(
        self,
        *,
        columns: Optional[List[str]] = None,
        batch_rows: int = EXPORT_BATCH_ROWS,
        max_rows: Optional[int] = None,
    ) -> Iterator[pd.DataFrame]:
        """Offset-paginated fallback scan shared by adapters without a cursor."""
        names = [c.name for c in self.schema()]
        keep = [n for n in (columns or names) if n in set(names)] or names
        offset = 0
        while max_rows is None or offset < max_rows:
            size = int(batch_rows)
            if max_rows is not None:
                size = min(size, max_rows - offset)
            rows = self.fetch_page(offset=offset, limit=size)
            if not rows:
                return
            frame = pd.DataFrame(rows, columns=names)
            yield frame[keep] if keep != names else frame
            if len(rows) < size:
                return
            offset += len(rows)

    @contextmanager
    def bound_session(self) -> Iterator[None]:
        """Hold one backend session for schema/count/page in a preview request.

        Default is a no-op; warehouse and SQL adapters override this.
        """
        yield


def escape_like(needle: str, extra: str = "") -> str:
    r"""Escape LIKE metacharacters with backslash so needles match literally.

    ``extra`` adds dialect-specific metacharacters (e.g. ``[`` for T-SQL).
    """
    out = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    for ch in extra:
        out = out.replace(ch, "\\" + ch)
    return out


def sql_type_kind(db_type: str) -> str:
    """Map a raw SQL type name to the frontend's column kind."""
    t = (db_type or "").lower()
    if "bool" in t or t == "bit":
        return "bool"
    if any(
        n in t
        for n in (
            "int",
            "serial",
            "numeric",
            "decimal",
            "real",
            "double",
            "float",
            "money",
        )
    ):
        return "number"
    if any(n in t for n in ("timestamp", "datetime", "date", "time")):
        return "date"
    if any(n in t for n in ("char", "text", "uuid", "json", "xml", "string")):
        return "string"
    return "other"


# ---------------------------------------------------------------------------
# Tiny TTL caches for counts and schemas. COUNT(*) on a big table is the most
# expensive part of serving a page; caching it per (source, query, generation)
# means paging through results costs one count, not one per page.
# ---------------------------------------------------------------------------
_CACHE_MAX_ENTRIES = 64
_count_cache: "OrderedDict[Tuple[Any, ...], Tuple[CountResult, float]]" = OrderedDict()
_schema_cache: "OrderedDict[Tuple[Any, ...], Tuple[List[AdapterColumn], float]]" = OrderedDict()
_cache_lock = Lock()


def _cache_get(cache: OrderedDict, key: Tuple[Any, ...]) -> Optional[Any]:
    with _cache_lock:
        hit = cache.get(key)
        if hit is None:
            return None
        value, expires_at = hit
        if time.monotonic() >= expires_at:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return value


def _cache_put(cache: OrderedDict, key: Tuple[Any, ...], value: Any, ttl: float) -> None:
    with _cache_lock:
        cache[key] = (value, time.monotonic() + ttl)
        cache.move_to_end(key)
        while len(cache) > _CACHE_MAX_ENTRIES:
            cache.popitem(last=False)


def cached_count(key: Tuple[Any, ...], ttl: float, compute) -> CountResult:
    value = _cache_get(_count_cache, key)
    if value is None:
        value = compute()
        _cache_put(_count_cache, key, value, ttl)
    return value


def cached_schema(key: Tuple[Any, ...], ttl: float, compute) -> List[AdapterColumn]:
    value = _cache_get(_schema_cache, key)
    if value is None:
        value = compute()
        _cache_put(_schema_cache, key, value, ttl)
    return value


def clear_caches() -> None:
    """Test helper."""
    with _cache_lock:
        _count_cache.clear()
        _schema_cache.clear()
