"""
Pandas-engine executor for FilterSpec annotate steps.

Builtins run in-process; agent-written pandas steps run in the workspace
sandbox (``WorkspaceSandboxRunner``) against ``filter_input.parquet`` (the
frame plus a ``__row_key__`` column, written here) and must produce
``filter_annotations/<step_id>.parquet``. Results merge into one frame indexed
by row key, cached in a tiny LRU keyed on (source identity, data version,
annotate fingerprint) — same shape as ``diff_service._diff_cache``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from filters import builtins as fb
from filters.spec import (
    ROW_KEY_COLUMN,
    DuplicateValuesStep,
    FilterSpec,
    FuzzyDuplicatesStep,
    PandasStep,
    SqlStep,
)
from workspace.runner import head_tail_preview

INPUT_PARQUET = "filter_input.parquet"
ANNOTATIONS_DIR = "filter_annotations"

_CACHE_MAX_ENTRIES = 2
_annotation_cache: "OrderedDict[Tuple, AnnotationResult]" = OrderedDict()


@dataclass
class AnnotationResult:
    """Merged annotation columns (indexed by row key) + per-step statuses."""

    frame: pd.DataFrame
    statuses: List[Dict[str, Any]] = field(default_factory=list)
    degraded: Optional[str] = None
    approx: bool = False


def _status(step_id: str, status: str, **extra: Any) -> Dict[str, Any]:
    return {
        "step_id": step_id,
        "status": status,
        "rows": extra.pop("rows", 0),
        "coverage": extra.pop("coverage", 0.0),
        "error": extra.pop("error", None),
        "cached": False,
        **extra,
    }


def _map_builtin_outputs(step, produced: pd.DataFrame) -> pd.DataFrame:
    """Rename canonical builtin columns to the step's declared outputs.

    Role-tagged outputs map by role; the rest map positionally over the
    remaining canonical columns.
    """
    role_to_canonical = {
        "group": fb.GROUP_COL,
        "score": fb.SCORE_COL,
        "ref": fb.MATCH_KEY_COL,
        "flag": fb.COUNT_COL,
    }
    mapping: Dict[str, str] = {}
    unclaimed = [c for c in produced.columns]
    for out in step.outputs:
        canonical = role_to_canonical.get(out.role or "")
        if canonical in unclaimed:
            mapping[canonical] = out.name
            unclaimed.remove(canonical)
    for out in step.outputs:
        if out.name not in mapping.values() and unclaimed:
            mapping[unclaimed.pop(0)] = out.name
    return produced[list(mapping)].rename(columns=mapping)


def _run_pandas_step(
    step: PandasStep, workspace: Path, runner, n_rows: int
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    result = runner.run(workspace, step.code)
    if not result.get("success"):
        detail = (result.get("error") or result.get("stderr") or "sandbox failure").strip()
        return None, _status(step.id, "error", error=head_tail_preview(detail))

    out_path = workspace / ANNOTATIONS_DIR / f"{step.id}.parquet"
    if not out_path.is_file():
        return None, _status(
            step.id, "error", error=f"code did not write {ANNOTATIONS_DIR}/{step.id}.parquet"
        )
    try:
        produced = pd.read_parquet(out_path)
    except Exception as exc:  # noqa: BLE001 — surface any parquet corruption to the agent
        return None, _status(step.id, "error", error=f"unreadable output parquet: {exc}")
    out_path.unlink(missing_ok=True)  # merged into `produced`; scratch file no longer needed

    declared = [o.name for o in step.outputs]
    missing = [c for c in [ROW_KEY_COLUMN, *declared] if c not in produced.columns]
    if missing:
        return None, _status(step.id, "error", error=f"output missing columns: {missing}")
    frame = produced.set_index(ROW_KEY_COLUMN)[declared]
    if frame.index.has_duplicates:
        frame = frame[~frame.index.duplicated(keep="first")]
    coverage = round(len(frame) / n_rows, 4) if n_rows else 0.0
    return frame, _status(step.id, "ok", rows=len(frame), coverage=coverage)


def run_annotate_steps(
    df: pd.DataFrame,
    spec: FilterSpec,
    *,
    row_keys: pd.Series,
    workspace: Optional[Path] = None,
    runner=None,
) -> AnnotationResult:
    """Execute every annotate step against ``df``; never raises per-step —
    failures land in ``statuses`` so the page can still render."""
    n_rows = len(df)
    merged = pd.DataFrame(index=pd.Index(row_keys))
    statuses: List[Dict[str, Any]] = []
    degraded: Optional[str] = None

    pandas_steps = [s for s in spec.annotate if isinstance(s, PandasStep)]
    if pandas_steps and workspace is not None:
        (workspace / ANNOTATIONS_DIR).mkdir(exist_ok=True)
        input_frame = df.copy()
        input_frame[ROW_KEY_COLUMN] = row_keys.to_numpy()
        input_frame.to_parquet(workspace / INPUT_PARQUET, index=False)

    for step in spec.annotate:
        try:
            if isinstance(step, FuzzyDuplicatesStep):
                if step.on not in df.columns:
                    statuses.append(
                        _status(step.id, "error", error=f"column {step.on!r} not found")
                    )
                    continue
                values = pd.Series(df[step.on].to_numpy(), index=pd.Index(row_keys))
                produced, capped = fb.fuzzy_duplicate_groups(
                    values,
                    strategy=step.strategy,
                    threshold=step.threshold,
                    max_rows=step.max_rows,
                )
                if capped:
                    degraded = "fuzzy_row_cap"
                frame = _map_builtin_outputs(step, produced)
            elif isinstance(step, DuplicateValuesStep):
                produced = fb.duplicate_values_pandas(df, row_keys, step.columns, step.min_count)
                frame = _map_builtin_outputs(step, produced)
            elif isinstance(step, PandasStep):
                if workspace is None or runner is None:
                    statuses.append(
                        _status(step.id, "error", error="no workspace available for pandas steps")
                    )
                    continue
                frame, status = _run_pandas_step(step, workspace, runner, n_rows)
                statuses.append(status)
                if frame is None:
                    continue
                merged = merged.join(frame, how="left")
                continue
            elif isinstance(step, SqlStep):
                statuses.append(
                    _status(step.id, "skipped", error="sql steps require a database source")
                )
                continue
            else:  # pragma: no cover — spec union is closed
                statuses.append(_status(step.id, "error", error="unknown step kind"))
                continue
        except KeyError as exc:
            statuses.append(_status(step.id, "error", error=f"column {exc.args[0]!r} not found"))
            continue

        annotated = int(frame.notna().any(axis=1).sum())
        coverage = round(annotated / n_rows, 4) if n_rows else 0.0
        statuses.append(_status(step.id, "ok", rows=annotated, coverage=coverage))
        merged = merged.join(frame, how="left")

    # Guarantee every declared output exists so select/present never KeyError
    # on a failed step — its columns are simply all-null.
    for name in spec.annotation_outputs():
        if name not in merged.columns:
            merged[name] = np.nan

    return AnnotationResult(
        frame=merged,
        statuses=statuses,
        degraded=degraded,
        approx=degraded is not None,
    )


def get_cached_annotations(
    cache_key: Optional[Tuple],
    df: pd.DataFrame,
    spec: FilterSpec,
    *,
    row_keys: pd.Series,
    workspace: Optional[Path] = None,
    runner=None,
) -> AnnotationResult:
    """LRU wrapper; only file-backed sources pass a cache key (mtime-based),
    mirroring ``diff_service.get_cached_diff``."""
    if cache_key is not None:
        hit = _annotation_cache.get(cache_key)
        if hit is not None:
            _annotation_cache.move_to_end(cache_key)
            return replace(hit, statuses=[{**s, "cached": True} for s in hit.statuses])
    result = run_annotate_steps(df, spec, row_keys=row_keys, workspace=workspace, runner=runner)
    if cache_key is not None:
        _annotation_cache[cache_key] = result
        while len(_annotation_cache) > _CACHE_MAX_ENTRIES:
            _annotation_cache.popitem(last=False)
    return result


def clear_cache() -> None:
    """Test helper."""
    _annotation_cache.clear()
