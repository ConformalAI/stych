"""
Ingestion must never silently rewrite data.

Pins the removal of implicit sentinel normalization: strings like 'UNKNOWN',
'N/A', '-' used to be wiped to NULL/NaN at upload, DB-connect, and temp-table
creation, which polluted every base-vs-working diff with changes the user
never made. Cleaning missing-like values is now an explicit agent operation
(script / sql_write_temp) like any other. Type inference is lossless: a column
converts only when every non-null value parses.
"""

import numpy as np
import pandas as pd

from helpers.helper import infer_data_types, read_tabular_file_from_bytes
from helpers.validators import validate_csv_readable_from_bytes
from tools.data_tools import DataCleaningTools


def test_infer_keeps_columns_with_unparseable_values_as_text():
    df = pd.DataFrame({"qty": ["1", "2", "UNKNOWN", "4"]})
    out = infer_data_types(df)
    # No value may be coerced away — 'UNKNOWN' survives, so the column stays text.
    assert out["qty"].tolist() == ["1", "2", "UNKNOWN", "4"]


def test_infer_converts_fully_numeric_and_datetime_columns():
    df = pd.DataFrame(
        {
            "n": ["1", "2", "3"],
            "d": ["2024-01-01", "2024-02-01", "2024-03-01"],
            "s": ["x", "y", "z"],
        }
    )
    out = infer_data_types(df)
    assert pd.api.types.is_numeric_dtype(out["n"])
    assert pd.api.types.is_datetime64_any_dtype(out["d"])
    assert out["s"].tolist() == ["x", "y", "z"]


def test_infer_ignores_true_nulls_when_checking_losslessness():
    df = pd.DataFrame({"n": ["1", None, "3"]})
    out = infer_data_types(df)
    assert pd.api.types.is_numeric_dtype(out["n"])


def test_handle_missing_preserves_sentinel_strings():
    df = pd.DataFrame({"status": ["UNKNOWN", "ok", "N/A", "ok"]})
    result = DataCleaningTools.handle_missing_values(df, strategy="auto")
    cleaned = result["data"]
    # Sentinel-looking strings are data, not missing values — only true NaN is filled.
    assert cleaned["status"].tolist() == ["UNKNOWN", "ok", "N/A", "ok"]


def test_handle_missing_still_fills_true_nan():
    df = pd.DataFrame({"v": [1.0, np.nan, 3.0]})
    result = DataCleaningTools.handle_missing_values(df, strategy="auto")
    assert result["data"]["v"].isna().sum() == 0


def test_semicolon_csv_is_not_silently_parsed_as_one_column():
    content = b"name;qty\ncoffee;2\ntea;3\n"
    valid, validation_error = validate_csv_readable_from_bytes(content, "sales.csv")
    frame, parse_error = read_tabular_file_from_bytes(content, "sales.csv")
    assert (valid, validation_error) == (True, None)
    assert parse_error is None
    assert frame.columns.tolist() == ["name", "qty"]
    assert frame.to_dict(orient="records") == [
        {"name": "coffee", "qty": 2},
        {"name": "tea", "qty": 3},
    ]


def test_tab_csv_is_not_silently_parsed_as_one_column():
    frame, error = read_tabular_file_from_bytes(b"name\tqty\ncoffee\t2\n", "sales.csv")
    assert error is None
    assert frame.columns.tolist() == ["name", "qty"]
