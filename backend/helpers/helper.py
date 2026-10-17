"""
Helper utilities for file handling, CSV processing, and data validation.
"""

import csv
import hashlib
import io
import logging
import os
import uuid
from datetime import datetime
from math import isinf, isnan
from typing import Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def generate_file_id() -> str:
    """Generate unique file ID."""
    return str(uuid.uuid4())


def get_file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_file_size_mb(file_path: str) -> float:
    """Get file size in MB."""
    size_bytes = os.path.getsize(file_path)
    return size_bytes / (1024 * 1024)


def read_excel_file(file_path: str, sheet_name: int = 0) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Read Excel file (.xlsx / .xls) into a DataFrame.
    Defaults to the first sheet.

    Returns:
        Tuple of (DataFrame, error_message)
    """
    try:
        df = pd.read_excel(file_path, sheet_name=sheet_name, engine="openpyxl")
        logger.info(f"Successfully read Excel file (sheet {sheet_name}): {file_path}")
        return df, None
    except Exception as e:
        logger.error(f"Error reading Excel file: {e}")
        return None, str(e)


def read_json_file(file_path: str) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Read JSON file into a DataFrame.
    Handles both JSON arrays and records-oriented objects.

    Returns:
        Tuple of (DataFrame, error_message)
    """
    try:
        df = pd.read_json(file_path, orient="records")
        logger.info(f"Successfully read JSON file: {file_path}")
        return df, None
    except ValueError:
        # Try other common orientations
        try:
            df = pd.read_json(file_path)
            return df, None
        except Exception as e:
            logger.error(f"Error reading JSON file: {e}")
            return None, str(e)
    except Exception as e:
        logger.error(f"Error reading JSON file: {e}")
        return None, str(e)


def read_tabular_file_from_bytes(
    content: bytes, filename: str
) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Parse a tabular file from raw bytes without writing to disk.

    Dispatches by *filename* extension (.csv, .xlsx, .xls, .json).
    After reading, column dtypes are inferred (losslessly) — identical
    post-processing as read_tabular_file().

    Returns:
        Tuple of (DataFrame or None, error_message or None)
    """
    import io as _io

    ext = os.path.splitext(filename)[1].lower() if filename else ""
    buf = _io.BytesIO(content)

    try:
        if ext == ".xlsx":
            df = pd.read_excel(buf, engine="openpyxl")
            err = None
        elif ext == ".xls":
            # openpyxl does not support legacy .xls (BIFF) format; use xlrd instead.
            try:
                df = pd.read_excel(buf, engine="xlrd")
            except Exception:
                return None, (
                    "Legacy .xls files are not supported. " "Please convert to .xlsx and re-upload."
                )
            err = None
        elif ext == ".json":
            try:
                df = pd.read_json(buf, orient="records")
                err = None
            except ValueError:
                buf.seek(0)
                df = pd.read_json(buf)
                err = None
        else:
            # CSV — detect the delimiter after decoding. Trying comma first and
            # accepting a one-column result silently corrupts semicolon/tab files.
            encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
            df, err = None, None
            for encoding in encodings:
                try:
                    text = content.decode(encoding, errors="strict")
                    try:
                        sep = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
                    except csv.Error:
                        sep = ","
                    df = pd.read_csv(
                        _io.StringIO(text),
                        sep=sep,
                        on_bad_lines="warn",
                        engine="python",
                    )
                    break
                except Exception:
                    df = None
            if df is None:
                # UTF-8 with replacement as last resort
                text = content.decode("utf-8", errors="replace")
                try:
                    sep = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
                except csv.Error:
                    sep = ","
                try:
                    df = pd.read_csv(
                        _io.StringIO(text),
                        sep=sep,
                        on_bad_lines="warn",
                        engine="python",
                    )
                    err = "Warning: Some characters couldn't be decoded properly."
                except Exception:
                    df = None
            if df is None:
                return None, (
                    "Could not parse CSV with any supported encoding or delimiter. "
                    "Use comma or semicolon delimiter and ensure no extra commas inside unquoted fields."
                )
    except Exception as exc:
        logger.error("Error reading %s from bytes: %s", filename, exc)
        return None, str(exc)

    df = infer_data_types(df)
    return df, err


def read_tabular_file(file_path: str) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Dispatch to the correct reader based on file extension.
    Supports .csv, .xlsx, .xls, and .json.

    After reading, column dtypes are inferred (losslessly — values are never
    coerced away) so downstream agents receive a typed DataFrame.

    Returns:
        Tuple of (DataFrame, error_message)
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df, err = read_excel_file(file_path)
    elif ext == ".json":
        df, err = read_json_file(file_path)
    else:
        df, err = read_csv_file(file_path)

    if df is not None:
        df = infer_data_types(df)

    return df, err


def _read_csv_with_delimiter(
    file_path: str, encoding: str, sep: str, **kwargs
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Try to read CSV with given encoding and delimiter. Returns (df, error)."""
    try:
        df = pd.read_csv(
            file_path,
            encoding=encoding,
            sep=sep,
            on_bad_lines="warn",
            engine="python",
            **kwargs,
        )
        return df, None
    except Exception as e:
        return None, str(e)


