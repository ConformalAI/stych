"""
TryOpenData integration tools for the IntegrationAgent.

Download strategy
-----------------
Primary: TryOpenData REST API  (https://api.tryopendata.ai/v1/datasets/{provider}/{dataset})
  - One HTTP request returns the full CSV, typically in < 1 second for cached datasets
    and ~10 s for large uncached ones (limited to 150 MB per dataset).
  - SQL filtering/ordering is applied locally via in-memory SQLite (built-in).

Fallback: MCP query_sql with parallel pagination
  - Used only when the query is multi-table (JOIN) or the REST API returns
    DATASET_TOO_LARGE or another hard error.
  - MCP query_sql only exposes 20 rows per response regardless of LIMIT,
    so throughput is ~35 rows/s unparallelised; parallelism helps but is
    fundamentally bottlenecked by per-call MCP latency (~570 ms/call).

Parallelism model (MCP path)
----------------------------
N workers each open one persistent ClientSession and share one httpx.AsyncClient.
COUNT(*) and page-0 are dispatched simultaneously (zero pre-flight overhead).
"""

from __future__ import annotations

import asyncio
import csv as csv_module
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx

from mcp_infra.registry import MCP_REGISTRY
from tools.opendata_parser import _parse_query_result
from tools.workspace_tools import _get_workspace_path

if TYPE_CHECKING:
    from agents.state import AgentState

logger = logging.getLogger(__name__)

# ── Tuning knobs ─────────────────────────────────────────────────────────────
_OPENDATA_PAGE_SIZE = 10_000  # MCP rows per call (display cap is 20, so this is nominal)
_OPENDATA_MAX_ROWS = 1_000_000  # absolute hard cap
_OPENDATA_ROW_LIMIT = 500_000  # reject datasets larger than this up-front
_OPENDATA_CONCURRENCY = 8  # parallel MCP session workers

_REST_API_BASE = "https://api.tryopendata.ai/v1/datasets"
_REST_TIMEOUT = 180  # seconds — large datasets can take time

# Use HTTP/2 if the h2 package is installed (better multiplexing)
try:
    import h2  # noqa: F401

    _HTTP2 = True
except ImportError:
    _HTTP2 = False


# ── REST API path ─────────────────────────────────────────────────────────────


def _parse_from_table(sql: str) -> Optional[Tuple[str, str]]:
    """
    Extract (provider, dataset) from a single-table FROM clause.
    Returns None for multi-table queries (JOINs, subqueries).
    """
    m = re.search(
        r'\bFROM\s+"?([a-z][a-z0-9_-]+)/([a-z][a-z0-9_-]+)"?',
        sql,
        re.IGNORECASE,
    )
    if not m:
        return None
    provider, dataset = m.group(1).lower(), m.group(2).lower()
    after = sql[m.end() :]
    if re.search(r"\bJOIN\b", after, re.IGNORECASE):
        return None
    return provider, dataset


async def _rest_download_csv(
    api_key: str,
    provider: str,
    dataset: str,
) -> bytes:
    """
    Download the full dataset CSV in one HTTP request from the REST API.
    Raises ValueError on any API error (including DATASET_TOO_LARGE).
    """
    url = f"{_REST_API_BASE}/{provider}/{dataset}"
    headers = {"X-API-Key": api_key}

    async with httpx.AsyncClient(headers=headers, timeout=_REST_TIMEOUT, http2=_HTTP2) as http:
        async with http.stream("GET", url, params={"format": "csv"}) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ValueError(
                    f"REST API {resp.status_code}: {body[:300].decode('utf-8', errors='replace')}"
                )
            chunks: List[bytes] = []
            async for chunk in resp.aiter_bytes(131_072):
                chunks.append(chunk)
        raw = b"".join(chunks)

    # DATASET_TOO_LARGE and similar errors arrive as JSON with HTTP 200.
    if raw[:1] == b"{":
        try:
            err = json.loads(raw)
            detail = err.get("detail", {})
            if isinstance(detail, dict):
                code = detail.get("code", "")
                msg = detail.get("message", str(err))
                raise ValueError(f"{code}: {msg}" if code else msg)
            raise ValueError(str(detail))
        except json.JSONDecodeError:
            pass

    return raw


