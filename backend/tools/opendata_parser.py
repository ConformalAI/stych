"""
Response parser for TryOpenData query_sql results.

TryOpenData can return data in multiple formats depending on the query and server
version. _parse_query_result tries each format in order and returns (headers, rows).
_resolve_headers reconciles the column names declared in the envelope with those
actually present in the data section.
"""

from __future__ import annotations

import csv as csv_module
import io
import json as json_module
import re
from typing import List, Tuple


def _resolve_headers(declared: List[str], sub_h: List[str], sub_r: List[List[str]]) -> List[str]:
    """
    Choose the best column-name list to use as CSV headers.

    TryOpenData sends column names ONLY in the envelope ("14 columns: …")
    and returns the data section without a header row.  The CSV fallback in
    _parse_query_result then misidentifies the first data row as headers
    (sub_h becomes a row of real data values).

    Strategy
    --------
    1. If declared is empty, trust sub_h (explicit header returned by server).
    2. If declared is non-empty AND sub_h looks like column names (no URLs,
       no plain numbers), prefer sub_h (server returned its own header row).
    3. Otherwise (sub_h is a data row — contains URLs / numbers) treat sub_h
       as the first data row that was accidentally consumed as a header and use
       declared instead, trimming to match the actual data width.

    The trimming step handles TryOpenData's occasional declaration of
    _source_page which is not present in the actual row data.
    """
    if not declared:
        return sub_h

    def _looks_like_col_names(row: List[str]) -> bool:
        if not row:
            return False
        for v in row:
            if v.startswith("http://") or v.startswith("https://"):
                return False
            if v.strip().lstrip("-").isdigit():
                return False
        return True

    if _looks_like_col_names(sub_h):
        return sub_h

    # sub_h is actually a data row — use the envelope declaration, trimmed to data width.
    data_width = len(sub_h) if sub_h else (len(sub_r[0]) if sub_r else len(declared))
    if len(declared) == data_width:
        return declared
    if len(declared) > data_width:
        # Drop _source_* cols that are absent from actual rows (e.g. _source_page metadata).
        trimmed = [c for c in declared if c == "_source_url" or not c.startswith("_source_")]
        if len(trimmed) == data_width:
            return trimmed
        return declared[:data_width]
    return declared