def read_csv_file(file_path: str, **kwargs) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Read CSV file with error handling.

    Tries multiple encodings and delimiters (comma, then semicolon) so that
    European-style CSVs (;) and fields containing commas are handled.

    Args:
        file_path: Path to CSV file
        **kwargs: Additional arguments for pd.read_csv

    Returns:
        Tuple of (DataFrame, error_message)
    """
    encodings = ["utf-8", "latin-1", "iso-8859-1", "cp1252"]
    delimiters = [",", ";", "\t"]

    for encoding in encodings:
        for sep in delimiters:
            df, err = _read_csv_with_delimiter(file_path, encoding, sep, **kwargs)
            if df is not None and len(df.columns) > 1:
                logger.info(
                    "Successfully read CSV with encoding=%s sep=%r columns=%s",
                    encoding,
                    sep,
                    len(df.columns),
                )
                return df, None
            if df is not None and len(df.columns) == 1 and sep != ",":
                # Single column with non-comma delimiter might be wrong; try next
                continue

    # If all encoding+delimiter combos fail, try utf-8 with replacement decoding
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            contents = f.read()
        for sep in delimiters:
            try:
                df = pd.read_csv(
                    io.StringIO(contents),
                    sep=sep,
                    on_bad_lines="warn",
                    engine="python",
                    **kwargs,
                )
                if df is not None and len(df.columns) > 1:
                    logger.info("Read CSV with encoding=utf-8 (replace) sep=%r", sep)
                    return df, "Warning: Some characters couldn't be decoded properly."
            except Exception:
                continue
    except Exception:
        pass

    # Final attempt: first line only to get a clear error
    try:
        for encoding in encodings:
            with open(file_path, "r", encoding=encoding) as f:
                sample = f.read(65536)
            for sep in delimiters:
                try:
                    df = pd.read_csv(
                        io.StringIO(sample),
                        encoding=encoding,
                        sep=sep,
                        on_bad_lines="warn",
                        engine="python",
                        nrows=1000,
                        **kwargs,
                    )
                    if len(df.columns) > 1:
                        df_full, _ = _read_csv_with_delimiter(file_path, encoding, sep, **kwargs)
                        if df_full is not None:
                            return df_full, None
                except Exception:
                    continue
    except Exception:
        pass

    return None, (
        "Could not parse CSV with any supported encoding or delimiter. "
        "Use comma or semicolon delimiter and ensure no extra commas inside unquoted fields."
    )


def infer_data_types(df: pd.DataFrame) -> pd.DataFrame:
    """
    Coerce object columns to numeric or datetime only when EVERY non-null
    value converts. Lossless by design: a column with even one stray string
    stays object rather than silently coercing values to NaN — cleaning that
    up is an explicit, user-visible cleaning operation.
    """
    try:
        df_optimized = df.copy()

        for col in df_optimized.columns:
            if pd.api.types.is_datetime64_any_dtype(df_optimized[col]):
                continue

            # pandas 3 reads text columns as StringDtype, not object — cover both.
            dtype = df_optimized[col].dtype
            if not (pd.api.types.is_object_dtype(dtype) or isinstance(dtype, pd.StringDtype)):
                continue

            series = df_optimized[col]
            non_null = series.dropna()
            if non_null.empty:
                continue

            # --- try numeric first (fastest) ---
            numeric = pd.to_numeric(series, errors="coerce")
            if numeric.notna().sum() == len(non_null):
                df_optimized[col] = numeric
                logger.info("Converted %s → numeric", col)
                continue

            # --- try datetime ---
            try:
                dt_series = pd.to_datetime(series, errors="coerce")
                if dt_series.notna().sum() == len(non_null):
                    df_optimized[col] = dt_series
                    logger.info("Converted %s → datetime", col)
                    continue
            except Exception:
                pass

        return df_optimized

    except Exception as e:
        logger.error("Error inferring data types: %s", e)
        return df


def to_json_safe(value):
    """
    Convert pandas/numpy values into JSON-safe Python primitives.
    """
    if value is None:
        return None

    # pandas missing sentinels (NaT/NA)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, (pd.Timestamp, datetime)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    if isinstance(value, np.generic):
        value = value.item()

    if isinstance(value, float):
        if isnan(value) or isinf(value):
            return None
        return value

    if isinstance(value, (list, tuple)):
        return [to_json_safe(v) for v in value]

    if isinstance(value, dict):
        return {str(k): to_json_safe(v) for k, v in value.items()}

    return value


def get_data_summary(df: pd.DataFrame) -> dict:
    """
    Get a quick summary of the DataFrame.

    Args:
        df: Input DataFrame

    Returns:
        Summary dictionary
    """
    try:
        sample_rows = df.head(5).to_dict("records") if len(df) > 0 else []
        sample_rows = [to_json_safe(row) for row in sample_rows]

        return {
            "rows": len(df),
            "columns": len(df.columns),
            "column_names": list(df.columns),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
            "missing_values": {
                str(col): int(cnt) for col, cnt in df.isnull().sum().to_dict().items()
            },
            "memory_usage_mb": df.memory_usage(deep=True).sum() / (1024 * 1024),
            "sample_rows": sample_rows,
        }
    except Exception as e:
        logger.error(f"Error generating data summary: {e}")
        return {"error": str(e)}


def format_error_response(error: Exception) -> dict:
    """Format error as JSON response."""
    return {
        "error": str(error),
        "type": type(error).__name__,
        "timestamp": datetime.now().isoformat(),
    }


def format_success_response(data: dict, message: str = "Success") -> dict:
    """Format success response."""
    return {
        "success": True,
        "message": message,
        "data": data,
        "timestamp": datetime.now().isoformat(),
    }


def clean_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean column names (remove special characters, spaces, etc.).

    Args:
        df: Input DataFrame

    Returns:
        DataFrame with cleaned column names
    """
    df_cleaned = df.copy()

    # Replace spaces with underscores, remove special characters
    df_cleaned.columns = (
        df_cleaned.columns.str.strip()
        .str.lower()
        .str.replace(" ", "_")
        .str.replace("[^a-zA-Z0-9_]", "", regex=True)
    )

    # Ensure unique column names
    cols = pd.Series(df_cleaned.columns)
    for dup in cols[cols.duplicated()].unique():
        cols[cols[cols == dup].index.values.tolist()] = [
            f"{dup}_{i}" if i != 0 else dup for i in range(sum(cols == dup))
        ]
    df_cleaned.columns = cols

    return df_cleaned
