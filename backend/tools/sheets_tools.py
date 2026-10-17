"""
Google Sheets direct-export tools for the IntegrationAgent.

Reads workspace CSV files server-side and writes them to Google Sheets via MCP.
Raw data rows never pass through the LLM context.
"""

from __future__ import annotations

import csv as csv_module
import json as json_module
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_infra.mcp_client import MCPClientManager
from mcp_infra.registry import MCP_REGISTRY
from tools.workspace_tools import _get_workspace_path

if False:  # TYPE_CHECKING without circular import
    pass

logger = logging.getLogger(__name__)

_SHEETS_BATCH_SIZE = 5_000  # max rows per update_values call
_SHEETS_MAX_ROWS = 50_000  # hard cap to prevent runaway writes


async def _write_csv_to_spreadsheet_impl(
    filename: str,
    workspace: Path,
    credentials: Dict[str, str],
    mcp_client: MCPClientManager,
    spreadsheet_id: Optional[str] = None,
    sheet_name: str = "Sheet1",
    title: Optional[str] = None,
) -> str:
    """
    Read a workspace CSV and write it directly to Google Sheets via MCP.

    If no spreadsheet_id is given a new spreadsheet is created first.
    For files with > _SHEETS_BATCH_SIZE rows the write is split into batches.
    """
    sheets_def = MCP_REGISTRY.get("google_sheets")
    if not sheets_def:
        return "Error: 'google_sheets' server not found in registry."

    # ── 1. Sanitise + validate path ──────────────────────────────────────
    safe_name = Path(filename.strip()).name
    if not safe_name:
        return "Error: filename is empty."
    full_path = (workspace / safe_name).resolve()
    try:
        full_path.relative_to(workspace.resolve())
    except ValueError:
        return f"Error: '{filename}' is outside the workspace."
    if not full_path.exists():
        return (
            f"Error: '{safe_name}' not found in workspace. "
            "Use list_workspace_files to see available files."
        )

    # ── 2. Read CSV server-side ──────────────────────────────────────────
    try:
        with open(full_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv_module.reader(fh)
            all_rows: List[List[str]] = [[str(c) for c in row] for row in reader]
    except Exception as exc:
        return f"Error reading '{safe_name}': {exc}"

    if not all_rows:
        return f"Warning: '{safe_name}' is empty — nothing to write."

    headers = all_rows[0]
    data_rows = all_rows[1:]
    n_cols = len(headers)

    truncated = len(data_rows) > _SHEETS_MAX_ROWS if data_rows else False
    if truncated:
        data_rows = data_rows[:_SHEETS_MAX_ROWS]

    def _normalise(row: List[str]) -> List[str]:
        if len(row) < n_cols:
            return row + [""] * (n_cols - len(row))
        return row[:n_cols]

    data_rows = [_normalise(r) for r in data_rows]
    rows_to_write = [headers] + data_rows
    n_rows = len(data_rows)

    # ── 3. Create spreadsheet if needed ─────────────────────────────────
    sid = spreadsheet_id
    if not sid:
        new_title = title or safe_name.rsplit(".", 1)[0]
        create_args: Dict[str, Any] = {"title": new_title}
        if sheet_name and sheet_name != "Sheet1":
            create_args["sheet_titles"] = [sheet_name]
        logger.info(
            "[Integration] write_csv_to_spreadsheet: creating spreadsheet title=%r sheet=%r",
            new_title,
            sheet_name,
        )
        try:
            create_result = await mcp_client.call_tool_strict(
                sheets_def,
                "create_spreadsheet",
                create_args,
                credentials,
            )
        except Exception as exc:
            return f"Error creating spreadsheet: {_unwrap_exception(exc)}"
        logger.info(
            "[Integration] write_csv_to_spreadsheet: create_spreadsheet response: %s",
            create_result[:300],
        )
        try:
            create_data = json_module.loads(create_result)
            sid = create_data.get("spreadsheetId") or create_data.get("id") or ""
        except Exception:
            pass
        if not sid:
            m = re.search(r'"spreadsheetId"\s*:\s*"([^"]+)"', create_result)
            if m:
                sid = m.group(1)
        if not sid:
            return (
                f"Error: could not extract spreadsheetId from create_spreadsheet response. "
                f"Raw response: {create_result[:400]}"
            )
        logger.info("[Integration] write_csv_to_spreadsheet: spreadsheetId=%s", sid)

    # ── 4. Batch write ───────────────────────────────────────────────────
    needs_quote = " " in sheet_name or "'" in sheet_name
    safe_sheet = (
        f"'{sheet_name.replace(chr(39), chr(39) + chr(39))}'" if needs_quote else sheet_name
    )

    batch_start_row = 1
    for batch_start in range(0, len(rows_to_write), _SHEETS_BATCH_SIZE):
        batch = rows_to_write[batch_start : batch_start + _SHEETS_BATCH_SIZE]
        range_notation = f"{safe_sheet}!A{batch_start_row}"
        logger.info(
            "[Integration] write_csv_to_spreadsheet: update_values range=%s rows=%d",
            range_notation,
            len(batch),
        )
        try:
            write_result = await mcp_client.call_tool_strict(
                sheets_def,
                "update_values",
                {"spreadsheetId": sid, "range": range_notation, "values": batch},
                credentials,
            )
            logger.info(
                "[Integration] write_csv_to_spreadsheet: update_values OK: %s",
                write_result[:200],
            )
        except Exception as exc:
            return f"Error writing to spreadsheet at {range_notation}: " f"{_unwrap_exception(exc)}"
        batch_start_row += len(batch)

    # ── 5. Build response ────────────────────────────────────────────────
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    suffix = ""
    if not data_rows:
        suffix = " (header row only — CSV had no data rows)"
    elif truncated:
        suffix = f" (first {_SHEETS_MAX_ROWS:,} rows written — file exceeded limit)"
    return f"Written {n_rows:,} rows × {n_cols} columns to '{sheet_name}' in spreadsheet: {url}{suffix}"


def _unwrap_exception(exc: BaseException) -> str:
    """Unwrap anyio ExceptionGroup to surface the real underlying error."""
    cause: BaseException = exc
    while hasattr(cause, "exceptions") and cause.exceptions:  # type: ignore[attr-defined]
        cause = cause.exceptions[0]  # type: ignore[attr-defined]
    return str(cause)


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets → workspace import (mirror of write_csv_to_spreadsheet).
# Reads the sheet server-side and writes a workspace CSV — raw rows never pass
# through the LLM context, so arbitrarily large sheets are handled correctly.
# ─────────────────────────────────────────────────────────────────────────────


def _extract_spreadsheet_id(ref: str) -> str:
    """Pull a spreadsheet ID out of a full URL, an ?id= link, or a bare ID."""
    ref = (ref or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", ref)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]+", ref):
        return ref
    return ""


def _first_sheet_title(meta_raw: str) -> Optional[str]:
    """Best-effort extraction of the first tab title from get_spreadsheet output."""
    try:
        data = json_module.loads(meta_raw)
    except Exception:
        m = re.search(r'"title"\s*:\s*"([^"]+)"', meta_raw)
        return m.group(1) if m else None
    sheets = data.get("sheets") if isinstance(data, dict) else None
    if isinstance(sheets, list):
        for sh in sheets:
            if isinstance(sh, dict):
                props = sh.get("properties")
                if isinstance(props, dict) and props.get("title"):
                    return props["title"]
            elif isinstance(sh, str) and sh:
                return sh
    return None


def _parse_values_response(values_raw: str) -> List[List[str]]:
    """Parse get_values output (Sheets valueRange or a bare 2D list) into rows."""
    try:
        data = json_module.loads(values_raw)
    except Exception:
        return []
    if isinstance(data, dict):
        vals = data.get("values")
        if isinstance(vals, list):
            return [[str(c) for c in row] for row in vals if isinstance(row, list)]
        return []
    if isinstance(data, list):
        return [[str(c) for c in row] for row in data if isinstance(row, list)]
    return []


def _promote_to_active_dataset(csv_path: Path, workspace: Path, state: Any) -> str:
    """Seed the pulled CSV as the canonical active dataset for analysis/cleaning."""
    try:
        from datetime import datetime  # noqa: PLC0415

        import pandas as pd  # noqa: PLC0415

        from agents.state import DataContext, state_manager  # noqa: PLC0415
        from workspace.manager import dataset_parquet_name, seed_workspace_data  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Integration] active-dataset deps unavailable: %s", exc)
        return " (saved as a file; could not set it as the active dataset)."

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Integration] could not read %s as CSV for active dataset: %s", csv_path, exc
        )
        return " (saved as a file; could not parse it as a dataset)."

    try:
        seed_workspace_data(workspace, df, csv_path.name)
        pq_name = dataset_parquet_name(csv_path.name)
        dc = DataContext(
            file_id=f"gsheet-{csv_path.stem}",
            filename=pq_name,
            raw_dataframe=df.copy(deep=True),
            working_dataframe=df.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={"source": "google_sheets"},
        )
        if state is not None and getattr(state, "data_context", None) is None:
            state.add_data_context(dc)
        elif state is not None:
            state.data_context = dc
            state._notify_changed()
        if state is not None and getattr(state, "session_id", None):
            state_manager.mark_dirty(state.session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Integration] could not promote %s to active dataset: %s", csv_path, exc)
        return " (saved as a file; could not set it as the active dataset)."

    return f" It is now the active dataset ({len(df):,} rows) — ready to analyze or chat with."