def _parse_query_result(text: str) -> Tuple[List[str], List[List[str]]]:
    """
    Parse a query_sql text response from TryOpenData into (headers, rows).

    Handles the following formats (tried in order):
      0. TryOpenData envelope:       "N rows returned\nK columns: ...\n\n<data>"
                                     N=0 → ([], []); N>0 → recurse on data section
      1. JSON array of objects:      [{"col": val, ...}, ...]
      2. JSON array of arrays:       [["col1","col2",...], [v1,v2,...], ...]
      3. JSON dict with data key:    {"data": [...], "total": N, ...}
      4. JSONL (one JSON obj/line):  {"col":val}\n{"col":val}\n...
      5. Markdown table:             | col | col |\n|---|---|\n| v | v |
     5b. PostgreSQL pipe table:      col1 | col2\n---+---\n val1 | val2
                                     (TryOpenData's actual data format — no leading |)
      6. CSV / TSV

    Returns ([], []) if the response is an error message or unparseable.
    """
    text = text.strip()
    if not text:
        return [], []

    # ── 0. TryOpenData "N rows returned" plain-text envelope ─────────────
    # This guard MUST run before the CSV fallback, which would otherwise parse
    # each metadata line as a fake data row, producing garbage and causing the
    # pagination loop to never terminate.
    _row_count_match = re.match(r"^(\d+)\s+rows?\s+returned\b", text, re.IGNORECASE)
    if _row_count_match:
        n_rows = int(_row_count_match.group(1))
        if n_rows == 0:
            return [], []

        _env_col = re.search(r"^\d+\s+columns?:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
        declared_headers: List[str] = (
            [c.strip() for c in _env_col.group(1).split(",")] if _env_col else []
        )

        col_line = re.search(r"^Columns:\s*(.+)$", text, re.MULTILINE)
        if col_line:
            if not declared_headers:
                declared_headers = [c.strip() for c in col_line.group(1).split(",")]
            after = text[col_line.end() :].strip()
            after = re.sub(
                r"\n?Open this query in the sandbox:.*$", "", after, flags=re.DOTALL
            ).strip()
            if after and after != "(no rows)":
                sub_h, sub_r = _parse_query_result(after)
                if sub_r:
                    headers = _resolve_headers(declared_headers, sub_h, sub_r)
                    data = ([sub_h] + sub_r) if (headers != sub_h and sub_h) else sub_r
                    return headers, data
            return [], []

        # No "Columns:" line — data table follows after the first blank line.
        sections = re.split(r"\n{2,}", text)
        for section in sections[1:]:
            section = re.sub(
                r"\nOpen this query in the sandbox:.*$", "", section.strip(), flags=re.DOTALL
            ).strip()
            if section:
                sub_h, sub_r = _parse_query_result(section)
                if sub_r:
                    headers = _resolve_headers(declared_headers, sub_h, sub_r)
                    data = ([sub_h] + sub_r) if (headers != sub_h and sub_h) else sub_r
                    return headers, data
        return [], []

    # ── 1–3. JSON ─────────────────────────────────────────────────────────
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json_module.loads(text)

            if isinstance(data, list) and data and isinstance(data[0], dict):
                headers = list(data[0].keys())
                rows = [[str(row.get(h, "")) for h in headers] for row in data]
                return headers, rows

            if isinstance(data, list) and data and isinstance(data[0], list):
                return [str(h) for h in data[0]], [[str(v) for v in row] for row in data[1:]]

            if isinstance(data, dict):
                for key in ("data", "rows", "results", "records", "items"):
                    inner = data.get(key)
                    if isinstance(inner, list) and inner:
                        if isinstance(inner[0], dict):
                            headers = list(inner[0].keys())
                            rows = [[str(r.get(h, "")) for h in headers] for r in inner]
                            return headers, rows
                        if isinstance(inner[0], list):
                            return [str(h) for h in inner[0]], [
                                [str(v) for v in row] for row in inner[1:]
                            ]
        except Exception:
            pass

    # ── 4. JSONL ──────────────────────────────────────────────────────────
    lines_raw = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines_raw and lines_raw[0].startswith("{"):
        try:
            parsed = [json_module.loads(ln) for ln in lines_raw if ln.startswith("{")]
            if parsed and isinstance(parsed[0], dict):
                headers = list(parsed[0].keys())
                rows = [[str(row.get(h, "")) for h in headers] for row in parsed]
                return headers, rows
        except Exception:
            pass

    # ── 5. Markdown table (lines start and end with |) ────────────────────
    if "|" in text:
        table_lines = [ln.strip() for ln in lines_raw if ln.startswith("|")]
        if len(table_lines) >= 2:

            def _pipe_row(line: str) -> List[str]:
                return [p.strip() for p in line.strip("|").split("|")]

            headers = _pipe_row(table_lines[0])
            data_lines = [ln for ln in table_lines[1:] if not re.match(r"\|[\s\-:|]+\|", ln)]
            rows = [_pipe_row(ln) for ln in data_lines]
            if headers and rows:
                return headers, rows

    # ── 5b. PostgreSQL-style pipe table (TryOpenData's default data format) ─
    # Columns separated by " | " but lines do NOT start/end with "|".
    if "|" in text:
        pg_lines = [ln for ln in lines_raw if "|" in ln and not ln.startswith("|")]
        if len(pg_lines) >= 2:
            pg_headers = [c.strip() for c in pg_lines[0].split("|")]
            pg_rows = [[c.strip() for c in ln.split("|")] for ln in pg_lines[1:]]
            n_cols = len(pg_headers)
            pg_rows = [r for r in pg_rows if len(r) == n_cols]
            if pg_headers and pg_rows:
                return pg_headers, pg_rows

    # ── 6. CSV / TSV ──────────────────────────────────────────────────────
    try:
        dialect = "excel-tab" if "\t" in text else "excel"
        reader = csv_module.reader(io.StringIO(text), dialect=dialect)
        all_rows = list(reader)
        if len(all_rows) >= 2:
            return all_rows[0], all_rows[1:]
    except Exception:
        pass

    return [], []
