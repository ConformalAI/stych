"""
Shared row-shaping helpers for the viz engines.

Lives outside ``compile_pandas`` so production code (``viz.executor``) can
shape SQL/DuckDB records without importing the pandas parity oracle.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .plan import PlannedDim

_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def _json_value(v: Any, dim: Optional[PlannedDim]) -> Any:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime(_ISO_FMT)
    # Quantitative dimension values are floats in both engines (SQL emits float8).
    if (
        dim is not None
        and (dim.field_type == "quantitative" or dim.kind == "bin")
        and isinstance(v, (int, float, np.integer, np.floating))
        and not isinstance(v, bool)
    ):
        return round(float(v), 6)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return round(float(v), 6)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (int, bool)):
        return v
    return str(v)
