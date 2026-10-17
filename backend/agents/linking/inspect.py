"""
Link inspect: materialize candidate scores and look up top-N matches.

The matching strategies already produce per-candidate scores; the join path
used to keep only the winner. This module stores the full candidate table
and serves a read-only inspect report over it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set

import numpy as np
import pandas as pd

from tools.linking_fuzzy_tools import FuzzyLinkingTools

logger = logging.getLogger(__name__)


def _semantic_likely_available() -> bool:
    """Skip companion semantic scoring when embeddings/pgvector cannot run."""
    import importlib.util
    import os

    if importlib.util.find_spec("pgvector") is None:
        return False
    return bool(
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("LITELLM_API_KEY")
        or os.getenv("OPENROUTER_EMBEDDING_MODEL")
    )


SEMANTIC_STRATEGY = "semantic"
LEXICAL_FALLBACK = "token_set"
CANDIDATE_TOP_N = 8
CANDIDATE_FLOOR = 0.25
PREVIEW_ROWS = 80
PREVIEW_COLS = 12
MAX_STORED_CANDIDATES = 20_000
MAX_CACHE_CANDIDATES = 500

# Session metadata keys
STORE_KEY = "linking_inspect_store"
ACTION_KIND = "linking_confirm"


def is_semantic(strategy: Optional[str]) -> bool:
    return (strategy or "").strip().lower() == SEMANTIC_STRATEGY


def lexical_family(strategy: Optional[str]) -> bool:
    """True for exact + every non-semantic fuzzy strategy."""
    return not is_semantic(strategy)


def companion_strategies(join_strategy: str) -> List[str]:
    """
    Strategies to score in addition to the join strategy so the UI can
    toggle semantic vs lexical ranking.
    """
    strat = (join_strategy or "exact").strip().lower()
    extra: List[str] = []
    if strat == SEMANTIC_STRATEGY:
        extra.append(LEXICAL_FALLBACK)
    elif strat == "exact":
        extra.extend([LEXICAL_FALLBACK, SEMANTIC_STRATEGY])
    else:
        extra.append(SEMANTIC_STRATEGY)
    # Deduplicate while preserving order, never re-run the primary.
    seen = {strat}
    out: List[str] = []
    for name in extra:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def json_cell(value: Any) -> Any:
    """JSON-safe scalar for preview / inspect payloads."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        if np.isnan(number) or np.isinf(number):
            return None
        return number
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


def row_snapshot(df: pd.DataFrame, idx: int, columns: Sequence[str]) -> Dict[str, Any]:
    if idx < 0 or idx >= len(df):
        return {"_row_id": idx}
    record = df.iloc[idx]
    snap: Dict[str, Any] = {"_row_id": int(idx)}
    for col in columns:
        if col in df.columns:
            snap[col] = json_cell(record[col])
    return snap


def preview_table(
    df: pd.DataFrame,
    name: str,
    join_key: Optional[str] = None,
    limit: int = PREVIEW_ROWS,
    max_cols: int = PREVIEW_COLS,
) -> Dict[str, Any]:
    cols = list(df.columns)
    if join_key and join_key in cols:
        rest = [c for c in cols if c != join_key]
        cols = [join_key] + rest
    cols = cols[:max_cols]
    rows: List[Dict[str, Any]] = []
    n = min(int(limit), len(df))
    for i in range(n):
        rows.append(row_snapshot(df, i, cols))
    return {
        "name": name,
        "join_key": join_key,
        "columns": cols,
        "rows": rows,
        "row_count": int(len(df)),
        "truncated": bool(len(df) > n),
    }


