"""
Data validation utilities for CSV uploads and API inputs.
"""

import csv
import io
import logging
import os
from typing import List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Custom validation error."""

    pass


def validate_file_extension(filename: str, allowed_extensions: List[str]) -> bool:
    """
    Check if file has allowed extension.

    Args:
        filename: Filename to check
        allowed_extensions: List of allowed extensions (e.g., ['.csv', '.xlsx'])

    Returns:
        True if valid

    Raises:
        ValidationError if invalid
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_extensions:
        raise ValidationError(
            f"Invalid file extension '{ext}'. Allowed: {', '.join(allowed_extensions)}"
        )
    return True


def validate_file_size(file_size_bytes: int, max_size_mb: float) -> bool:
    """
    Check if file size is within limit.

    Args:
        file_size_bytes: File size in bytes
        max_size_mb: Maximum allowed size in MB

    Returns:
        True if valid

    Raises:
        ValidationError if too large
    """
    max_size_bytes = max_size_mb * 1024 * 1024
    if file_size_bytes > max_size_bytes:
        actual_mb = file_size_bytes / (1024 * 1024)
        raise ValidationError(
            f"File too large ({actual_mb:.2f}MB). Maximum allowed: {max_size_mb}MB"
        )
    return True


def validate_csv_readable(file_path: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a tabular file can be read. Dispatches by extension.
    Supports .csv, .xlsx, .xls, and .json.

    Args:
        file_path: Path to file

    Returns:
        Tuple of (is_valid, error_message)
    """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            pd.read_excel(file_path, nrows=5, engine="openpyxl")
        elif ext == ".json":
            df = pd.read_json(file_path)
            if df.empty:
                return False, "JSON file produced an empty DataFrame"
        else:
            with open(file_path, "rb") as handle:
                return validate_csv_readable_from_bytes(handle.read(), filename=file_path)
        return True, None
    except Exception as e:
        return False, f"Cannot read file: {str(e)}"


def validate_csv_readable_from_bytes(
    content: bytes, filename: str = ""
) -> Tuple[bool, Optional[str]]:
    """
    Validate that raw file bytes can be parsed as a tabular file.

    Works for S3 and local backends alike — no local file path needed.
    Dispatches by *filename* extension (.csv, .xlsx, .xls, .json).

    Returns:
        Tuple of (is_valid, error_message)
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    try:
        if ext in (".xlsx", ".xls"):
            pd.read_excel(io.BytesIO(content), nrows=5, engine="openpyxl")
        elif ext == ".json":
            df = pd.read_json(io.BytesIO(content))
            if df.empty:
                return False, "JSON file produced an empty DataFrame"
        else:
            for encoding in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = content.decode(encoding, errors="strict")
                    try:
                        sep = csv.Sniffer().sniff(text[:65536], delimiters=",;\t").delimiter
                    except csv.Error:
                        sep = ","
                    pd.read_csv(
                        io.StringIO(text),
                        nrows=5,
                        sep=sep,
                        on_bad_lines="warn",
                        engine="python",
                    )
                    break
                except (UnicodeDecodeError, pd.errors.ParserError):
                    continue
            else:
                return False, "Cannot decode or parse CSV with a supported encoding"
        return True, None
    except Exception as e:
        return False, f"Cannot read file: {str(e)}"


def validate_dataframe(df: pd.DataFrame, min_rows: int = 1, min_cols: int = 1) -> bool:
    """
    Validate DataFrame meets minimum requirements.

    Args:
        df: DataFrame to validate
        min_rows: Minimum number of rows required
        min_cols: Minimum number of columns required

    Returns:
        True if valid

    Raises:
        ValidationError if invalid
    """
    if df.empty:
        raise ValidationError("DataFrame is empty")

    if len(df) < min_rows:
        raise ValidationError(f"DataFrame has {len(df)} rows, minimum required: {min_rows}")

    if len(df.columns) < min_cols:
        raise ValidationError(
            f"DataFrame has {len(df.columns)} columns, minimum required: {min_cols}"
        )

    return True


def validate_column_exists(df: pd.DataFrame, column: str) -> bool:
    """
    Check if column exists in DataFrame.

    Args:
        df: DataFrame to check
        column: Column name

    Returns:
        True if exists

    Raises:
        ValidationError if column doesn't exist
    """
    if column not in df.columns:
        raise ValidationError(
            f"Column '{column}' not found. Available columns: {', '.join(df.columns)}"
        )
    return True


def validate_columns_exist(df: pd.DataFrame, columns: List[str]) -> bool:
    """
    Check if multiple columns exist in DataFrame.

    Args:
        df: DataFrame to check
        columns: List of column names

    Returns:
        True if all exist

    Raises:
        ValidationError if any column doesn't exist
    """
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise ValidationError(
            f"Columns not found: {', '.join(missing)}. Available: {', '.join(df.columns)}"
        )
    return True


def validate_numeric_column(df: pd.DataFrame, column: str) -> bool:
    """
    Check if column contains numeric data.

    Args:
        df: DataFrame to check
        column: Column name

    Returns:
        True if numeric

    Raises:
        ValidationError if not numeric
    """
    validate_column_exists(df, column)

    if not pd.api.types.is_numeric_dtype(df[column]):
        raise ValidationError(f"Column '{column}' is not numeric (dtype: {df[column].dtype})")

    return True


def sanitize_input(text: str, max_length: int = 10000) -> str:
    """
    Sanitize user input text.

    Args:
        text: Input text
        max_length: Maximum allowed length

    Returns:
        Sanitized text

    Raises:
        ValidationError if input is invalid
    """
    if not text or not text.strip():
        raise ValidationError("Input text cannot be empty")

    text = text.strip()

    if len(text) > max_length:
        raise ValidationError(f"Input too long ({len(text)} chars). Maximum: {max_length}")

    return text
