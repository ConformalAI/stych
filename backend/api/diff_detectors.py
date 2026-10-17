"""
Cell/column change detectors for the spreadsheet diff engine: imputation
methods, numeric transforms (standardize, min-max, log, scale/shift, round),
label encoding, consistent remaps, recasts, and string tweaks.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd

# Column-level transforms need most value pairs to agree before we trust them.
TRANSFORM_MATCH_RATIO = 0.9
MIN_TRANSFORM_CELLS = 3
MAX_REMAP_PAIRS = 20

# Strings that mean "missing" regardless of source. pandas' ``read_csv`` maps its
# default NA set to NaN on load, but Postgres/Excel/JSON sources keep them as
# literal strings; both diff engines must treat these identically or the same
# imputation is classified differently across sources (empty-string base cells
# get flagged "changed"/"remapped" instead of "imputed"). All entries are
# already lowercased + stripped — compare with ``value.strip().lower()``.
MISSING_TOKENS = frozenset(
    {
        "",
        "na",
        "n/a",
        "n.a.",
        "nan",
        "null",
        "none",
        "nil",
        "nd",
        "n/d",
        "<na>",
        "#n/a",
        "#n/a n/a",
        "#na",
        "unknown",
        "unk",
        "undefined",
        "missing",
        "not available",
        "not applicable",
        "no data",
        "tbd",
    }
)


def is_missing_value(value) -> bool:
    """Scalar 'missing' test shared by both diff engines: SQL NULL / NaN / pd.NA,
    or a recognized missing-token string (case- and whitespace-insensitive)."""
    if value is None or value is pd.NA:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and value.strip().lower() in MISSING_TOKENS:
        return True
    return False


def normalize_missing(series: pd.Series) -> pd.Series:
    """Replace recognized missing-token strings with NaN so ``.isna()`` agrees
    across sources. Numeric/temporal/bool columns can't hold token strings, so
    they pass through untouched (and stay cheap)."""
    if series.dtype.kind in "biufcMm":
        return series
    return series.map(
        lambda v: np.nan if (isinstance(v, str) and v.strip().lower() in MISSING_TOKENS) else v
    )


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _matches(candidate: pd.Series, tv: pd.Series, mask: pd.Series) -> bool:
    """Do candidate values reproduce the target on masked rows (>=90% agree)?"""
    if int(mask.sum()) < MIN_TRANSFORM_CELLS:
        return False
    c, t = candidate[mask], _numeric(tv)[mask]
    ok = ~c.isna() & ~t.isna()
    if ok.sum() < MIN_TRANSFORM_CELLS:
        return False
    hits = np.isclose(c[ok], t[ok], rtol=1e-05, atol=1e-08)
    return hits.mean() >= TRANSFORM_MATCH_RATIO


def _detect_numeric_transform(bv: pd.Series, tv: pd.Series, modified: pd.Series) -> Optional[str]:
    """Column-wide systematic numeric transforms, checked most-specific first."""
    b, t = _numeric(bv), _numeric(tv)
    pair = ~b.isna() & ~t.isna()

    mean = b[pair].mean()
    for ddof in (0, 1):  # scalers (sklearn) use population std; pandas defaults to sample
        std = b[pair].std(ddof=ddof)
        if std and std > 0 and _matches((b - mean) / std, tv, modified):
            return "standardized"
    lo, hi = b[pair].min(), b[pair].max()
    if hi > lo and _matches((b - lo) / (hi - lo), tv, modified):
        return "min-max scaled"
    if (b[pair] > 0).all() and _matches(np.log(b), tv, modified):
        return "log transform"
    if (b[pair] >= 0).all() and _matches(np.log1p(b), tv, modified):
        return "log1p transform"

    for digits in range(0, 5):
        if _matches(b.round(digits), tv, modified):
            return "rounded"

    return _detect_affine(b, t, modified & pair)


def _detect_affine(b: pd.Series, t: pd.Series, mask: pd.Series) -> Optional[str]:
    """Fit t ≈ a·b + c and name the transform by the result's shape.

    Catches scalers whose fit statistics don't reproduce exactly from the
    surviving rows (e.g. standardized before rows were dropped), plus plain
    unit conversions and constant shifts.
    """
    if int(mask.sum()) < MIN_TRANSFORM_CELLS:
        return None
    bb = b[mask].to_numpy(dtype=float)
    tt = t[mask].to_numpy(dtype=float)
    if np.ptp(bb) == 0:
        return None
    a, c = np.polyfit(bb, tt, 1)
    if not (np.isfinite(a) and np.isfinite(c)) or a == 0:
        return None
    pred = a * bb + c
    if np.isclose(pred, tt, rtol=1e-04, atol=1e-08).mean() < TRANSFORM_MATCH_RATIO:
        return None

    near_identity = np.isclose(a, 1, rtol=1e-06) and np.isclose(
        c, 0, atol=1e-09 * max(np.abs(tt).max(), 1)
    )
    if near_identity:
        return None
    # Shape of the *result* names the intent, independent of fit rows.
    t_std = tt.std()
    if a > 0 and abs(tt.mean()) < 0.1 and t_std > 0 and abs(t_std - 1) < 0.1:
        return "standardized"
    if a > 0 and np.isclose(tt.min(), 0, atol=0.02) and np.isclose(tt.max(), 1, atol=0.02):
        return "min-max scaled"
    if np.isclose(c, 0, atol=1e-06 * max(np.abs(tt).max(), 1)):
        return f"scaled ×{a:g}"
    if np.isclose(a, 1, rtol=1e-06):
        return f"shifted {c:+g}"
    return "linear transform"


def _is_label_encoding(bv: pd.Series, tv: pd.Series, pair: pd.Series) -> bool:
    """String column became numeric codes with a consistent bijection."""
    if not pd.api.types.is_numeric_dtype(tv):
        return False
    if pd.api.types.is_numeric_dtype(bv):
        return False
    b, t = bv[pair], tv[pair]
    if len(b) < MIN_TRANSFORM_CELLS:
        return False
    try:
        forward = pd.DataFrame({"b": b, "t": t}).groupby("b")["t"].nunique()
        backward = pd.DataFrame({"b": b, "t": t}).groupby("t")["b"].nunique()
    except TypeError:
        return False
    return bool((forward == 1).all() and (backward == 1).all())


def _consistent_remap(bv: pd.Series, tv: pd.Series, modified: pd.Series) -> bool:
    """Categorical values consistently mapped old->new, with at least one pair
    recurring (a one-off edit per value is just a change, not a remap)."""
    if pd.api.types.is_numeric_dtype(bv) or pd.api.types.is_numeric_dtype(tv):
        return False
    b, t = bv[modified], tv[modified]
    if len(b) < 2:
        return False
    try:
        pairs = pd.DataFrame({"b": b.astype(str), "t": t.astype(str)})
        mapping = pairs.groupby("b")["t"].nunique()
    except (TypeError, ValueError):
        return False
    return bool((mapping == 1).all() and len(mapping) < len(b) and len(mapping) <= MAX_REMAP_PAIRS)


def _imputation_labels(bv: pd.Series, tv: pd.Series, imputed: pd.Series) -> pd.Series:
    """Per-cell imputation method: mean/median/mode/zero, else 'imputed'."""
    labels = pd.Series("imputed", index=tv.index)[imputed]
    base_nonnull = bv.dropna()
    if pd.api.types.is_numeric_dtype(tv) and len(base_nonnull) > 0:
        t = _numeric(tv)[imputed]
        b = _numeric(base_nonnull)
        for value, name in (
            (b.mean(), "mean imputed"),
            (b.median(), "median imputed"),
            (0, "zero filled"),
        ):
            if value is not None and not pd.isna(value):
                # Loose enough to survive the fill value being rounded on write.
                labels[np.isclose(t, value, rtol=1e-06, atol=1e-09) & (labels == "imputed")] = name
    mode = base_nonnull.mode()
    if len(mode) > 0:
        labels[(tv[imputed] == mode.iloc[0]) & (labels == "imputed")] = "mode imputed"
    # Several cells filled with one identical value that no base statistic
    # reproduces (e.g. stats computed before rows were dropped): still worth
    # naming as a systematic fill rather than independent edits.
    remaining = labels == "imputed"
    if remaining.sum() >= 2:
        values = tv[imputed][remaining]
        counts = values.value_counts()
        if len(counts) > 0 and counts.iloc[0] >= 2:
            labels[(values == counts.index[0]) & remaining] = "constant fill"
    return labels


def classify_column(
    bv: pd.Series, tv: pd.Series, changed: pd.Series
) -> Tuple[pd.Series, pd.Series, Optional[str]]:
    """Returns (kinds, labels) aligned to tv's index plus a column transform note."""
    kinds = pd.Series(None, index=tv.index, dtype=object)
    labels = pd.Series(None, index=tv.index, dtype=object)

    imputed = changed & bv.isna() & ~tv.isna()
    nulled = changed & ~bv.isna() & tv.isna()
    modified = changed & ~imputed & ~nulled

    kinds[imputed] = "imputed"
    labels[imputed] = _imputation_labels(bv, tv, imputed)
    kinds[nulled] = "removed"
    labels[nulled] = "dropped"

    column_note: Optional[str] = None
    if modified.any():
        pair = ~bv.isna() & ~tv.isna()
        # Whole-column recast: same values, different dtype/representation.
        if bv.dtype != tv.dtype:
            try:
                as_str_equal = (
                    bv[modified].astype(str).str.strip() == tv[modified].astype(str).str.strip()
                )
            except (TypeError, ValueError):
                as_str_equal = pd.Series(False, index=tv.index)[modified]
            if len(as_str_equal) > 0 and as_str_equal.mean() >= TRANSFORM_MATCH_RATIO:
                column_note = "recast"
        if column_note is None and _is_label_encoding(bv, tv, pair):
            column_note = "label encoded"
        if column_note is None and pd.api.types.is_numeric_dtype(tv):
            column_note = _detect_numeric_transform(bv, tv, modified)

        if column_note is not None:
            kinds[modified] = "transformed"
            labels[modified] = column_note
        else:
            kinds[modified] = "changed"
            labels[modified] = _cell_string_labels(bv, tv, modified)
            transformed = labels[modified].notna()
            kinds[modified] = np.where(transformed, "transformed", "changed")
            if _consistent_remap(bv, tv, modified):
                unlabeled = modified & labels.isna()
                kinds[unlabeled] = "transformed"
                labels[unlabeled] = "remapped"

    return kinds, labels, column_note


def _cell_string_labels(bv: pd.Series, tv: pd.Series, modified: pd.Series) -> pd.Series:
    """Per-cell string tweaks: case-only and whitespace-only changes."""
    labels = pd.Series(None, index=tv.index, dtype=object)[modified]
    if pd.api.types.is_numeric_dtype(tv) or pd.api.types.is_numeric_dtype(bv):
        return labels
    try:
        b, t = bv[modified].astype(str), tv[modified].astype(str)
    except (TypeError, ValueError):
        return labels
    labels[b.str.strip() == t.str.strip()] = "trimmed"
    labels[(b.str.lower() == t.str.lower()) & labels.isna()] = "case change"
    return labels