async def _read_spreadsheet_to_workspace_impl(
    spreadsheet_ref: str,
    filename: str,
    workspace: Path,
    credentials: Dict[str, str],
    mcp_client: MCPClientManager,
    state: Any = None,
    sheet_name: Optional[str] = None,
    cell_range: Optional[str] = None,
    as_active_dataset: bool = False,
) -> str:
    """Read a Google Sheet via MCP and write it to a workspace CSV server-side."""
    sheets_def = MCP_REGISTRY.get("google_sheets")
    if not sheets_def:
        return "Error: 'google_sheets' server not found in registry."

    # ── 1. Resolve spreadsheet ID + output path ─────────────────────────
    sid = _extract_spreadsheet_id(spreadsheet_ref)
    if not sid:
        return f"Error: could not extract a spreadsheet ID from '{spreadsheet_ref}'."

    safe_name = Path(filename.strip()).name
    if not safe_name:
        return "Error: filename is empty."
    if not safe_name.lower().endswith(".csv"):
        safe_name += ".csv"
    full_path = (workspace / safe_name).resolve()
    try:
        full_path.relative_to(workspace.resolve())
    except ValueError:
        return f"Error: '{filename}' is outside the workspace."

    # ── 2. Determine the A1 range to read ───────────────────────────────
    a1_range = cell_range or sheet_name
    if not a1_range:
        try:
            meta_raw = await mcp_client.call_tool_strict(
                sheets_def, "get_spreadsheet", {"spreadsheetId": sid}, credentials
            )
            a1_range = _first_sheet_title(meta_raw) or "Sheet1"
            logger.info(
                "[Integration] read_spreadsheet: resolved range=%r from get_spreadsheet "
                "(meta preview: %s)",
                a1_range,
                meta_raw[:200],
            )
        except Exception as exc:
            a1_range = "Sheet1"
            logger.warning(
                "[Integration] read_spreadsheet: get_spreadsheet failed (%s); "
                "defaulting range to 'Sheet1'",
                _unwrap_exception(exc),
            )

    # ── 3. Read values server-side ──────────────────────────────────────
    try:
        values_raw = await mcp_client.call_tool_strict(
            sheets_def,
            "get_values",
            {"spreadsheetId": sid, "range": a1_range},
            credentials,
            full=True,  # server-side bulk read: never truncate, or the JSON won't parse
        )
    except Exception as exc:
        return f"Error reading spreadsheet {sid} range '{a1_range}': {_unwrap_exception(exc)}"

    rows = _parse_values_response(values_raw)
    logger.info(
        "[Integration] read_spreadsheet: sid=%s range=%r parsed_rows=%d raw_len=%d raw_preview=%s",
        sid,
        a1_range,
        len(rows),
        len(values_raw or ""),
        (values_raw or "")[:300],
    )
    if not rows:
        return (
            f"Warning: range '{a1_range}' in spreadsheet {sid} returned no data — nothing saved. "
            f"Check the sheet/tab name (resolved range was '{a1_range}') and that the tab has rows."
        )

    truncated = len(rows) - 1 > _SHEETS_MAX_ROWS
    if truncated:
        rows = rows[: _SHEETS_MAX_ROWS + 1]

    # ── 4. Write CSV server-side (rows never enter the LLM context) ─────
    n_cols = max((len(r) for r in rows), default=0)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with open(full_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv_module.writer(fh)
            for r in rows:
                writer.writerow(list(r) + [""] * (n_cols - len(r)))
    except Exception as exc:
        return f"Error writing '{safe_name}': {exc}"

    n_data_rows = max(len(rows) - 1, 0)

    # ── 5. Optionally promote to the canonical active dataset ───────────
    active_note = ""
    if as_active_dataset:
        active_note = _promote_to_active_dataset(full_path, workspace, state)

    suffix = f" (first {_SHEETS_MAX_ROWS:,} rows — sheet exceeded the limit)" if truncated else ""
    return (
        f"Saved {n_data_rows:,} rows × {n_cols} columns from spreadsheet {sid} "
        f"to '{safe_name}'.{active_note}{suffix}"
    )


async def handle_read_spreadsheet_to_workspace(
    inp: Any,
    creds_map: Dict[str, Dict[str, str]],
    state: Any,
    mcp_client: MCPClientManager,
) -> str:
    if not isinstance(inp, dict):
        return "Error: read_spreadsheet_to_workspace requires object arguments."

    spreadsheet_ref = inp.get("spreadsheet_url_or_id") or inp.get("spreadsheet_id") or ""
    filename_arg = inp.get("filename", "")
    sheet_name_arg = inp.get("sheet_name")
    range_arg = inp.get("range")
    as_active = bool(inp.get("as_active_dataset", False))

    if not spreadsheet_ref:
        return "Error: 'spreadsheet_url_or_id' parameter is required."
    if not filename_arg:
        return "Error: 'filename' parameter is required."

    if "google_sheets" not in creds_map:
        return (
            "Error: Google Sheets is not connected. "
            "Go to Settings → Integrations to connect it first."
        )

    sheets_creds = creds_map.get("google_sheets", {})
    sheets_def = MCP_REGISTRY.get("google_sheets")
    if sheets_def:
        missing = [k for k in sheets_def.required_credentials if not sheets_creds.get(k)]
        if missing:
            return (
                f"Error: Google Sheets is connected but missing required credentials: "
                f"{missing}. Go to Settings → Integrations and add them."
            )

    workspace = _get_workspace_path(state)
    if not workspace:
        return "Error: cannot determine workspace path for this session."

    return await _read_spreadsheet_to_workspace_impl(
        spreadsheet_ref=spreadsheet_ref,
        filename=filename_arg,
        workspace=workspace,
        credentials=sheets_creds,
        mcp_client=mcp_client,
        state=state,
        sheet_name=sheet_name_arg,
        cell_range=range_arg,
        as_active_dataset=as_active,
    )


async def handle_write_csv_to_spreadsheet(
    inp: Any,
    creds_map: Dict[str, Dict[str, str]],
    state: Any,
    mcp_client: MCPClientManager,
) -> str:
    filename_arg = inp.get("filename", "") if isinstance(inp, dict) else str(inp)
    spreadsheet_id_arg = inp.get("spreadsheet_id") if isinstance(inp, dict) else None
    sheet_name_arg = (inp.get("sheet_name") or "Sheet1") if isinstance(inp, dict) else "Sheet1"
    title_arg = inp.get("title") if isinstance(inp, dict) else None

    if not filename_arg:
        return "Error: 'filename' parameter is required."

    if "google_sheets" not in creds_map:
        return (
            "Error: Google Sheets is not connected. "
            "Go to Settings → Integrations to connect it first."
        )

    sheets_creds = creds_map.get("google_sheets", {})
    sheets_def = MCP_REGISTRY.get("google_sheets")
    if sheets_def:
        missing = [k for k in sheets_def.required_credentials if not sheets_creds.get(k)]
        if missing:
            return (
                f"Error: Google Sheets is connected but missing required credentials: "
                f"{missing}. Go to Settings → Integrations and add them."
            )

    workspace = _get_workspace_path(state)
    if not workspace:
        return "Error: cannot determine workspace path for this session."

    return await _write_csv_to_spreadsheet_impl(
        filename=filename_arg,
        workspace=workspace,
        credentials=sheets_creds,
        mcp_client=mcp_client,
        spreadsheet_id=spreadsheet_id_arg,
        sheet_name=sheet_name_arg,
        title=title_arg,
    )