def _is_select_star_only(sql: str) -> bool:
    """True when sql is exactly 'SELECT * FROM table' with no other clauses."""
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()
    return bool(re.match(r"^select\s+\*\s+from\s+\S+$", normalized))


def _apply_sql_with_sqlite(
    csv_bytes: bytes,
    sql: str,
    provider: str,
    dataset: str,
) -> Tuple[List[str], List]:
    """
    Load downloaded CSV into in-memory SQLite and execute the original SQL
    (with table name remapped).  Returns (headers, rows) as Python lists.
    """
    import sqlite3

    import pandas as pd

    df = pd.read_csv(io.BytesIO(csv_bytes))
    if df.empty:
        return list(df.columns), []

    table_id = re.sub(r"[^a-z0-9_]", "_", f"{provider}_{dataset}".lower())
    conn = sqlite3.connect(":memory:")
    try:
        df.to_sql(table_id, conn, index=False, if_exists="replace")

        rewritten = sql.strip().rstrip(";")
        for pat in (
            f'"{provider}/{dataset}"',
            f"'{provider}/{dataset}'",
            f"{provider}/{dataset}",
        ):
            rewritten = re.sub(re.escape(pat), table_id, rewritten, flags=re.IGNORECASE)

        result_df = pd.read_sql_query(rewritten, conn)
    finally:
        conn.close()

    headers = list(result_df.columns)
    rows = result_df.values.tolist()
    return headers, rows


async def _fetch_via_rest(
    sql: str,
    provider: str,
    dataset: str,
    api_key: str,
    out_path: Path,
    state: Optional["AgentState"],
) -> Tuple[int, int, float]:
    """
    Download via REST API, apply any SQL filtering locally, write to out_path.
    Returns (total_rows, col_count, elapsed_seconds).
    Raises ValueError if download fails.
    """
    t0 = time.perf_counter()
    logger.info("[Integration] REST download: %s/%s", provider, dataset)

    csv_bytes = await _rest_download_csv(api_key, provider, dataset)
    elapsed_dl = time.perf_counter() - t0
    logger.info(
        "[Integration] REST download complete: %.1f KB in %.2fs",
        len(csv_bytes) / 1024,
        elapsed_dl,
    )

    if _is_select_star_only(sql):
        # No transformation needed — write directly.
        out_path.write_bytes(csv_bytes)
        import pandas as pd

        df_check = pd.read_csv(io.BytesIO(csv_bytes), low_memory=False)
        total_rows, col_count = len(df_check), len(df_check.columns)
        headers = list(df_check.columns)
    else:
        # Apply WHERE / ORDER BY / LIMIT / column selection via SQLite.
        try:
            headers, rows = _apply_sql_with_sqlite(csv_bytes, sql, provider, dataset)
        except Exception as exc:
            logger.warning("[Integration] SQLite transform failed (%s) — saving raw CSV", exc)
            # Fall back: save raw CSV without transformation.
            out_path.write_bytes(csv_bytes)
            import pandas as pd

            df_check = pd.read_csv(io.BytesIO(csv_bytes), low_memory=False)
            total_rows, col_count = len(df_check), len(df_check.columns)
            headers = list(df_check.columns)
        else:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv_module.writer(f)
                w.writerow(headers)
                w.writerows(rows)
            total_rows, col_count = len(rows), len(headers)

    elapsed = time.perf_counter() - t0

    # Load into state.data_context if available.
    if state is not None and out_path.exists():
        _load_csv_into_state(out_path, out_path.name, sql, state)

    return total_rows, col_count, elapsed


# ── MCP path helpers ──────────────────────────────────────────────────────────


def _raw_from_result(result: Any) -> str:
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


