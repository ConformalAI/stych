"""
Fuzzy and similarity-based data linking tools.

Provides multiple matching strategies for joining records that don't
share exact keys: fuzzy string matching, token-based matching,
cosine similarity (TF-IDF), phonetic matching, and semantic embedding
similarity via pgvector.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Strategy registry — maps strategy names to matcher functions
# -------------------------------------------------------------------------

_STRATEGIES = {}


def _register(name: str):
    """Decorator that registers a matcher function by strategy name."""

    def decorator(fn):
        _STRATEGIES[name] = fn
        return fn

    return decorator


def available_strategies() -> List[str]:
    """Return the names of all registered fuzzy strategies."""
    return sorted(_STRATEGIES.keys())


# -------------------------------------------------------------------------
# FuzzyLinkingTools
# -------------------------------------------------------------------------


class FuzzyLinkingTools:
    """Static helper methods for similarity-based data linking."""

    # ----- main entry point ---------------------------------------------

    @staticmethod
    def fuzzy_join(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        left_on: str,
        right_on: str,
        strategy: str = "levenshtein",
        threshold: float = 0.8,
        max_matches: int = 1,
        join_type: str = "inner",
        **strategy_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Join two DataFrames using a similarity strategy.

        Parameters
        ----------
        strategy : one of levenshtein, jaro_winkler, token_sort,
                   token_set, partial_ratio, cosine_tfidf,
                   soundex, metaphone, semantic
        threshold : minimum similarity score (0-1) to consider a match
        max_matches : how many right-side matches to keep per left row
        join_type : "inner" keeps only matched rows;
                    "left" keeps all left rows (unmatched get NaN)
        **strategy_kwargs : optional per-strategy extras. The `semantic`
            strategy accepts `db_config`, `embedding_model`,
            `embedding_dimensions`, and `hnsw_min_rows`. Other strategies
            ignore unknown kwargs.

            Inspect extras (stripped before the matcher runs):
            `candidate_top_n` — how many scored neighbours to keep per
            source row (default 8); `candidate_floor` — minimum score
            for the candidate table, typically below the join threshold
            so near-misses remain inspectable.

        Returns a dict with the joined DataFrame, match details,
        per-candidate scores, and statistics.
        """
        if strategy not in _STRATEGIES:
            return {
                "error": (f"Unknown strategy '{strategy}'. " f"Available: {available_strategies()}")
            }

        try:
            # Validate columns exist
            if left_on not in left_df.columns:
                return {"error": f"Column '{left_on}' not found in left dataset"}
            if right_on not in right_df.columns:
                return {"error": f"Column '{right_on}' not found in right dataset"}

            candidate_top_n = int(strategy_kwargs.pop("candidate_top_n", 8))
            raw_floor = strategy_kwargs.pop("candidate_floor", None)
            candidate_floor = (
                float(raw_floor) if raw_floor is not None else min(float(threshold), 0.25)
            )
            matcher_k = max(int(max_matches), max(candidate_top_n, 1))

            scored = FuzzyLinkingTools.score_candidates(
                left_df,
                right_df,
                left_on,
                right_on,
                strategy=strategy,
                threshold=candidate_floor,
                max_matches=matcher_k,
                **strategy_kwargs,
            )
            if "error" in scored:
                return scored

            all_matches = scored["matches"]
            join_matches = _select_join_matches(all_matches, float(threshold), int(max_matches))

            # Build result DataFrame from the join subset (best match / threshold)
            result_df, match_details = FuzzyLinkingTools._build_result(
                left_df,
                right_df,
                join_matches,
                left_on,
                right_on,
                join_type,
            )

            # Statistics
            total_left = len(left_df)
            matched_left = len(set(m["left_idx"] for m in join_matches))
            avg_score = np.mean([m["score"] for m in join_matches]) if join_matches else 0.0

            return {
                "data": result_df.to_dict(orient="list"),
                "rows": len(result_df),
                "columns": list(result_df.columns),
                "strategy": strategy,
                "threshold": threshold,
                "statistics": {
                    "left_rows": total_left,
                    "right_rows": len(right_df),
                    "matched_rows": matched_left,
                    "match_rate": round(matched_left / max(total_left, 1) * 100, 2),
                    "avg_similarity": round(float(avg_score), 4),
                    "total_matches": len(join_matches),
                },
                "match_details": match_details[:100],  # cap for payload size
                "candidates": scored.get("candidates") or [],
            }

        except ImportError as e:
            return {
                "error": (
                    f"Missing dependency for strategy '{strategy}': {e}. "
                    "Install with: pip install rapidfuzz scikit-learn jellyfish "
                    "pgvector psycopg2-binary httpx"
                )
            }
        except Exception as e:
            logger.error("fuzzy_join failed: %s", e)
            return {"error": str(e)}

    # ----- compare strategies ------------------------------------------

    @staticmethod
    def compare_strategies(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        left_on: str,
        right_on: str,
        strategies: Optional[List[str]] = None,
        threshold: float = 0.8,
        sample_size: int = 100,
        **strategy_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Run multiple strategies on a sample and compare match rates.

        Useful for deciding which strategy works best before running
        the full join. Per-strategy kwargs (e.g. `db_config` for the
        semantic strategy) flow through via **strategy_kwargs.
        """
        if strategies is None:
            strategies = available_strategies()

        # Sample randomly so strategy comparison is not biased towards
        # the head of sorted or time-ordered data.
        left_sample = (
            left_df.sample(n=min(sample_size, len(left_df)), random_state=42)
            if len(left_df) > sample_size
            else left_df
        )
        right_sample = (
            right_df.sample(n=min(sample_size, len(right_df)), random_state=42)
            if len(right_df) > sample_size
            else right_df
        )

        results: List[Dict[str, Any]] = []
        for strat in strategies:
            try:
                result = FuzzyLinkingTools.fuzzy_join(
                    left_sample,
                    right_sample,
                    left_on,
                    right_on,
                    strategy=strat,
                    threshold=threshold,
                    max_matches=1,
                    join_type="inner",
                    **strategy_kwargs,
                )
                if "error" not in result:
                    results.append(
                        {
                            "strategy": strat,
                            "match_rate": result["statistics"]["match_rate"],
                            "avg_similarity": result["statistics"]["avg_similarity"],
                            "matched_rows": result["statistics"]["matched_rows"],
                        }
                    )
                else:
                    results.append(
                        {
                            "strategy": strat,
                            "error": result["error"],
                        }
                    )
            except Exception as e:
                results.append({"strategy": strat, "error": str(e)})

        # Rank by match rate
        valid = [r for r in results if "error" not in r]
        ranked = sorted(valid, key=lambda x: x["match_rate"], reverse=True)

        return {
            "sample_size": len(left_sample),
            "threshold": threshold,
            "results": results,
            "recommended": ranked[0]["strategy"] if ranked else None,
        }

    # ----- analyze match quality ----------------------------------------

    @staticmethod
    def analyze_fuzzy_quality(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        left_on: str,
        right_on: str,
    ) -> Dict[str, Any]:
        """
        Analyze column characteristics to help decide between
        exact vs fuzzy matching and which strategy fits best.
        """
        try:
            left_vals = left_df[left_on].astype(str).str.strip()
            right_vals = right_df[right_on].astype(str).str.strip()

            # Exact overlap
            left_set = set(left_vals.str.lower())
            right_set = set(right_vals.str.lower())
            exact_overlap = len(left_set & right_set)

            # Length statistics
            left_lengths = left_vals.str.len()
            right_lengths = right_vals.str.len()

            # Token count (proxy for multi-word fields)
            left_tokens = left_vals.str.split().apply(len)
            right_tokens = right_vals.str.split().apply(len)

            # Unique ratio (high = IDs, low = categories)
            left_unique_ratio = left_vals.nunique() / max(len(left_vals), 1)
            right_unique_ratio = right_vals.nunique() / max(len(right_vals), 1)

            analysis = {
                "left_column": {
                    "name": left_on,
                    "distinct": int(left_vals.nunique()),
                    "nulls": int(left_vals.isin(["", "nan", "None"]).sum()),
                    "avg_length": round(float(left_lengths.mean()), 1),
                    "avg_tokens": round(float(left_tokens.mean()), 1),
                    "unique_ratio": round(float(left_unique_ratio), 3),
                    "sample": [v[:120] for v in left_vals.head(5).tolist()],
                },
                "right_column": {
                    "name": right_on,
                    "distinct": int(right_vals.nunique()),
                    "nulls": int(right_vals.isin(["", "nan", "None"]).sum()),
                    "avg_length": round(float(right_lengths.mean()), 1),
                    "avg_tokens": round(float(right_tokens.mean()), 1),
                    "unique_ratio": round(float(right_unique_ratio), 3),
                    "sample": [v[:120] for v in right_vals.head(5).tolist()],
                },
                "exact_overlap": exact_overlap,
                "exact_overlap_pct_left": round(exact_overlap / max(len(left_set), 1) * 100, 2),
            }

            # Suggest strategy
            suggestions = []
            overlap_pct = exact_overlap / max(len(left_set), 1)
            avg_tokens = max(float(left_tokens.mean()), float(right_tokens.mean()))
            avg_length = max(float(left_lengths.mean()), float(right_lengths.mean()))

            if overlap_pct > 0.9:
                suggestions.append("High exact overlap (>90%) — exact join may be sufficient")
            else:
                # If overlap is very low AND values look like free-form text,
                # semantic matching often beats lexical strategies.
                if overlap_pct < 0.3 and (avg_tokens > 1.5 or avg_length > 15):
                    suggestions.append(
                        "Low exact overlap with multi-word or longer text — "
                        "try the 'semantic' strategy (embeddings) before token-based ones"
                    )

                if avg_tokens > 3:
                    suggestions.append("Multi-word fields — try cosine_tfidf or token_set")
                elif avg_tokens > 1.5:
                    suggestions.append("Short multi-word fields — try token_sort or jaro_winkler")
                else:
                    suggestions.append("Single-word fields — try levenshtein or soundex")

            analysis["suggestions"] = suggestions
            return analysis

        except Exception as e:
            logger.error("analyze_fuzzy_quality failed: %s", e)
            return {"error": str(e)}

    # ----- candidate scoring (inspect) ----------------------------------

    @staticmethod
    def score_candidates(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        left_on: str,
        right_on: str,
        strategy: str = "levenshtein",
        threshold: float = 0.25,
        max_matches: int = 8,
        **strategy_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Score per-row candidates without building a joined DataFrame.

        Used by fuzzy_join (to keep the candidate table that would otherwise
        be discarded) and by the inspect helpers (companion strategies).
        """
        if left_on not in left_df.columns:
            return {"error": f"Column '{left_on}' not found in left dataset"}
        if right_on not in right_df.columns:
            return {"error": f"Column '{right_on}' not found in right dataset"}

        # SQL-style null semantics: missing keys never match. Matchers use
        # compact positional indices, so map them back to original rows.
        left_valid = np.flatnonzero(left_df[left_on].notna().to_numpy())
        right_valid = np.flatnonzero(right_df[right_on].notna().to_numpy())
        left_keys = left_df[left_on].iloc[left_valid].astype(str).str.strip().reset_index(drop=True)
        right_keys = (
            right_df[right_on].iloc[right_valid].astype(str).str.strip().reset_index(drop=True)
        )

        try:
            if strategy == "exact":
                matches = _match_exact(left_keys, right_keys, max_matches)
            else:
                if strategy not in _STRATEGIES:
                    return {
                        "error": (
                            f"Unknown strategy '{strategy}'. "
                            f"Available: {available_strategies()}"
                        )
                    }
                matcher_fn = _STRATEGIES[strategy]
                matches = matcher_fn(
                    left_keys,
                    right_keys,
                    float(threshold),
                    int(max_matches),
                    **strategy_kwargs,
                )
            for match in matches:
                match["left_idx"] = int(left_valid[match["left_idx"]])
                match["right_idx"] = int(right_valid[match["right_idx"]])
        except ImportError as e:
            return {
                "error": (
                    f"Missing dependency for strategy '{strategy}': {e}. "
                    "Install with: pip install rapidfuzz scikit-learn jellyfish "
                    "pgvector psycopg2-binary httpx"
                )
            }
        except Exception as e:
            logger.error("score_candidates failed (%s): %s", strategy, e)
            return {"error": str(e)}

        candidates = _matches_to_candidates(matches, left_df, right_df, left_on, right_on, strategy)
        return {"matches": matches, "candidates": candidates}

    # =====================================================================
    # Internal: build result DataFrame from match list
    # =====================================================================

    @staticmethod
    def _build_result(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        matches: List[Dict[str, Any]],
        left_on: str,
        right_on: str,
        join_type: str,
    ) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
        """
        Combine left and right rows based on match list.
        Returns (result_df, match_details).
        """
        match_details: List[Dict[str, Any]] = []
        result_rows: List[Dict[str, Any]] = []
        matched_left_indices = set()
        matched_right_indices = set()

        normalized_join_type = join_type.lower().strip()
        if normalized_join_type in {"full", "full outer"}:
            normalized_join_type = "outer"
        if normalized_join_type not in {"inner", "left", "right", "outer"}:
            raise ValueError(
                f"Unsupported fuzzy join type '{join_type}'. "
                "Supported: inner, left, right, full outer."
            )

        # Prefix every overlapping right-hand name, including a shared fuzzy
        # key. Unlike an exact join, the two matched key values can differ, so
        # dropping either one would hide the evidence used to approve a match.
        right_cols_rename = {}
        for col in right_df.columns:
            if col in left_df.columns:
                candidate = f"right_{col}"
                suffix = 2
                occupied = (
                    set(left_df.columns) | set(right_df.columns) | set(right_cols_rename.values())
                )
                while candidate in occupied:
                    candidate = f"right_{col}_{suffix}"
                    suffix += 1
                right_cols_rename[col] = candidate

        right_output_columns = [right_cols_rename.get(col, col) for col in right_df.columns]

        def _left_only_row(index: int) -> Dict[str, Any]:
            row = left_df.iloc[index].to_dict()
            for column in right_output_columns:
                if column not in row:
                    row[column] = None
            row["_match_score"] = None
            return row

        def _right_only_row(index: int) -> Dict[str, Any]:
            row = {column: None for column in left_df.columns}
            right_row = right_df.iloc[index].to_dict()
            for old_name, new_name in right_cols_rename.items():
                if old_name in right_row:
                    right_row[new_name] = right_row.pop(old_name)
            row.update(right_row)
            row["_match_score"] = None
            return row

        for m in matches:
            li, ri, score = m["left_idx"], m["right_idx"], m["score"]
            matched_left_indices.add(li)
            matched_right_indices.add(ri)

            left_row = left_df.iloc[li].to_dict()
            right_row = right_df.iloc[ri].to_dict()

            # Rename overlapping columns from right
            for old_name, new_name in right_cols_rename.items():
                if old_name in right_row:
                    right_row[new_name] = right_row.pop(old_name)

            combined = {**left_row, **right_row}
            combined["_match_score"] = round(score, 4)
            result_rows.append(combined)

            match_details.append(
                {
                    "left_value": str(left_df.iloc[li][left_on]),
                    "right_value": str(right_df.iloc[ri][right_on]),
                    "score": round(score, 4),
                }
            )

        if normalized_join_type in {"left", "outer"}:
            for i in range(len(left_df)):
                if i not in matched_left_indices:
                    result_rows.append(_left_only_row(i))

        if normalized_join_type in {"right", "outer"}:
            for i in range(len(right_df)):
                if i not in matched_right_indices:
                    result_rows.append(_right_only_row(i))

        output_columns = [*left_df.columns, *right_output_columns, "_match_score"]
        # dict construction already avoids duplicates, but keep the explicit
        # order stable for empty and non-empty results alike.
        output_columns = list(dict.fromkeys(output_columns))
        result_df = pd.DataFrame(result_rows, columns=output_columns)
        return result_df, match_details


# =========================================================================
# Candidate-table helpers (inspect)
# =========================================================================


def _select_join_matches(
    matches: List[Dict[str, Any]],
    threshold: float,
    max_matches: int,
) -> List[Dict[str, Any]]:
    """Keep the join subset: scores >= threshold, at most *max_matches* per left row."""
    by_left: Dict[int, List[Dict[str, Any]]] = {}
    for m in matches:
        if float(m.get("score", 0.0)) < threshold:
            continue
        by_left.setdefault(int(m["left_idx"]), []).append(m)

    selected: List[Dict[str, Any]] = []
    for group in by_left.values():
        group.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)
        selected.extend(group[: max(int(max_matches), 1)])
    return selected


