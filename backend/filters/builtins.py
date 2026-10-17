"""
Builtin annotate-step analyzers for the pandas engine.

Both return a frame indexed by row key with canonical column names; the
executor (``annotate_pandas``) renames them to the step's declared outputs by
role. Fuzzy scoring runs over the *deduped value domain* — exact duplicates
share one scored value, so cost is O(u²) on unique values, not rows — and
matched pairs are merged transitively with union-find so chains like
A~B, B~C land in one group.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

GROUP_COL = "group"
SCORE_COL = "score"
MATCH_KEY_COL = "match_key"
COUNT_COL = "count"

# Per-value candidate limit passed to the strategy matcher. Transitive closure
# connects chains, so a small k loses no groups — only redundant edges.
_MAX_MATCHES_PER_VALUE = 6


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        root = i
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[i] != root:  # path compression
            self.parent[i], i = root, self.parent[i]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def fuzzy_duplicate_groups(
    values: pd.Series,
    *,
    strategy: str,
    threshold: float,
    max_rows: int,
) -> Tuple[pd.DataFrame, bool]:
    """Group rows whose text values fuzzy-match (or exactly repeat).

    ``values`` is indexed by row key. Returns ``(frame, degraded)`` where the
    frame (indexed by row key) has GROUP_COL (ordinal, first-occurrence order),
    SCORE_COL (best pair similarity; 1.0 for exact repeats), MATCH_KEY_COL
    (row key of the best-matching *other* row); non-members are all-NaN rows.
    ``degraded`` is True when the row cap truncated the input.
    """
    from tools.linking_fuzzy_tools import _STRATEGIES  # noqa: PLC0415

    degraded = len(values) > max_rows
    work = values.iloc[:max_rows] if degraded else values

    text = work.where(work.isna(), work.astype(str).str.strip())
    valid = text.notna() & (text != "")
    text = text[valid]

    empty = pd.DataFrame(
        {GROUP_COL: np.nan, SCORE_COL: np.nan, MATCH_KEY_COL: None}, index=values.index
    )
    if text.empty:
        return empty, degraded

    uniq: List[str] = list(pd.unique(text))
    index_of: Dict[str, int] = {v: i for i, v in enumerate(uniq)}
    rows_of_value: Dict[str, List] = {}
    for row_key, val in text.items():
        rows_of_value.setdefault(val, []).append(row_key)

    matcher = _STRATEGIES[strategy]
    uniq_series = pd.Series(uniq)
    raw_pairs = matcher(uniq_series, uniq_series, threshold, _MAX_MATCHES_PER_VALUE)

    uf = _UnionFind(len(uniq))
    # best fuzzy partner per unique value: (score, partner_index)
    best: Dict[int, Tuple[float, int]] = {}
    for match in raw_pairs:
        i, j, score = match["left_idx"], match["right_idx"], float(match["score"])
        if i == j:
            continue
        uf.union(i, j)
        if score > best.get(i, (-1.0, -1))[0]:
            best[i] = (score, j)
        if score > best.get(j, (-1.0, -1))[0]:
            best[j] = (score, i)

    # A component qualifies when it spans >= 2 rows: either a fuzzy edge joined
    # values, or a single value repeats exactly (score 1.0).
    root_rows: Dict[int, int] = {}
    for val, keys in rows_of_value.items():
        root = uf.find(index_of[val])
        root_rows[root] = root_rows.get(root, 0) + len(keys)

    group_ordinal: Dict[int, int] = {}
    out = empty.copy()
    for row_key, val in text.items():
        idx = index_of[val]
        root = uf.find(idx)
        if root_rows[root] < 2:
            continue
        if root not in group_ordinal:
            group_ordinal[root] = len(group_ordinal)

        same_value_rows = rows_of_value[val]
        fuzzy = best.get(idx)
        if len(same_value_rows) > 1:
            score = 1.0
            partner_rows = same_value_rows
        else:
            score = fuzzy[0] if fuzzy else 1.0
            partner_rows = rows_of_value[uniq[fuzzy[1]]] if fuzzy else same_value_rows
        match_key = next((k for k in partner_rows if k != row_key), None)

        out.loc[row_key, GROUP_COL] = group_ordinal[root]
        out.loc[row_key, SCORE_COL] = round(score, 4)
        out.loc[row_key, MATCH_KEY_COL] = None if match_key is None else str(match_key)
    return out, degraded


def duplicate_values_pandas(
    df: pd.DataFrame,
    row_keys: pd.Series,
    columns: List[str],
    min_count: int = 2,
) -> pd.DataFrame:
    """Exact-duplicate grouping over a column set.

    Rows with a null in any key column never group (a shared *missing* email is
    not a duplicate email). Returns a frame indexed by row key with COUNT_COL
    and GROUP_COL (first-occurrence ordinals); non-members are NaN.
    """
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(missing[0])

    sub = df[columns].reset_index(drop=True)
    keys = row_keys.reset_index(drop=True)

    grouped = sub.groupby(columns, dropna=True, sort=False)
    # Null-key rows fall out of dropna=True groupbys as NaN in both series.
    counts = grouped[columns[0]].transform("size")
    raw_group = grouped.ngroup()
    member = counts.notna() & (counts >= min_count)

    ordinal_of: Dict[int, int] = {}
    group_vals: List[float] = []
    for pos in range(len(sub)):
        if not bool(member.iloc[pos]):
            group_vals.append(np.nan)
            continue
        g = int(raw_group.iloc[pos])
        if g not in ordinal_of:
            ordinal_of[g] = len(ordinal_of)
        group_vals.append(float(ordinal_of[g]))

    return pd.DataFrame(
        {
            COUNT_COL: counts.where(member).to_numpy(),
            GROUP_COL: np.array(group_vals, dtype=float),
        },
        index=pd.Index(keys),
    )