async def _session_run_sql(http_client: httpx.AsyncClient, url: str, sql: str) -> str:
    """Open a fresh MCP session, run one SQL query, return raw response text."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url, http_client=http_client) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool("query_sql", {"sql": sql})
            return _raw_from_result(result)


async def _count_query(http_client: httpx.AsyncClient, url: str, sql_base: str) -> Optional[int]:
    """Return total row count for sql_base, or None if the query fails."""
    fm = re.search(r"\bFROM\b", sql_base, re.IGNORECASE)
    if not fm:
        return None
    count_sql = "SELECT COUNT(*) " + sql_base[fm.start() :]
    try:
        raw = await _session_run_sql(http_client, url, count_sql)
        _, rows = _parse_query_result(raw)
        if rows and len(rows) == 1 and rows[0]:
            return int(rows[0][0])
    except Exception:
        logger.warning("[Integration] pre-flight count failed", exc_info=True)
    return None


async def _worker(
    http_client: httpx.AsyncClient,
    url: str,
    sql_base: str,
    offsets: List[int],
    page_size: int,
    worker_id: int,
) -> List[Tuple[int, str]]:
    """Fetch a slice of pages inside one persistent MCP session."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    results: List[Tuple[int, str]] = []
    async with streamable_http_client(url, http_client=http_client) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            for offset in offsets:
                page_sql = f"{sql_base} LIMIT {page_size} OFFSET {offset}"
                try:
                    result = await session.call_tool("query_sql", {"sql": page_sql})
                    raw = _raw_from_result(result)
                except Exception as exc:
                    logger.warning(
                        "[Integration] worker %d error at offset=%d: %s",
                        worker_id,
                        offset,
                        exc,
                    )
                    break
                results.append((offset, raw))
                logger.debug(
                    "[Integration] worker=%d offset=%d chars=%d", worker_id, offset, len(raw)
                )
    return results