def _matches_to_candidates(
    matches: List[Dict[str, Any]],
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_on: str,
    right_on: str,
    strategy: str,
) -> List[Dict[str, Any]]:
    """Materialize (source_row_id, candidate_row_id, score, strategy) rows."""
    candidates: List[Dict[str, Any]] = []
    n_left = len(left_df)
    n_right = len(right_df)
    for m in matches:
        li = int(m["left_idx"])
        ri = int(m["right_idx"])
        if li < 0 or ri < 0 or li >= n_left or ri >= n_right:
            continue
        candidates.append(
            {
                "source_row_id": li,
                "candidate_row_id": ri,
                "score": round(float(m["score"]), 4),
                "strategy": strategy,
                "source_value": str(left_df.iloc[li][left_on]),
                "candidate_value": str(right_df.iloc[ri][right_on]),
            }
        )
    return candidates


def _match_exact(
    left_keys: pd.Series,
    right_keys: pd.Series,
    max_matches: int,
) -> List[Dict[str, Any]]:
    """Exact (case-insensitive, stripped) matches with score 1.0."""
    right_index: Dict[str, List[int]] = {}
    for j, val in enumerate(right_keys.tolist()):
        key = str(val).strip().lower()
        if not key or key in {"nan", "none"}:
            continue
        right_index.setdefault(key, []).append(j)

    matches: List[Dict[str, Any]] = []
    cap = max(int(max_matches), 1)
    for i, val in enumerate(left_keys.tolist()):
        key = str(val).strip().lower()
        if not key or key not in right_index:
            continue
        for j in right_index[key][:cap]:
            matches.append({"left_idx": i, "right_idx": j, "score": 1.0})
    return matches


