"""
Field catalog — the thin semantic layer specs reference.

One typed field list per dataset: names, viz types (quantitative / nominal /
temporal), dimension-vs-measure roles, default aggregates, cardinality, null
percentages, and sample values. The UI's field list and the agent's
``list_fields`` tool both read it; ``viz.validate`` checks specs against it.

Cached in ``DataContext.metadata["field_catalog"]`` (rides the existing
dirty-session snapshot) with a content fingerprint — shape, dtypes, and
per-column null counts — so cleaning mutations invalidate it naturally.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CATALOG_META_KEY = "field_catalog"
CATALOG_CACHE_TOKEN_KEY = "field_catalog_cache_token"

_DATE_SAMPLE_SIZE = 50
_DATE_PARSE_RATIO = 0.80
_NUMERIC_PARSE_RATIO = 0.80
_ID_UNIQUE_RATIO = 0.95
_ID_MIN_ROWS = 50
_MAX_SAMPLES = 5
_CATALOG_CLASSIFIER_VERSION = "semantic-v3"

# Postgres type buckets (lowercased data_type from information_schema).
PG_TEMPORAL_TYPES = frozenset(
    {
        "date",
        "time",
        "timetz",
        "timestamp",
        "timestamptz",
        "timestamp without time zone",
        "timestamp with time zone",
    }
)
PG_NUMERIC_TYPES = frozenset(
    {
        "integer",
        "bigint",
        "smallint",
        "numeric",
        "decimal",
        "real",
        "double precision",
        "float4",
        "float8",
        "money",
        "serial",
        "bigserial",
    }
)


class FieldInfo(BaseModel):
    name: str
    type: str  # quantitative | nominal | temporal (ordinal is user-assigned)
    role: str  # dimension | measure
    default_aggregate: Optional[str] = None
    cardinality: Optional[int] = None
    null_pct: Optional[float] = None
    samples: List[Any] = Field(default_factory=list)
    is_id: bool = False
    db_type: Optional[str] = None  # authoritative Postgres type on SQL sessions
    format_hint: Optional[str] = None
    is_calculated: bool = False  # spec-level derived column (viz.expr), not in the dataset


class FieldCatalog(BaseModel):
    fingerprint: str
    built_at: str
    engine: str  # pandas | sql | mongodb | duckdb
    row_count: int
    fields: List[FieldInfo]
    # Stable DataContext.file_id when the catalog was built through a session.
    # Standalone compiler/tests may leave it unset.
    source: Optional[str] = None
    scope: Optional[str] = None
    freshness: Optional[str] = None
    row_limit: Optional[int] = None
    truncated: bool = False
    schema_complete: bool = True

    def get(self, name: str) -> Optional[FieldInfo]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def field_names(self) -> List[str]:
        return [f.name for f in self.fields]


# ---------------------------------------------------------------------------
# Inference helpers (pandas path)
# ---------------------------------------------------------------------------


def _is_text_dtype(dtype: Any) -> bool:
    return pd.api.types.is_object_dtype(dtype) or isinstance(dtype, pd.StringDtype)


def _looks_temporal(series: pd.Series) -> bool:
    """Text column whose values overwhelmingly parse as dates."""
    sample = series.dropna().iloc[:_DATE_SAMPLE_SIZE]
    if sample.empty:
        return False
    try:
        parsed = pd.to_datetime(sample.astype(str), errors="coerce", format="mixed")
    except Exception:  # noqa: BLE001
        return False
    return float(parsed.notna().mean()) >= _DATE_PARSE_RATIO


def _looks_numeric(series: pd.Series) -> bool:
    """Text column whose non-null values are predominantly numeric."""
    sample = series.dropna()
    if sample.empty:
        return False
    parsed = pd.to_numeric(sample, errors="coerce")
    return float(parsed.notna().mean()) >= _NUMERIC_PARSE_RATIO


def _looks_id(series: pd.Series) -> bool:
    n = len(series)
    if n < _ID_MIN_ROWS:
        return False
    return series.nunique() / n > _ID_UNIQUE_RATIO


def _json_samples(series: pd.Series, limit: int = _MAX_SAMPLES) -> List[Any]:
    out: List[Any] = []
    for v in series.dropna().unique()[: limit * 2]:
        if isinstance(v, (int, float, bool)):
            out.append(v)
        else:
            out.append(str(v))
        if len(out) >= limit:
            break
    return out


def _infer_field_pandas(name: str, series: pd.Series) -> FieldInfo:
    null_pct = round(float(series.isnull().mean()) * 100, 1)
    cardinality = int(series.nunique())
    is_id = _looks_id(series)

    if pd.api.types.is_datetime64_any_dtype(series):
        ftype, role, agg = "temporal", "dimension", None
    elif pd.api.types.is_bool_dtype(series):
        ftype, role, agg = "nominal", "dimension", None
    elif pd.api.types.is_numeric_dtype(series):
        if is_id:
            ftype, role, agg = "quantitative", "dimension", None
        else:
            ftype, role, agg = "quantitative", "measure", "sum"
    elif _is_text_dtype(series.dtype):
        if _looks_numeric(series):
            if is_id:
                ftype, role, agg = "quantitative", "dimension", None
            else:
                ftype, role, agg = "quantitative", "measure", "sum"
        elif _looks_temporal(series):
            ftype, role, agg = "temporal", "dimension", None
        else:
            ftype, role, agg = "nominal", "dimension", None
    else:
        ftype, role, agg = "nominal", "dimension", None

    return FieldInfo(
        name=name,
        type=ftype,
        role=role,
        default_aggregate=agg,
        cardinality=cardinality,
        null_pct=null_pct,
        samples=_json_samples(series),
        is_id=is_id,
    )


def fingerprint_dataframe(df: pd.DataFrame) -> str:
    """Shape + dtypes + per-column null counts — cheap, catches imputations."""
    parts = [_CATALOG_CLASSIFIER_VERSION, str(len(df))]
    for col in df.columns:
        parts.append(f"{col}:{df[col].dtype}:{int(df[col].isnull().sum())}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def build_catalog_pandas(df: pd.DataFrame) -> FieldCatalog:
    return FieldCatalog(
        fingerprint=fingerprint_dataframe(df),
        built_at=datetime.now(timezone.utc).isoformat(),
        engine="pandas",
        row_count=len(df),
        fields=[_infer_field_pandas(str(c), df[c]) for c in df.columns],
    )


def build_catalog_duckdb(plane: Any, preview_df: pd.DataFrame) -> FieldCatalog:
    """Build the semantic catalog from a DuckDB relation and its loaded snapshot."""
    fields: List[FieldInfo] = []
    for column in plane.relation.columns:
        if column.name in preview_df.columns:
            info = _infer_field_pandas(column.name, preview_df[column.name])
        else:
            db_type = column.data_type.lower()
            if any(part in db_type for part in ("int", "float", "double", "decimal")):
                ftype, role, agg = "quantitative", "measure", "sum"
            elif any(part in db_type for part in ("date", "time", "timestamp")):
                ftype, role, agg = "temporal", "dimension", None
            else:
                ftype, role, agg = "nominal", "dimension", None
            info = FieldInfo(
                name=column.name,
                type=ftype,
                role=role,
                default_aggregate=agg,
            )
        info.db_type = column.data_type.lower()
        fields.append(info)
    fingerprint = hashlib.sha1(
        (plane.snapshot_token + "|" + "|".join(f"{f.name}:{f.db_type}" for f in fields)).encode()
    ).hexdigest()
    return FieldCatalog(
        fingerprint=fingerprint,
        built_at=datetime.now(timezone.utc).isoformat(),
        engine="duckdb",
        row_count=plane.row_count,
        fields=fields,
        source=plane.source_id,
        scope=plane.scope.value,
        freshness=plane.freshness.value,
        row_limit=plane.row_limit,
        truncated=plane.truncated,
        schema_complete=plane.schema_complete,
    )


# ---------------------------------------------------------------------------
# SQL path — authoritative types from Postgres, stats enriched from the
# in-memory preview frame when one exists.
# ---------------------------------------------------------------------------


def _fingerprint_sql(schema: str, table: str, columns: List[dict], row_count: int) -> str:
    parts = [_CATALOG_CLASSIFIER_VERSION, f"{schema}.{table}", str(row_count)]
    for c in columns:
        parts.append(f"{c.get('column_name')}:{c.get('data_type')}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def build_catalog_sql(
    schema: str,
    table: str,
    columns: List[dict],
    row_count: int,
    preview_df: Optional[pd.DataFrame] = None,
) -> FieldCatalog:
    fields: List[FieldInfo] = []
    for c in columns:
        name = str(c.get("column_name") or "")
        db_type = str(c.get("data_type") or "").lower()
        series = preview_df[name] if preview_df is not None and name in preview_df.columns else None
        if db_type in PG_TEMPORAL_TYPES:
            ftype, role, agg = "temporal", "dimension", None
        elif db_type in PG_NUMERIC_TYPES:
            ftype, role, agg = "quantitative", "measure", "sum"
        elif db_type == "boolean":
            ftype, role, agg = "nominal", "dimension", None
        elif series is not None and _looks_numeric(series):
            ftype, role, agg = "quantitative", "measure", "sum"
        elif series is not None and _looks_temporal(series):
            ftype, role, agg = "temporal", "dimension", None
        else:
            ftype, role, agg = "nominal", "dimension", None

        info = FieldInfo(name=name, type=ftype, role=role, default_aggregate=agg, db_type=db_type)
        if series is not None:
            info.cardinality = int(series.nunique())
            info.null_pct = round(float(series.isnull().mean()) * 100, 1)
            info.samples = _json_samples(series)
        fields.append(info)

    return FieldCatalog(
        fingerprint=_fingerprint_sql(schema, table, columns, row_count),
        built_at=datetime.now(timezone.utc).isoformat(),
        engine="sql",
        row_count=row_count,
        fields=fields,
    )


def build_catalog_mongo(
    database: str,
    collection: str,
    columns: List[dict],
    row_count: int,
    preview_df: Optional[pd.DataFrame] = None,
) -> FieldCatalog:
    """Build MongoDB's semantic catalog from its sampled document shape.

    MongoDB has no declared schema.  ``columns`` are therefore fields seen in
    a bounded sample, while ``row_count`` is the exact live collection count.
    Stats and semantic inference are explicitly sample-derived and the
    catalog is marked incomplete so clients do not present it as a full schema.
    """
    fields: List[FieldInfo] = []
    for c in columns:
        name = str(c.get("column_name") or "")
        series = preview_df[name] if preview_df is not None and name in preview_df.columns else None
        if series is not None:
            info = _infer_field_pandas(name, series)
            info.db_type = str(c.get("data_type") or series.dtype).lower()
        else:
            db_type = str(c.get("data_type") or "object").lower()
            if any(part in db_type for part in ("int", "float", "double", "decimal")):
                ftype, role, agg = "quantitative", "measure", "sum"
            elif any(part in db_type for part in ("date", "time", "datetime")):
                ftype, role, agg = "temporal", "dimension", None
            else:
                ftype, role, agg = "nominal", "dimension", None
            info = FieldInfo(
                name=name,
                type=ftype,
                role=role,
                default_aggregate=agg,
                db_type=db_type,
            )
        fields.append(info)

    fingerprint = _fingerprint_mongo(database, collection, columns, row_count)
    return FieldCatalog(
        fingerprint=fingerprint,
        built_at=datetime.now(timezone.utc).isoformat(),
        engine="mongodb",
        row_count=row_count,
        fields=fields,
        # The collection itself is live and fully queryable; only its schema
        # and per-field sample stats are bounded/inferred.
        scope="full",
        freshness="live",
        truncated=False,
        schema_complete=False,
    )


def _fingerprint_mongo(database: str, collection: str, columns: List[dict], row_count: int) -> str:
    parts = [_CATALOG_CLASSIFIER_VERSION, "mongodb", f"{database}.{collection}", str(row_count)]
    parts.extend(f"{c.get('column_name')}:{c.get('data_type')}" for c in columns)
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Cached accessor
# ---------------------------------------------------------------------------


async def get_field_catalog(
    state: Any,
    sql_agent: Any = None,
    force: bool = False,
    data_context: Any = None,
) -> FieldCatalog:
    """
    Return the dataset's field catalog, rebuilding when the cached fingerprint
    no longer matches the data (or ``force`` is set). ``state`` is an
    ``AgentState`` with a populated ``data_context``.
    """
    dc = data_context or state.data_context
    if dc is None:
        raise ValueError("No dataset loaded — upload a file or connect a database first.")
    del sql_agent

    from execution.session_plane import (  # noqa: PLC0415
        PlaneKind,
        plane_to_pandas,
        resolve_data_plane,
    )

    plane = await asyncio.to_thread(resolve_data_plane, state, dc)
    if plane.kind == PlaneKind.SOURCE:
        columns, row_count, temp_schema, temp_table = await asyncio.to_thread(
            _source_catalog_inputs, dc
        )
        if str((dc.metadata or {}).get("source") or "") == "mongodb":
            current_fp = _fingerprint_mongo(str(temp_schema), str(temp_table), columns, row_count)
        else:
            current_fp = _fingerprint_sql(str(temp_schema), str(temp_table), columns, row_count)
        cached = _load_cached(dc, current_fp, force)
        if cached is not None:
            return cached
        preview = dc.working_dataframe
        preview = preview if preview is not None and not preview.empty else None
        if str((dc.metadata or {}).get("source") or "") == "mongodb":
            catalog = build_catalog_mongo(
                str(temp_schema),
                str(temp_table),
                columns,
                row_count,
                preview_df=preview,
            )
        else:
            catalog = build_catalog_sql(
                str(temp_schema),
                str(temp_table),
                columns,
                row_count,
                preview_df=preview,
            )
    else:
        cache_token = f"{_CATALOG_CLASSIFIER_VERSION}:duckdb:{plane.snapshot_token}"
        if not force and (dc.metadata or {}).get(CATALOG_CACHE_TOKEN_KEY) == cache_token:
            cached = _load_cached_any(dc)
            if cached is not None:
                cached.source = str(dc.file_id)
                return cached
        normalized_columns = list(
            ((dc.metadata or {}).get("analytical_snapshot") or {}).get("normalized_columns") or []
        )
        preview = (
            await asyncio.to_thread(plane_to_pandas, plane)
            if normalized_columns
            else dc.working_dataframe
        )
        catalog = build_catalog_duckdb(plane, preview)

    catalog.source = str(dc.file_id)
    dc.metadata[CATALOG_META_KEY] = catalog.model_dump(mode="json")
    if plane.kind == PlaneKind.DUCKDB:
        dc.metadata[CATALOG_CACHE_TOKEN_KEY] = (
            f"{_CATALOG_CLASSIFIER_VERSION}:duckdb:{plane.snapshot_token}"
        )
    return catalog


def _source_catalog_inputs(dc: Any) -> tuple[list[dict], int, str, str]:
    """Read field names and row count from the live adapter. Does not clone."""
    from contextlib import nullcontext  # noqa: PLC0415

    from datasources.adapters import TempTableUnavailable, resolve_adapter  # noqa: PLC0415

    meta = dc.metadata or {}
    variant = "temp" if meta.get("temp_schema") and meta.get("temp_table") else "base"
    try:
        adapter = resolve_adapter(dc, variant)
    except TempTableUnavailable:
        adapter = resolve_adapter(dc, "base")
    with getattr(adapter, "bound_session", nullcontext)():
        cols = adapter.schema()
        count = adapter.count()
    from agents.sql_agent import working_copies as wc  # noqa: PLC0415 — avoid a cycle

    columns = [
        {"column_name": c.name, "data_type": c.db_type}
        for c in cols
        if not wc.is_internal_column(c.name)
    ]
    schema, table = dc.get_sql_target()
    if meta.get("source") == "mongodb":
        schema = getattr(adapter, "database", None)
        table = getattr(adapter, "collection", None)
    if not schema or not table:
        schema = str(getattr(adapter, "db_schema", None) or meta.get("schema") or "query")
        table = str(getattr(adapter, "table", None) or "custom")
    return columns, int(count.value), str(schema), str(table)


def _load_cached(dc: Any, current_fp: str, force: bool) -> Optional[FieldCatalog]:
    if force:
        return None
    raw = (dc.metadata or {}).get(CATALOG_META_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        cached = FieldCatalog.model_validate(raw)
    except Exception:  # noqa: BLE001 — stale/corrupt cache is rebuilt, never fatal
        return None
    if cached.fingerprint != current_fp:
        return None
    cached.source = str(dc.file_id)
    return cached


def _load_cached_any(dc: Any) -> Optional[FieldCatalog]:
    raw = (dc.metadata or {}).get(CATALOG_META_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return FieldCatalog.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None