async def _adaptive_sequential_fetch(
    http_client: httpx.AsyncClient,
    url: str,
    sql_base: str,
    page_size: int,
) -> List[Tuple[int, str]]:
    """
    Sequential fallback for the count-unknown MCP path.
    Pages through with one persistent session until an empty/partial result.
    Returns (offset, raw_text) pairs starting from page 1.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    results: List[Tuple[int, str]] = []
    offset = page_size

    async with streamable_http_client(url, http_client=http_client) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            while offset < _OPENDATA_MAX_ROWS:
                page_sql = f"{sql_base} LIMIT {page_size} OFFSET {offset}"
                try:
                    result = await session.call_tool("query_sql", {"sql": page_sql})
                    raw = _raw_from_result(result)
                except Exception as exc:
                    logger.warning(
                        "[Integration] adaptive fetch error at offset=%d: %s", offset, exc
                    )
                    break
                _, rows = _parse_query_result(raw)
                if not rows:
                    break
                results.append((offset, raw))
                if len(rows) < page_size:
                    break
                offset += page_size

    return results


# ── MCP path CSV assembly ─────────────────────────────────────────────────────


def _get_source_col_indices(headers: List[str]) -> frozenset:
    return frozenset(i for i, h in enumerate(headers) if h.startswith("_source_"))


def _strip_source_cols(
    headers: List[str], rows: List[List[str]], indices: frozenset
) -> Tuple[List[str], List[List[str]]]:
    if not indices:
        return headers, rows
    h = [h for i, h in enumerate(headers) if i not in indices]
    r = [[c for i, c in enumerate(row) if i not in indices] for row in rows]
    return h, r


def _clean_rows(rows: List[List[str]], expected_cols: int) -> List[List[str]]:
    return [
        r
        for r in rows
        if not (len(r) == 1 and re.search(r"^[-+\s]+$", r[0])) and len(r) == expected_cols
    ]


def _pages_to_csv(
    writer: "csv_module.writer",
    page_data: List[Tuple[int, str]],
) -> Tuple[List[str], int]:
    headers_written: List[str] = []
    source_col_indices: Optional[frozenset] = None
    total = 0

    for _offset, raw in page_data:
        page_headers, rows = _parse_query_result(raw)
        if not rows:
            continue

        if source_col_indices is None:
            source_col_indices = _get_source_col_indices(page_headers)
            if source_col_indices:
                dropped = [page_headers[i] for i in sorted(source_col_indices)]
                logger.info("[Integration] filtering metadata columns: %s", dropped)

        page_headers, rows = _strip_source_cols(
            page_headers, rows, source_col_indices or frozenset()
        )
        rows = _clean_rows(rows, len(page_headers))

        if not headers_written:
            headers_written = page_headers
            writer.writerow(page_headers)

        writer.writerows(rows)
        total += len(rows)

    return headers_written, total


# ── State loader (shared between both paths) ──────────────────────────────────


def _load_csv_into_state(out_path: Path, filename: str, sql: str, state: "AgentState") -> None:
    try:
        from datetime import datetime
        from uuid import uuid4

        import pandas as pd

        from agents.state import DataContext  # noqa: PLC0415

        df = pd.read_csv(out_path)
        dc = DataContext(
            file_id=str(uuid4()),
            filename=filename,
            raw_dataframe=df.copy(deep=True),
            working_dataframe=df.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "opendata",
                "workspace_path": str(out_path),
                "sql_query": sql,
            },
        )
        state.set_data_context(dc)
        logger.info("[Integration] loaded %s into DataContext (%d rows)", filename, len(df))
    except Exception as exc:
        logger.warning("[Integration] could not load %s into DataContext: %s", filename, exc)


# ── MCP paginated implementation ──────────────────────────────────────────────


async def _fetch_via_mcp(
    sql_base: str,
    out_path: Path,
    url: str,
    http_headers: Dict[str, str],
    state: Optional["AgentState"],
) -> Tuple[int, int, float]:
    """
    MCP pagination fallback.  Each MCP query_sql call returns at most 20 rows
    regardless of the LIMIT requested (TryOpenData display cap), so throughput
    is low (~35 rows/s serial, better with N parallel workers).

    Returns (total_rows, col_count, elapsed_seconds).
    """
    t0 = time.perf_counter()
    limits = httpx.Limits(
        max_connections=_OPENDATA_CONCURRENCY + 2,
        max_keepalive_connections=_OPENDATA_CONCURRENCY,
    )
    async with httpx.AsyncClient(
        headers=http_headers, timeout=120, http2=_HTTP2, limits=limits
    ) as http:
        count_coro = _count_query(http, url, sql_base)
        first_page_coro = _session_run_sql(
            http, url, f"{sql_base} LIMIT {_OPENDATA_PAGE_SIZE} OFFSET 0"
        )
        total_count, first_raw = await asyncio.gather(count_coro, first_page_coro)

        if total_count is not None and total_count > _OPENDATA_ROW_LIMIT:
            raise ValueError(
                f"Dataset has {total_count:,} rows, exceeding the {_OPENDATA_ROW_LIMIT:,}-row limit."
            )

        _, first_rows = _parse_query_result(first_raw)
        if not first_rows and (total_count is None or total_count == 0):
            raise ValueError("No rows returned from TryOpenData for this query.")

        first_page_full = len(first_rows) >= _OPENDATA_PAGE_SIZE
        if not first_page_full or (total_count is not None and total_count <= _OPENDATA_PAGE_SIZE):
            all_page_data: List[Tuple[int, str]] = [(0, first_raw)]
        elif total_count is not None:
            all_offsets = list(
                range(
                    _OPENDATA_PAGE_SIZE, min(total_count, _OPENDATA_MAX_ROWS), _OPENDATA_PAGE_SIZE
                )
            )
            num_workers = min(_OPENDATA_CONCURRENCY, max(1, len(all_offsets)))
            slices = [all_offsets[i::num_workers] for i in range(num_workers)]

            logger.info(
                "[Integration] MCP parallel fetch: %d total rows, %d workers, %d remaining pages",
                total_count,
                num_workers,
                len(all_offsets),
            )
            worker_results = await asyncio.gather(
                *[
                    _worker(http, url, sql_base, slc, _OPENDATA_PAGE_SIZE, i)
                    for i, slc in enumerate(slices)
                ],
                return_exceptions=True,
            )
            all_page_data = [(0, first_raw)]
            for wr in worker_results:
                if isinstance(wr, Exception):
                    logger.warning("[Integration] MCP worker exception: %s", wr)
                else:
                    all_page_data.extend(wr)
            all_page_data.sort(key=lambda x: x[0])
        else:
            remaining = await _adaptive_sequential_fetch(http, url, sql_base, _OPENDATA_PAGE_SIZE)
            all_page_data = [(0, first_raw)] + remaining
            all_page_data.sort(key=lambda x: x[0])

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer_obj = csv_module.writer(f)
            headers_written, total_rows = _pages_to_csv(writer_obj, all_page_data)

    elapsed = time.perf_counter() - t0

    if state is not None and out_path.exists():
        _load_csv_into_state(out_path, out_path.name, sql_base, state)

    return total_rows, len(headers_written), elapsed


# ── Main implementation ───────────────────────────────────────────────────────


async def _fetch_opendata_to_workspace_impl(
    sql: str,
    filename: str,
    workspace: Path,
    credentials: Dict[str, str],
    state: Optional["AgentState"] = None,
) -> str:
    """
    Fetch a TryOpenData dataset and write it to workspace as a CSV.

    Download order:
    1. Parse the FROM clause to extract provider/dataset.
    2. Try the REST API (https://api.tryopendata.ai/v1/datasets/{p}/{d}?format=csv).
       Apply any SQL WHERE/ORDER/LIMIT locally with in-memory SQLite.
    3. On REST failure (DATASET_TOO_LARGE, multi-table SQL, network error):
       fall back to MCP query_sql parallel pagination.
    4. Load the resulting CSV into state.data_context for immediate analysis.
    """
    opendata_def = MCP_REGISTRY.get("opendata")
    if not opendata_def:
        return "Error: 'opendata' server not found in registry."

    api_key = credentials.get("api_key", "")
    if not api_key:
        return "Error: TryOpenData API key is missing. Add it in Settings → Integrations."

    safe_name = Path(filename.strip()).name or "opendata_export.csv"
    if not safe_name.lower().endswith(".csv"):
        safe_name += ".csv"
    out_path = workspace / safe_name

    sql_base = sql.rstrip().rstrip(";")

    logger.info("[Integration] fetch_opendata → %s  sql=%.200s", safe_name, sql_base)

    table_ref = _parse_from_table(sql_base)
    mcp_fallback_reason = ""

    if table_ref:
        provider, dataset = table_ref
        try:
            total_rows, col_count, elapsed = await _fetch_via_rest(
                sql_base,
                provider,
                dataset,
                api_key,
                out_path,
                state,
            )
            if total_rows == 0:
                out_path.unlink(missing_ok=True)
                return (
                    "No rows returned from TryOpenData for this query. "
                    "Try a different SQL or verify the dataset with get_dataset_info."
                )
            file_size_kb = out_path.stat().st_size / 1024
            mbps = (file_size_kb / 1024) / elapsed if elapsed > 0 else 0
            logger.info(
                "[Integration] REST complete: %d rows × %d cols → %s (%.1f KB in %.2fs = %.2f MB/s)",
                total_rows,
                col_count,
                safe_name,
                file_size_kb,
                elapsed,
                mbps,
            )
            return (
                f"Saved {total_rows:,} rows × {col_count} columns to '{safe_name}' "
                f"({file_size_kb:.1f} KB). "
                "The dataset has been loaded into the session — the analysis agent can "
                "now analyze it directly."
            )
        except ValueError as exc:
            err_str = str(exc)
            if "DATASET_TOO_LARGE" in err_str:
                mcp_fallback_reason = (
                    "Dataset exceeds the 150 MB single-download limit. "
                    "Falling back to paginated MCP fetch (slower for large datasets)."
                )
            else:
                mcp_fallback_reason = f"REST API unavailable ({err_str[:120]}); using MCP fallback."
            logger.warning("[Integration] REST failed (%s) — falling back to MCP", err_str[:120])
        except Exception as exc:
            mcp_fallback_reason = f"REST error ({exc!s:.120}); using MCP fallback."
            logger.warning("[Integration] REST error — falling back to MCP: %s", exc)
    else:
        mcp_fallback_reason = "Multi-table or complex SQL — using MCP query_sql directly."

    # ── MCP fallback ──────────────────────────────────────────────────────────
    has_limit = bool(re.search(r"\bLIMIT\b", sql_base, re.IGNORECASE))
    url = opendata_def.http_url or ""
    http_headers = {"X-API-Key": api_key}

    if has_limit:
        # Single MCP call for explicit-LIMIT queries.
        t0 = time.perf_counter()
        async with httpx.AsyncClient(headers=http_headers, timeout=120, http2=_HTTP2) as http:
            raw = await _session_run_sql(http, url, sql_base)
        page_headers, rows = _parse_query_result(raw)
        if not page_headers:
            return (
                f"Error: could not parse TryOpenData response. "
                f"Raw (first 600 chars): {raw[:600]}"
            )
        src_idx = _get_source_col_indices(page_headers)
        page_headers, rows = _strip_source_cols(page_headers, rows, src_idx)
        rows = _clean_rows(rows, len(page_headers))
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv_module.writer(f)
            w.writerow(page_headers)
            w.writerows(rows)
        total_rows = len(rows)
        col_count = len(page_headers)
        elapsed = time.perf_counter() - t0
        if state is not None:
            _load_csv_into_state(out_path, safe_name, sql_base, state)
    else:
        try:
            total_rows, col_count, elapsed = await _fetch_via_mcp(
                sql_base,
                out_path,
                url,
                http_headers,
                state,
            )
        except ValueError as exc:
            return f"Error: {exc}"

    if total_rows == 0:
        out_path.unlink(missing_ok=True)
        return (
            "No rows returned from TryOpenData for this query. "
            "Try a different SQL or verify the dataset with get_dataset_info."
        )

    file_size_kb = out_path.stat().st_size / 1024
    mbps = (file_size_kb / 1024) / elapsed if elapsed > 0 else 0
    logger.info(
        "[Integration] MCP complete: %d rows × %d cols → %s (%.1f KB in %.2fs = %.2f MB/s)",
        total_rows,
        col_count,
        safe_name,
        file_size_kb,
        elapsed,
        mbps,
    )

    fallback_note = f" (Note: {mcp_fallback_reason})" if mcp_fallback_reason else ""
    return (
        f"Saved {total_rows:,} rows × {col_count} columns to '{safe_name}' "
        f"({file_size_kb:.1f} KB).{fallback_note} "
        "The dataset has been loaded into the session — the analysis agent can "
        "now analyze it directly."
    )


async def handle_fetch_opendata(
    inp: Any, creds_map: Dict[str, Dict[str, str]], state: Optional["AgentState"]
) -> str:
    sql_arg = inp.get("sql", "") if isinstance(inp, dict) else str(inp)
    filename_arg = (
        inp.get("filename", "opendata_export.csv")
        if isinstance(inp, dict)
        else "opendata_export.csv"
    )

    if not sql_arg:
        return "Error: 'sql' parameter is required."

    opendata_creds = creds_map.get("opendata", {})
    if not opendata_creds.get("api_key"):
        return (
            "Error: TryOpenData is not connected or missing its API key. "
            "Go to Settings → Integrations to add your API key."
        )

    workspace = _get_workspace_path(state)
    if not workspace:
        return "Error: cannot determine workspace path for this session."
    workspace.mkdir(parents=True, exist_ok=True)

    return await _fetch_opendata_to_workspace_impl(
        sql=sql_arg,
        filename=filename_arg,
        workspace=workspace,
        credentials=opendata_creds,
        state=state,
    )