def _run_strategy(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_on: str,
    right_on: str,
    strategy: str,
    *,
    threshold: float,
    max_matches: int,
    strategy_kwargs: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    kwargs = dict(strategy_kwargs or {})
    scored = FuzzyLinkingTools.score_candidates(
        left_df,
        right_df,
        left_on,
        right_on,
        strategy=strategy,
        threshold=threshold,
        max_matches=max_matches,
        **kwargs,
    )
    if "error" in scored:
        logger.info("inspect scoring skipped for %s: %s", strategy, scored["error"])
        return []
    return list(scored.get("candidates") or [])


def collect_candidates(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_on: str,
    right_on: str,
    join_strategy: str,
    *,
    threshold: float = 0.8,
    top_n: int = CANDIDATE_TOP_N,
    floor: float = CANDIDATE_FLOOR,
    strategy_kwargs: Optional[Dict[str, Any]] = None,
    existing_candidates: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Build the candidate-score table for the join strategy plus a companion
    family so the inspect UI can toggle semantic vs lexical ranking.
    """
    strat = (join_strategy or "exact").strip().lower()
    matcher_floor = min(float(threshold), float(floor)) if strat != "exact" else 0.0
    collected: List[Dict[str, Any]] = []

    if existing_candidates:
        collected.extend(existing_candidates)
        already = {c.get("strategy") for c in existing_candidates}
    else:
        already = set()
        collected.extend(
            _run_strategy(
                left_df,
                right_df,
                left_on,
                right_on,
                strat,
                threshold=matcher_floor if strat != "exact" else 0.0,
                max_matches=top_n,
                strategy_kwargs=strategy_kwargs,
            )
        )
        already.add(strat)

    for extra in companion_strategies(strat):
        if extra in already:
            continue
        if extra == SEMANTIC_STRATEGY and not _semantic_likely_available():
            logger.info("skip companion semantic scoring: embeddings/pgvector not configured")
            continue
        extra_floor = matcher_floor if extra != "exact" else 0.0
        collected.extend(
            _run_strategy(
                left_df,
                right_df,
                left_on,
                right_on,
                extra,
                threshold=extra_floor,
                max_matches=top_n,
                strategy_kwargs=strategy_kwargs,
            )
        )

    if len(collected) > MAX_STORED_CANDIDATES:
        collected = collected[:MAX_STORED_CANDIDATES]
    return collected


def build_inspect_store(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    *,
    left_name: str,
    right_name: str,
    left_on: str,
    right_on: str,
    strategy: str,
    threshold: float,
    join_type: str = "INNER",
    candidates: List[Dict[str, Any]],
    committed: bool = False,
    preview_rows: int = PREVIEW_ROWS,
) -> Dict[str, Any]:
    """
    Compact session payload: preview tables, plan, candidate table, row cache.
    """
    left_preview = preview_table(left_df, left_name, left_on, limit=preview_rows)
    right_preview = preview_table(right_df, right_name, right_on, limit=preview_rows)
    columns_left = left_preview["columns"]
    columns_right = right_preview["columns"]

    preview_source_ids = {int(r["_row_id"]) for r in left_preview["rows"]}
    needed_right: Set[int] = set()
    needed_left: Set[int] = set(preview_source_ids)
    for cand in candidates:
        src = int(cand["source_row_id"])
        if src in preview_source_ids:
            needed_right.add(int(cand["candidate_row_id"]))
            needed_left.add(src)

    if len(needed_right) > MAX_CACHE_CANDIDATES:
        ranked: List[tuple] = []
        for cand in candidates:
            src = int(cand["source_row_id"])
            if src in preview_source_ids:
                ranked.append((float(cand.get("score") or 0.0), int(cand["candidate_row_id"])))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        needed_right = set()
        for _, rid in ranked:
            needed_right.add(rid)
            if len(needed_right) >= MAX_CACHE_CANDIDATES:
                break

    left_cache = {
        str(i): row_snapshot(left_df, i, columns_left) for i in needed_left if 0 <= i < len(left_df)
    }
    right_cache = {
        str(i): row_snapshot(right_df, i, columns_right)
        for i in needed_right
        if 0 <= i < len(right_df)
    }

    strategies = sorted({str(c.get("strategy")) for c in candidates if c.get("strategy")})
    has_semantic = SEMANTIC_STRATEGY in strategies
    has_lexical = any(lexical_family(s) for s in strategies)

    return {
        "candidates": candidates,
        "left_row_cache": left_cache,
        "right_row_cache": right_cache,
        "committed": bool(committed),
        "context": {
            "left": left_preview,
            "right": right_preview,
            "plan": {
                "left": left_name,
                "right": right_name,
                "left_on": left_on,
                "right_on": right_on,
                "strategy": strategy,
                "threshold": float(threshold),
                "join_type": join_type,
            },
            "available_strategies": strategies,
            "has_semantic": has_semantic,
            "has_lexical": has_lexical,
            "committed": bool(committed),
        },
    }


def public_context(store: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip the bulky candidate table before sending context to the client."""
    if not store:
        return None
    from .viz import VIZ_KINDS

    ctx = dict(store.get("context") or {})
    ctx["committed"] = bool(store.get("committed", ctx.get("committed", False)))
    ctx["viz"] = {"kinds": list(VIZ_KINDS)}
    return ctx


def inspect_source_row(
    store: Dict[str, Any],
    source_row_id: int,
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
    top_n: int = CANDIDATE_TOP_N,
) -> Dict[str, Any]:
    """
    Filter the stored candidate table for one source row.

    *strategy* may be a concrete matcher name, or the family aliases
    ``semantic`` / ``lexical``. When omitted, all strategies are returned
    so the client can toggle without a second round-trip.

    When *strategy* / family is omitted, *top_n* is applied **per strategy
    group** so the caller receives results for every strategy at once.
    The client then filters by family. Maximum returned =
    ``top_n × len(strategies)``. When a family is set, *top_n* caps the
    combined list.
    """
    candidates = list(store.get("candidates") or [])
    ctx = store.get("context") or {}
    plan = ctx.get("plan") or {}
    left_cache: Dict[str, Any] = store.get("left_row_cache") or {}
    right_cache: Dict[str, Any] = store.get("right_row_cache") or {}

    family = (strategy or "").strip().lower() or None
    cutoff = None if threshold is None else float(threshold)

    def _wanted(strat_name: str) -> bool:
        if not family:
            return True
        if family == "lexical":
            return lexical_family(strat_name)
        if family == SEMANTIC_STRATEGY:
            return is_semantic(strat_name)
        return strat_name == family

    matched: List[Dict[str, Any]] = []
    for cand in candidates:
        if int(cand.get("source_row_id", -1)) != int(source_row_id):
            continue
        strat_name = str(cand.get("strategy") or "")
        if not _wanted(strat_name):
            continue
        score = float(cand.get("score") or 0.0)
        if cutoff is not None and score < cutoff:
            continue
        rid = int(cand["candidate_row_id"])
        row = right_cache.get(str(rid)) or {
            "_row_id": rid,
            plan.get("right_on") or "value": cand.get("candidate_value"),
        }
        matched.append(
            {
                "candidate_row_id": rid,
                "score": round(score, 4),
                "strategy": strat_name,
                "value": cand.get("candidate_value"),
                "source_value": cand.get("source_value"),
                "row": row,
                "passes_threshold": (True if cutoff is None else score >= cutoff),
            }
        )

    matched.sort(key=lambda c: c["score"], reverse=True)
    cap = max(int(top_n), 1)
    # When family is None, the cap applies per strategy group so the
    # caller receives results for all strategies simultaneously. The client
    # filters by family client-side. Max returned = top_n × len(strategies).
    if family:
        matched = matched[:cap]
    else:
        by_strat: Dict[str, List[Dict[str, Any]]] = {}
        for item in matched:
            by_strat.setdefault(str(item["strategy"]), []).append(item)
        trimmed: List[Dict[str, Any]] = []
        for group in by_strat.values():
            trimmed.extend(group[:cap])
        trimmed.sort(key=lambda c: c["score"], reverse=True)
        matched = trimmed

    source_row = left_cache.get(str(source_row_id)) or {
        "_row_id": int(source_row_id),
        plan.get("left_on") or "value": matched[0]["source_value"] if matched else None,
    }

    return {
        "source_row_id": int(source_row_id),
        "source_row": source_row,
        "source_value": (
            source_row.get(plan.get("left_on"))
            if plan.get("left_on")
            else (matched[0]["source_value"] if matched else None)
        ),
        "join_key": plan.get("left_on"),
        "candidate_join_key": plan.get("right_on"),
        "strategy": family or plan.get("strategy"),
        "threshold": cutoff if cutoff is not None else plan.get("threshold"),
        "candidates": matched,
        "strategies": sorted({c["strategy"] for c in matched}),
    }


def mark_committed(store: Dict[str, Any]) -> Dict[str, Any]:
    store = dict(store)
    store["committed"] = True
    ctx = dict(store.get("context") or {})
    ctx["committed"] = True
    store["context"] = ctx
    return store
