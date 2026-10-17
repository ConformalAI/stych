"""
CSV Agent — file-based data (CSV, XLSX) cleaning and analysis.

Handles in-memory DataFrame operations: cleaning via pandas,
analysis via sandbox code generation and execution.
Used when session data comes from upload (CSV/XLSX), not from a database.
"""

from .agent import CSVAgent

__all__ = ["CSVAgent"]
