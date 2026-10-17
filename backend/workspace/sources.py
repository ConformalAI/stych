"""
Unified source catalog: the single list of everything the orchestrator and
sub-agents can work on — workspace data files plus any connected SQL source —
with no file getting special treatment.

This is the file-first counterpart to the legacy ``working_dataframe`` identity:
callers ask "what sources exist?" and hand a source id to a sub-agent, rather
than assuming one privileged active frame. See docs/UNIFIED_SOURCE_MODEL.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from workspace.manager import dataset_stem, list_workspace_artifacts

logger = logging.getLogger(__name__)

# Tabular files that can back a source. Text/report artifacts are not sources.
_TABLE_SUFFIXES = frozenset({"csv", "tsv", "xlsx", "parquet"})
# Prefer the lossless representation when several mirror copies share one identity.
_SUFFIX_RANK = {"parquet": 0, "csv": 1, "tsv": 2, "xlsx": 3}
_MAX_COLS_LISTED = 30


def _peek_schema(path: Path) -> tuple[List[str], Optional[int]]:
    """Cheap (columns, row_count) without loading the whole frame. Row count is
    only returned when it is cheap to know (parquet metadata)."""
    suffix = path.suffix.lower().lstrip(".")
    try:
        if suffix == "parquet":
            import pyarrow.parquet as pq  # noqa: PLC0415

            pf = pq.ParquetFile(path)
            return list(pf.schema_arrow.names), pf.metadata.num_rows
        if suffix in ("csv", "tsv"):
            sep = "\t" if suffix == "tsv" else ","
            return list(pd.read_csv(path, nrows=0, sep=sep).columns), None
        if suffix == "xlsx":
            return list(pd.read_excel(path, nrows=0).columns), None
    except Exception as exc:  # noqa: BLE001 — a bad file must not break the catalog
        logger.debug("[sources] schema peek failed for %s: %s", path, exc)
    return [], None


def _column_names(cols: List[Any]) -> List[str]:
    names = [str(c) for c in cols[:_MAX_COLS_LISTED]]
    if len(cols) > _MAX_COLS_LISTED:
        names.append("…")
    return names


def list_sources(state: Any, workspace_root: Path) -> List[Dict[str, Any]]:
    """Every input the orchestrator can direct a sub-agent to, as uniform dicts:

        {id, kind: "file"|"sql"|"dataset", connector, name, rows, columns,
         column_names, active, note}

    ``connector`` names which system a source came from ("upload", "bigquery",
    "databricks", …) so the orchestrator can tell two connected tables apart and
    address the right one.

    Mirror copies of one dataset (``<stem>.parquet``/``.csv`` + the raw upload
    name) collapse to a single entry, preferring the lossless parquet path as the
    id. The SQL datasource, when present, appears alongside files as one entry.
    """
    active_stem = None
    try:
        active_stem = (state.metadata or {}).get("active_dataset") if state else None
    except Exception:  # noqa: BLE001
        active_stem = None
    active_stem = dataset_stem(active_stem) if active_stem else None

    # Group workspace table files by canonical identity so mirrors collapse.
    groups: Dict[str, Path] = {}
    if workspace_root and Path(workspace_root).is_dir():
        for art in list_workspace_artifacts(Path(workspace_root)):
            if art["suffix"] not in _TABLE_SUFFIXES:
                continue
            path = Path(workspace_root) / art["path"]
            identity = dataset_stem(art["name"])
            current = groups.get(identity)
            if current is None or _SUFFIX_RANK.get(
                path.suffix.lower().lstrip("."), 9
            ) < _SUFFIX_RANK.get(current.suffix.lower().lstrip("."), 9):
                groups[identity] = path

    sources: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for identity, path in sorted(groups.items()):
        cols, rows = _peek_schema(path)
        seen.add(identity)
        sources.append(
            {
                "id": str(path.relative_to(workspace_root)),
                "kind": "file",
                "connector": "upload",
                "name": path.name,
                "rows": rows,
                "columns": len(cols) if cols else None,
                "column_names": _column_names(cols),
                "active": bool(active_stem) and identity == active_stem,
                "note": None,
            }
        )

    # In-memory datasets (primary + additional uploads). All uploads are normally
    # seeded to disk and already listed above; this is a fallback for any dataset not
    # yet materialized as a file (e.g. before the first agent run seeds it), so the
    # catalog never hides a dataset from the orchestrator. SQL contexts are handled below.
    if state and hasattr(state, "get_all_data_contexts"):
        contexts = state.get_all_data_contexts()
    else:
        # A state without the multi-context API still has one context; dropping
        # it here would hide the session's only source from the catalog.
        primary_ctx = getattr(state, "data_context", None) if state else None
        contexts = [primary_ctx] if primary_ctx is not None else []
    for ctx in contexts:
        is_remote = bool(
            callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source()
        )
        if ctx is None or ctx.is_sql_datasource() or is_remote:
            continue
        identity = dataset_stem(ctx.filename)
        if identity in seen:
            continue
        seen.add(identity)
        df = ctx.working_dataframe
        cols = list(df.columns) if df is not None else []
        sources.append(
            {
                "id": f"dataset:{ctx.filename}",
                "kind": "dataset",
                "connector": str((ctx.metadata or {}).get("source") or "upload"),
                "name": ctx.filename or "dataset",
                "rows": int(len(df)) if df is not None else None,
                "columns": len(cols) if cols else None,
                "column_names": _column_names(cols),
                "active": bool(active_stem) and identity == active_stem,
                "note": "in-memory dataset (not yet a workspace file)",
            }
        )

    # SQL datasources: virtual sources (connection + scratch table), not files.
    # Every connected table is listed, not just the primary one — a session can
    # hold several, and hiding the rest made multi-source instructions unreliable.
    primary = getattr(state, "data_context", None) if state else None
    for ctx in contexts:
        is_remote = bool(
            callable(getattr(ctx, "is_remote_source", None)) and ctx.is_remote_source()
        )
        if ctx is None or not (ctx.is_sql_datasource() or is_remote):
            continue
        preview = ctx.working_dataframe
        cols = list(preview.columns) if preview is not None else []
        meta = ctx.metadata or {}
        connector = str(meta.get("source") or "sql")
        target = meta.get("temp_table") or meta.get("table") or ctx.filename
        label = meta.get("source_label") or meta.get("table") or ctx.filename
        sources.append(
            {
                "id": f"sql:{target}",
                "kind": "sql",
                "connector": connector,
                "name": ctx.filename or str(target),
                "rows": None,  # full table lives in the DB; unknown without a query
                "columns": len(cols) if cols else None,
                "column_names": _column_names(cols),
                "active": ctx is primary,
                "note": (
                    f"{connector} table {label} — a connected data source, not an "
                    "integration; query it with the SQL/analysis agent (run_sql)"
                ),
            }
        )

    return sources


def render_sources_block(sources: List[Dict[str, Any]]) -> str:
    """Compact human/LLM-readable catalog for the orchestrator prompt."""
    if not sources:
        return ""
    lines = [
        "Workspace sources (files + connections; pass one's id as call_agent "
        "`input` to direct a sub-agent):"
    ]
    for s in sources:
        dims = ""
        if s["rows"] is not None and s["columns"] is not None:
            dims = f", {s['rows']}×{s['columns']}"
        elif s["columns"] is not None:
            dims = f", {s['columns']} cols"
        flags = " [active]" if s["active"] else ""
        connector = s.get("connector") or ""
        if s["kind"] == "file":
            kind = ""
        elif s["kind"] == "sql":
            kind = f" (sql · {connector})"
        else:
            kind = f" ({s['kind']})"
        cols = ", ".join(s["column_names"]) if s["column_names"] else ""
        lines.append(f"- `{s['id']}`{kind}{flags}{dims}: {cols}")
    return "\n".join(lines)