# =========================================================================
# Matcher functions — each registered under a strategy name
#
# All matchers accept **kwargs to absorb strategy-specific extras
# (e.g. `db_config` for `semantic`). Strategies that don't need them
# simply ignore the kwargs.
# =========================================================================


@_register("levenshtein")
def _match_levenshtein(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Levenshtein distance normalised to 0-1 similarity."""
    from rapidfuzz import fuzz

    return _pairwise_match(
        left_keys,
        right_keys,
        threshold,
        max_matches,
        scorer=lambda a, b: fuzz.ratio(a, b) / 100.0,
    )


@_register("jaro_winkler")
def _match_jaro_winkler(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Jaro-Winkler similarity — good for short strings like names."""
    import jellyfish

    return _pairwise_match(
        left_keys,
        right_keys,
        threshold,
        max_matches,
        scorer=lambda a, b: jellyfish.jaro_winkler_similarity(a, b),
    )


@_register("token_sort")
def _match_token_sort(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Token sort ratio — handles word reordering."""
    from rapidfuzz import fuzz

    return _pairwise_match(
        left_keys,
        right_keys,
        threshold,
        max_matches,
        scorer=lambda a, b: fuzz.token_sort_ratio(a, b) / 100.0,
    )


@_register("token_set")
def _match_token_set(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Token set ratio — handles extra/missing words."""
    from rapidfuzz import fuzz

    return _pairwise_match(
        left_keys,
        right_keys,
        threshold,
        max_matches,
        scorer=lambda a, b: fuzz.token_set_ratio(a, b) / 100.0,
    )


@_register("partial_ratio")
def _match_partial_ratio(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Partial ratio — finds best substring match."""
    from rapidfuzz import fuzz

    return _pairwise_match(
        left_keys,
        right_keys,
        threshold,
        max_matches,
        scorer=lambda a, b: fuzz.partial_ratio(a, b) / 100.0,
    )


@_register("cosine_tfidf")
def _match_cosine_tfidf(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Cosine similarity on TF-IDF vectors.
    Best for longer text fields like descriptions or addresses.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    left_list = left_keys.tolist()
    right_list = right_keys.tolist()

    # Fit on combined vocabulary
    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        lowercase=True,
    )
    all_texts = left_list + right_list
    vectorizer.fit(all_texts)

    left_vecs = vectorizer.transform(left_list)
    right_vecs = vectorizer.transform(right_list)

    # Compute similarity matrix
    sim_matrix = cosine_similarity(left_vecs, right_vecs)

    matches: List[Dict[str, Any]] = []
    for i in range(len(left_list)):
        row_scores = sim_matrix[i]
        # Get top-k matches above threshold
        top_indices = np.argsort(row_scores)[::-1][:max_matches]
        for j in top_indices:
            score = float(row_scores[j])
            if score >= threshold:
                matches.append(
                    {
                        "left_idx": i,
                        "right_idx": int(j),
                        "score": score,
                    }
                )

    return matches


@_register("soundex")
def _match_soundex(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Soundex phonetic matching.
    Groups words that sound alike (e.g. Smith / Smyth).
    Score is 1.0 for a match, 0.0 for no match.
    """
    import jellyfish

    left_list = left_keys.tolist()
    right_list = right_keys.tolist()

    # Pre-compute soundex codes
    left_codes = [jellyfish.soundex(s) if s.strip() else "" for s in left_list]
    right_codes = [jellyfish.soundex(s) if s.strip() else "" for s in right_list]

    # Build index: soundex_code → list of right indices
    code_index: Dict[str, List[int]] = {}
    for j, code in enumerate(right_codes):
        if code:
            code_index.setdefault(code, []).append(j)

    matches: List[Dict[str, Any]] = []
    for i, code in enumerate(left_codes):
        if code and code in code_index:
            for j in code_index[code][:max_matches]:
                matches.append(
                    {
                        "left_idx": i,
                        "right_idx": j,
                        "score": 1.0,
                    }
                )

    return matches


@_register("metaphone")
def _match_metaphone(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **_kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Metaphone phonetic matching.
    More accurate than Soundex for English names.
    """
    import jellyfish

    left_list = left_keys.tolist()
    right_list = right_keys.tolist()

    left_codes = [jellyfish.metaphone(s) if s.strip() else "" for s in left_list]
    right_codes = [jellyfish.metaphone(s) if s.strip() else "" for s in right_list]

    code_index: Dict[str, List[int]] = {}
    for j, code in enumerate(right_codes):
        if code:
            code_index.setdefault(code, []).append(j)

    matches: List[Dict[str, Any]] = []
    for i, code in enumerate(left_codes):
        if code and code in code_index:
            for j in code_index[code][:max_matches]:
                matches.append(
                    {
                        "left_idx": i,
                        "right_idx": j,
                        "score": 1.0,
                    }
                )

    return matches


@_register("semantic")
def _match_semantic(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Semantic similarity via embeddings + pgvector kNN.

    Embeds both sides with the configured embedding model (Gemini
    Embedding 2 Preview by default), loads the right side into a
    pgvector temp table, optionally builds an HNSW index, and does a
    per-row kNN query for each left value.

    Recognised kwargs
    -----------------
    db_config            : dict — Postgres connection params; if absent,
                           the underlying get_connection() defaults are used.
    embedding_model      : str — overrides OPENROUTER_EMBEDDING_MODEL
    embedding_dimensions : int — overrides EMBEDDING_DIMENSIONS
    hnsw_min_rows        : int — row count above which HNSW is built
                           (default: 10_000)

    Note
    ----
    The threshold here is a *cosine similarity* (higher = better),
    matching the convention used by every other strategy in this module.
    """
    # Local imports keep this module importable even when pgvector /
    # embeddings deps are missing (the import error surfaces via
    # fuzzy_join's ImportError handler with a helpful install hint).
    from tools.linking_tools import get_connection

    from .embeddings import embed_texts
    from .vector_store import VectorStore

    db_config = kwargs.get("db_config") or {}
    embedding_model = kwargs.get("embedding_model")
    embedding_dimensions = kwargs.get("embedding_dimensions")
    hnsw_min_rows = int(kwargs.get("hnsw_min_rows", 10_000))

    left_list = left_keys.tolist()
    right_list = right_keys.tolist()

    if not left_list or not right_list:
        return []

    # ---- Embed both sides -------------------------------------------
    logger.info(
        "semantic strategy: embedding %d left + %d right values",
        len(left_list),
        len(right_list),
    )
    left_vecs = embed_texts(
        left_list,
        model=embedding_model,
        dimensions=embedding_dimensions,
    )
    right_vecs = embed_texts(
        right_list,
        model=embedding_model,
        dimensions=embedding_dimensions,
    )

    if left_vecs.shape[0] == 0 or right_vecs.shape[0] == 0:
        return []

    dim = int(right_vecs.shape[1])

    # ---- Load right side into pgvector + kNN ------------------------
    # Use a uniquely-named temp table so concurrent semantic joins on
    # the same connection don't clobber each other (unlikely in
    # practice, but cheap insurance).
    import uuid

    table_name = f"semantic_right_{uuid.uuid4().hex[:12]}"

    conn = get_connection(db_config)
    try:
        VectorStore.ensure_extension(conn)
        VectorStore.create_temp_table(conn, table_name, dim=dim)
        VectorStore.bulk_insert(
            conn,
            table_name,
            values=[str(v) for v in right_list],
            embeddings=right_vecs,
        )
        VectorStore.maybe_create_hnsw(conn, table_name, min_rows=hnsw_min_rows)

        matches: List[Dict[str, Any]] = []
        for i, qvec in enumerate(left_vecs):
            # Skip rows that came back as all-zero (blank source text)
            if not np.any(qvec):
                continue
            hits = VectorStore.knn(
                conn,
                table_name,
                qvec,
                k=max_matches,
                threshold=float(threshold),
            )
            for right_idx, sim in hits:
                matches.append(
                    {
                        "left_idx": i,
                        "right_idx": int(right_idx),
                        "score": float(sim),
                    }
                )

        return matches
    finally:
        # Closing the connection drops the temp table automatically.
        conn.close()


# =========================================================================
# Shared pairwise matcher used by string-distance strategies
# =========================================================================


def _pairwise_match(
    left_keys: pd.Series,
    right_keys: pd.Series,
    threshold: float,
    max_matches: int,
    scorer,
) -> List[Dict[str, Any]]:
    """
    Generic pairwise scoring loop.

    For large datasets (>5000 × 5000) this becomes slow — consider
    blocking/indexing strategies in production.
    """
    left_list = left_keys.tolist()
    right_list = right_keys.tolist()

    # Optimisation: use rapidfuzz's cdist if available and scorer is from rapidfuzz
    try:
        from rapidfuzz import process

        matches: List[Dict[str, Any]] = []
        for i, left_val in enumerate(left_list):
            if not left_val.strip():
                continue
            results = process.extract(
                left_val,
                right_list,
                scorer=lambda a, b, **kw: scorer(a, b) * 100,
                limit=max_matches,
                score_cutoff=threshold * 100,
            )
            for val, score, j in results:
                matches.append(
                    {
                        "left_idx": i,
                        "right_idx": j,
                        "score": score / 100.0,
                    }
                )
        return matches

    except (ImportError, TypeError):
        pass

    # Fallback: plain nested loop
    matches = []
    for i, left_val in enumerate(left_list):
        if not left_val.strip():
            continue
        best: List[Tuple[int, float]] = []
        for j, right_val in enumerate(right_list):
            if not right_val.strip():
                continue
            score = scorer(left_val, right_val)
            if score >= threshold:
                best.append((j, score))

        # Keep top-k
        best.sort(key=lambda x: x[1], reverse=True)
        for j, score in best[:max_matches]:
            matches.append(
                {
                    "left_idx": i,
                    "right_idx": j,
                    "score": score,
                }
            )

    return matches
