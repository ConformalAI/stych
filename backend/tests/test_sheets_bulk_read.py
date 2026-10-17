"""
Regression test for the Google Sheets → workspace read returning "no data".

`read_spreadsheet_to_workspace` reads the whole sheet server-side (rows never
reach the LLM), so the MCP get_values response can be far larger than the
MAX_RESULT_CHARS context-protection cap. Routing that bulk read through the cap
truncated the JSON mid-token, so `_parse_values_response` failed to parse it and
returned [], which the tool surfaced as "returned no data — nothing saved" for
every non-trivial sheet. The read MUST use the untruncated (full=True) path.
"""

from __future__ import annotations

import json

from mcp_infra.mcp_client import MAX_RESULT_CHARS, _format_result
from tools.sheets_tools import _parse_values_response


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    isError = False

    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


def _large_value_range_json() -> str:
    """A get_values response whose pretty-printed JSON exceeds MAX_RESULT_CHARS."""
    header = ["sepal_length", "sepal_width", "petal_length", "petal_width", "species"]
    rows = [header] + [["5.1", "3.5", "1.4", "0.2", "setosa"]] * 150
    raw = json.dumps(
        {"range": "Iris Data!A1:E151", "majorDimension": "ROWS", "values": rows}, indent=2
    )
    assert len(raw) > MAX_RESULT_CHARS  # guard: the scenario must actually exceed the cap
    return raw


def test_capped_read_corrupts_large_json_to_zero_rows():
    """The bug: the default cap clips the JSON so it parses to no rows."""
    capped = _format_result(_Result(_large_value_range_json()))
    assert _parse_values_response(capped) == []


def test_full_read_preserves_all_rows():
    """The fix: full=True (max_chars=0) returns the whole payload intact."""
    full = _format_result(_Result(_large_value_range_json()), max_chars=0)
    rows = _parse_values_response(full)
    assert len(rows) == 151
    assert rows[0][0] == "sepal_length"
