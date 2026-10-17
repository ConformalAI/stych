"""
Data Linking Agent for SQL-based and similarity-based table joining.

Receives data in various formats (CSV, JSON, dict), loads them into
PostgreSQL temporary tables, and performs JOIN operations.  Supports
both exact SQL joins and fuzzy/similarity-based matching (Levenshtein,
Jaro-Winkler, token-based, cosine TF-IDF, phonetic, and semantic
embeddings via pgvector).

The agent can execute explicitly requested joins, use the LLM to
recommend the best linking strategy (including fuzzy vs exact), or
compare multiple fuzzy strategies side by side.
"""

import json as _json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

from llm.infer import LLMRouter
from tools.linking_fuzzy_tools import FuzzyLinkingTools
from tools.linking_tools import (
    DataLinkingTools,
    get_connection,
    parse_input_to_dataframe,
)

from ..base.agent import BaseStychAgent
from .inspect import (
    CANDIDATE_FLOOR,
    CANDIDATE_TOP_N,
    build_inspect_store,
    collect_candidates,
    inspect_source_row,
    public_context,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Strategies that need a Postgres connection passed through. Listed here
# so we can decide *once* whether to inject db_config rather than
# sprinkling that knowledge through each call site.
_DB_AWARE_STRATEGIES = {"semantic"}
_CURRENT_PARITY_STRATEGIES = [
    "levenshtein",
    "jaro_winkler",
    "token_sort",
    "token_set",
    "partial_ratio",
    "cosine_tfidf",
    "soundex",
    "metaphone",
]

# Per-section char budget for JSON blobs inlined into recommendation prompts.
# Backstop against pathological payloads (production data showed a 159K-token
# recommend prompt when uncapped sample values leaked huge cell contents).
_PROMPT_SECTION_CHARS = 8_000


def _clip_json(obj: Any, limit: int = _PROMPT_SECTION_CHARS) -> str:
    """Serialize *obj* to JSON, hard-capped at *limit* chars."""
    blob = _json.dumps(obj, default=str)
    if len(blob) <= limit:
        return blob
    return blob[:limit] + f"... [truncated {len(blob) - limit} chars]"


class DataLinkingAgent(BaseStychAgent):
    """Agent for SQL-based and similarity-based data linking."""

    def __init__(
        self,
        llm_router: LLMRouter,
        db_config: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name="DataLinkingAgent",
            description="Links (joins) datasets via SQL and fuzzy matching",
            llm_router=llm_router,
        )

        self._config = config or {}
        self._db_config = db_config or {}
        self.tools = DataLinkingTools()
        self.fuzzy_tools = FuzzyLinkingTools()

        # Embedding settings — overridable via config, fall back to env,
        # then to module defaults. The semantic strategy is the only
        # consumer today; other agents that import embed_texts directly
        # will use the same env vars.
        embed_cfg = (self._config.get("embedding") or {}) if isinstance(self._config, dict) else {}
        self._embedding_model: Optional[str] = (
            embed_cfg.get("model") or os.environ.get("OPENROUTER_EMBEDDING_MODEL") or None
        )
        self._embedding_dimensions: Optional[int] = None
        raw_dim = embed_cfg.get("dimensions") or os.environ.get("EMBEDDING_DIMENSIONS")
        if raw_dim is not None:
            try:
                self._embedding_dimensions = int(raw_dim)
            except (TypeError, ValueError):
                self._embedding_dimensions = None

        # HNSW row-count threshold (semantic strategy only)
        self._hnsw_min_rows = int(embed_cfg.get("hnsw_min_rows", 10_000))

        self.system_prompt = (
            "You are a data linking expert. You can:\n"
            "- Analyse table schemas and recommend JOIN strategies\n"
            "- Identify the best columns to join on based on data overlap\n"
            "- Choose the appropriate JOIN type (INNER, LEFT, RIGHT, FULL OUTER)\n"
            "- Recommend exact vs fuzzy matching strategies\n"
            "- Choose fuzzy algorithms: levenshtein, jaro_winkler, token_sort,\n"
            "  token_set, partial_ratio, cosine_tfidf, soundex, metaphone\n"
            "- Detect potential data quality issues that could affect joins\n"
            "- Explain the rationale behind each recommendation\n\n"
            "When deciding between exact and fuzzy matching, consider:\n"
            "- Exact overlap percentage (high → exact; low → fuzzy)\n"
            "- Column content type (IDs → exact; names/addresses → fuzzy)\n"
            "- Average token count (single word → levenshtein/phonetic;\n"
            "  multi-word → token_set/cosine_tfidf)\n"
            "Always explain your reasoning and provide the SQL or Python code."
        )

    # ------------------------------------------------------------------
    # Connection + strategy-kwargs helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        """Open a Postgres connection using the agent's db config."""
        return get_connection(self._db_config)

    def _strategy_kwargs(self, strategy: Optional[str]) -> Dict[str, Any]:
        """
        Build the per-strategy kwargs dict passed through to
        FuzzyLinkingTools. Currently only the `semantic` strategy
        consumes anything; other strategies ignore extras.
        """
        kwargs: Dict[str, Any] = {}
        if strategy in _DB_AWARE_STRATEGIES:
            kwargs["db_config"] = self._db_config
            if self._embedding_model is not None:
                kwargs["embedding_model"] = self._embedding_model
            if self._embedding_dimensions is not None:
                kwargs["embedding_dimensions"] = self._embedding_dimensions
            kwargs["hnsw_min_rows"] = self._hnsw_min_rows
        return kwargs

    # ==================================================================
    # Public API: recommend join strategy (LLM-powered)
    # ==================================================================

    async def recommend_join(
        self,
        left_data: Any,
        right_data: Any,
        left_name: str = "left_table",
        right_name: str = "right_table",
        left_fmt: Optional[str] = None,
        right_fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyse two datasets and recommend the best join strategy.

        Loads both datasets into Postgres temp tables, gathers schema /
        overlap stats, then asks the LLM for a recommendation.
        """
        conn = None
        try:
            left_df = parse_input_to_dataframe(left_data, fmt=left_fmt)
            right_df = parse_input_to_dataframe(right_data, fmt=right_fmt)

            validation = self._validate_inputs(left_df, right_df)
            if validation:
                return validation

            conn = self._get_conn()

            # Load into temp tables
            left_meta = self.tools.load_to_temp_table(conn, left_df, left_name)
            if "error" in left_meta:
                return left_meta
            right_meta = self.tools.load_to_temp_table(conn, right_df, right_name)
            if "error" in right_meta:
                return right_meta

            left_schema = self.tools.get_table_schema(conn, left_name)
            right_schema = self.tools.get_table_schema(conn, right_name)

            # Find candidate key pairs (columns with the same name)
            candidate_pairs = self._find_candidate_keys(left_schema, right_schema)

            # Gather quality metrics for each candidate pair
            quality_reports: List[Dict[str, Any]] = []
            for pair in candidate_pairs:
                report = self.tools.analyze_join_quality(
                    conn, left_name, right_name, pair["left"], pair["right"]
                )
                if "error" not in report:
                    quality_reports.append({"keys": pair, "quality": report})

            # Ask LLM for recommendation (now includes fuzzy analysis)
            # Gather fuzzy quality analysis for candidate pairs
            fuzzy_analyses: List[Dict[str, Any]] = []
            strategy_comparisons: List[Dict[str, Any]] = []
            for pair in candidate_pairs:
                fa = self.fuzzy_tools.analyze_fuzzy_quality(
                    left_df, right_df, pair["left"], pair["right"]
                )
                if "error" not in fa:
                    fuzzy_analyses.append({"keys": pair, "analysis": fa})
                    # Only run comparison if exact overlap is low
                    if fa.get("exact_overlap_pct_left", 100) < 80:
                        sc = self.fuzzy_tools.compare_strategies(
                            left_df,
                            right_df,
                            pair["left"],
                            pair["right"],
                            strategies=_CURRENT_PARITY_STRATEGIES,
                            threshold=0.8,
                            sample_size=50,
                            **self._strategy_kwargs("semantic"),
                        )
                        if "error" not in sc:
                            strategy_comparisons.append({"keys": pair, "comparison": sc})

            recommendation = await self._llm_recommend(
                left_schema,
                right_schema,
                quality_reports,
                fuzzy_analysis=fuzzy_analyses if fuzzy_analyses else None,
                strategy_comparison=(strategy_comparisons if strategy_comparisons else None),
            )

            return {
                "left_table": left_meta,
                "right_table": right_meta,
                "candidate_keys": candidate_pairs,
                "quality_reports": quality_reports,
                "fuzzy_analyses": fuzzy_analyses,
                "strategy_comparisons": strategy_comparisons,
                "recommendation": recommendation,
            }

        except Exception as e:
            logger.error("recommend_join failed: %s", e)
            return {"error": str(e)}
        finally:
            if conn:
                conn.close()

    # ==================================================================
    # Public API: execute an explicit join
    # ==================================================================

    async def join_data(
        self,
        left_data: Any,
        right_data: Any,
        join_type: str = "INNER",
        left_on: Optional[str] = None,
        right_on: Optional[str] = None,
        join_keys: Optional[List[Dict[str, str]]] = None,
        left_name: str = "left_table",
        right_name: str = "right_table",
        left_fmt: Optional[str] = None,
        right_fmt: Optional[str] = None,
        selected_columns: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Join two datasets.

        Caller may supply a single key pair (left_on / right_on) or
        multiple pairs via *join_keys*.  If no keys are given the agent
        will auto-detect matching column names.
        """
        conn = None
        try:
            left_df = parse_input_to_dataframe(left_data, fmt=left_fmt)
            right_df = parse_input_to_dataframe(right_data, fmt=right_fmt)

            validation = self._validate_inputs(left_df, right_df)
            if validation:
                return validation

            conn = self._get_conn()

            # Load into temp tables
            left_meta = self.tools.load_to_temp_table(conn, left_df, left_name)
            if "error" in left_meta:
                return left_meta
            right_meta = self.tools.load_to_temp_table(conn, right_df, right_name)
            if "error" in right_meta:
                return right_meta

            # Track the resolved join keys in every branch so we never
            # reference an undefined `pairs` variable.
            key_left: Optional[str] = None
            key_right: Optional[str] = None

            # Resolve keys
            if join_keys:
                result = self.tools.execute_join_multi_key(
                    conn,
                    left_name,
                    right_name,
                    join_type,
                    join_keys,
                    selected_columns,
                )
                key_left = join_keys[0]["left"]
                key_right = join_keys[0]["right"]
            elif left_on and right_on:
                result = self.tools.execute_join(
                    conn,
                    left_name,
                    right_name,
                    join_type,
                    left_on,
                    right_on,
                    selected_columns,
                )
                key_left = left_on
                key_right = right_on
            else:
                # Auto-detect: columns with same name
                left_schema = self.tools.get_table_schema(conn, left_name)
                right_schema = self.tools.get_table_schema(conn, right_name)
                pairs = self._find_candidate_keys(left_schema, right_schema)
                if not pairs:
                    return {
                        "error": (
                            "No matching column names found between datasets. "
                            "Please specify left_on/right_on or join_keys."
                        )
                    }
                key_left = pairs[0]["left"]
                key_right = pairs[0]["right"]
                # Use the first matching pair
                result = self.tools.execute_join(
                    conn,
                    left_name,
                    right_name,
                    join_type,
                    key_left,
                    key_right,
                    selected_columns,
                )

            if "error" in result:
                return result

            # Attach quality report and reproducible code
            quality = self.tools.analyze_join_quality(
                conn,
                left_name,
                right_name,
                key_left,
                key_right,
            )
            code = self.tools.generate_join_code(
                left_name,
                right_name,
                join_type,
                key_left,
                key_right,
            )

            result["quality"] = quality
            result["code"] = code
            result["original_shapes"] = {
                "left": list(left_df.shape),
                "right": list(right_df.shape),
            }

            return result

        except Exception as e:
            logger.error("join_data failed: %s", e)
            return {"error": str(e)}
        finally:
            if conn:
                conn.close()

    # ==================================================================
    # Public: quick quality preview without running the actual join
    # ==================================================================

    async def preview_join(
        self,
        left_data: Any,
        right_data: Any,
        left_on: str,
        right_on: str,
        left_name: str = "left_table",
        right_name: str = "right_table",
        left_fmt: Optional[str] = None,
        right_fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return join quality metrics **without** materialising the join.
        Useful for validating a join strategy before committing.
        """
        conn = None
        try:
            left_df = parse_input_to_dataframe(left_data, fmt=left_fmt)
            right_df = parse_input_to_dataframe(right_data, fmt=right_fmt)

            validation = self._validate_inputs(left_df, right_df)
            if validation:
                return validation

            conn = self._get_conn()

            self.tools.load_to_temp_table(conn, left_df, left_name)
            self.tools.load_to_temp_table(conn, right_df, right_name)

            quality = self.tools.analyze_join_quality(
                conn,
                left_name,
                right_name,
                left_on,
                right_on,
            )

            return quality

        except Exception as e:
            logger.error("preview_join failed: %s", e)
            return {"error": str(e)}
        finally:
            if conn:
                conn.close()

    # ==================================================================
    # Public API: fuzzy / similarity-based join
    # ==================================================================

    async def fuzzy_join_data(
        self,
        left_data: Any,
        right_data: Any,
        left_on: str,
        right_on: str,
        strategy: str = "levenshtein",
        threshold: float = 0.8,
        max_matches: int = 1,
        join_type: str = "inner",
        left_fmt: Optional[str] = None,
        right_fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Join two datasets using fuzzy / similarity matching.

        Strategies: levenshtein, jaro_winkler, token_sort, token_set,
        partial_ratio, cosine_tfidf, soundex, metaphone, semantic.
        """
        try:
            left_df = parse_input_to_dataframe(left_data, fmt=left_fmt)
            right_df = parse_input_to_dataframe(right_data, fmt=right_fmt)

            validation = self._validate_inputs(left_df, right_df)
            if validation:
                return validation

            result = self.fuzzy_tools.fuzzy_join(
                left_df,
                right_df,
                left_on=left_on,
                right_on=right_on,
                strategy=strategy,
                threshold=threshold,
                max_matches=max_matches,
                join_type=join_type,
                **self._strategy_kwargs(strategy),
            )

            if "error" in result:
                return result

            result["original_shapes"] = {
                "left": list(left_df.shape),
                "right": list(right_df.shape),
            }
            # Inspect payload lives on the result; callers (session join) persist it.
            result["inspect"] = build_inspect_store(
                left_df,
                right_df,
                left_name="left",
                right_name="right",
                left_on=left_on,
                right_on=right_on,
                strategy=strategy,
                threshold=threshold,
                join_type=join_type,
                candidates=collect_candidates(
                    left_df,
                    right_df,
                    left_on,
                    right_on,
                    strategy,
                    threshold=threshold,
                    strategy_kwargs=self._strategy_kwargs("semantic"),
                    existing_candidates=result.get("candidates"),
                ),
                committed=True,
            )
            return result

        except Exception as e:
            logger.error("fuzzy_join_data failed: %s", e)
            return {"error": str(e)}

    # ==================================================================
    # Public API: compare fuzzy strategies
    # ==================================================================

    async def compare_strategies(
        self,
        left_data: Any,
        right_data: Any,
        left_on: str,
        right_on: str,
        strategies: Optional[List[str]] = None,
        threshold: float = 0.8,
        sample_size: int = 100,
        left_fmt: Optional[str] = None,
        right_fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run multiple fuzzy strategies on a sample and compare results.
        Helps the caller pick the best strategy before a full run.
        """
        try:
            left_df = parse_input_to_dataframe(left_data, fmt=left_fmt)
            right_df = parse_input_to_dataframe(right_data, fmt=right_fmt)

            validation = self._validate_inputs(left_df, right_df)
            if validation:
                return validation

            return self.fuzzy_tools.compare_strategies(
                left_df,
                right_df,
                left_on=left_on,
                right_on=right_on,
                strategies=strategies or _CURRENT_PARITY_STRATEGIES,
                threshold=threshold,
                sample_size=sample_size,
                **self._strategy_kwargs("semantic"),
            )

        except Exception as e:
            logger.error("compare_strategies failed: %s", e)
            return {"error": str(e)}

    # ==================================================================
    # Public API: multi-dataset join (N datasets, chained or explicit)
    # ==================================================================

    async def multi_join(
        self,
        datasets: List[Dict[str, Any]],
        join_plan: Optional[List[Dict[str, Any]]] = None,
        default_join_type: str = "INNER",
        match_strategy: str = "exact",
        fuzzy_threshold: float = 0.8,
    ) -> Dict[str, Any]:
        """
        Join N datasets together.

        Parameters
        ----------
        datasets : list of dataset descriptors, each containing:
            - "data": the actual data (any supported format)
            - "name": optional label (defaults to "table_0", "table_1", ...)
            - "format": optional format hint ("csv", "json", "tsv", etc.)

        join_plan : optional list of explicit join steps, each containing:
            - "left": index or name of left dataset
            - "right": index or name of right dataset
            - "left_on": join key column in left
            - "right_on": join key column in right
            - "join_type": optional (defaults to default_join_type)
            - "strategy": optional fuzzy strategy override
            - "threshold": optional fuzzy threshold override

            If join_plan is None, datasets are chained sequentially:
            (0 JOIN 1) → (result JOIN 2) → (result JOIN 3) → ...
            with auto-detected keys.

        match_strategy : "exact" for SQL joins, or a current lexical strategy
        fuzzy_threshold : default threshold for fuzzy joins

        Returns the final joined dataset plus a step-by-step log.
        """
        if len(datasets) < 2:
            return {"error": "At least 2 datasets are required"}

        try:
            # Parse all datasets
            parsed: List[Dict[str, Any]] = []
            for i, ds in enumerate(datasets):
                name = ds.get("name", f"table_{i}")
                fmt = ds.get("format")
                data = ds.get("data")
                if data is None:
                    return {"error": f"Dataset '{name}' has no data"}

                df = parse_input_to_dataframe(data, fmt=fmt)
                err = self._validate_single(df, name)
                if err:
                    return err

                parsed.append(
                    {
                        "name": name,
                        "df": df,
                        "original_shape": list(df.shape),
                    }
                )

            # Build join plan if not provided
            if join_plan is None:
                join_plan = self._auto_join_plan(
                    parsed,
                    default_join_type,
                    match_strategy,
                )

            if not join_plan:
                return {"error": "join_plan is empty — at least one join step is required."}

            # Execute join steps
            step_log: List[Dict[str, Any]] = []
            # Name → DataFrame lookup (including intermediate results)
            df_lookup = {p["name"]: p["df"] for p in parsed}
            result_df: Optional[pd.DataFrame] = None
            first_step_candidates: Optional[List[Dict[str, Any]]] = None
            joined_sources: set[str] = set()

            def _source_name(ref: Any) -> Optional[str]:
                if isinstance(ref, int) and 0 <= ref < len(parsed):
                    return str(parsed[ref]["name"])
                if isinstance(ref, str) and ref in {p["name"] for p in parsed}:
                    return ref
                return None

            for step_idx, step in enumerate(join_plan):
                left_ref = step.get("left")
                if left_ref is None:
                    left_ref = parsed[0]["name"] if step_idx == 0 else f"_result_{step_idx - 1}"

                right_ref = step.get("right")
                if right_ref is None:
                    if step_idx + 1 < len(parsed):
                        right_ref = parsed[step_idx + 1]["name"]
                    else:
                        return {
                            "error": (
                                f"Step {step_idx}: no 'right' dataset specified "
                                f"and no more datasets available at index "
                                f"{step_idx + 1}."
                            )
                        }

                logical_left_ref, logical_right_ref = left_ref, right_ref
                left_name, right_name = _source_name(left_ref), _source_name(right_ref)
                if step_idx == 0:
                    if left_name is None or right_name is None:
                        return {"error": "Step 0 must connect two input datasets."}
                    joined_sources.update((left_name, right_name))
                else:
                    prior_result = f"_result_{step_idx - 1}"
                    left_joined = left_ref == prior_result or (
                        left_name is not None and left_name in joined_sources
                    )
                    right_joined = right_ref == prior_result or (
                        right_name is not None and right_name in joined_sources
                    )
                    left_new = left_name is not None and left_name not in joined_sources
                    right_new = right_name is not None and right_name not in joined_sources
                    if left_joined and right_new:
                        left_ref = prior_result
                        joined_sources.add(right_name)
                    elif right_joined and left_new:
                        right_ref = prior_result
                        joined_sources.add(left_name)
                    else:
                        return {
                            "error": (
                                f"Step {step_idx}: each step must add exactly one new dataset "
                                "to the prior result."
                            )
                        }

                # Resolve references (index or name)
                left_df = self._resolve_dataset(left_ref, parsed, df_lookup)
                right_df = self._resolve_dataset(right_ref, parsed, df_lookup)

                if left_df is None:
                    return {"error": f"Step {step_idx}: cannot resolve left dataset '{left_ref}'"}
                if right_df is None:
                    return {"error": f"Step {step_idx}: cannot resolve right dataset '{right_ref}'"}

                key_pairs = step.get("keys") or []
                left_on = step.get("left_on")
                right_on = step.get("right_on")
                if key_pairs:
                    left_on = [pair.get("left") for pair in key_pairs]
                    right_on = [pair.get("right") for pair in key_pairs]
                # Treat null/None join_type as the default (avoids AttributeError on .upper())
                jtype = step.get("join_type") or default_join_type
                strategy = step.get("strategy", match_strategy)
                threshold = step.get("threshold", fuzzy_threshold)

                if strategy != "exact" and (
                    isinstance(left_on, list) or isinstance(right_on, list)
                ):
                    if (
                        not isinstance(left_on, list)
                        or not isinstance(right_on, list)
                        or len(left_on) != 1
                        or len(right_on) != 1
                    ):
                        return {
                            "error": f"Step {step_idx}: lexical matching supports one key pair."
                        }
                    left_on, right_on = left_on[0], right_on[0]

                if not left_on or not right_on:
                    common = self._find_common_columns(left_df, right_df)
                    if common:
                        # _find_common_columns returns List[str] — same name on both sides.
                        left_on = common[0]
                        right_on = common[0]
                    else:
                        # Fallback: try matching base names after stripping
                        # the "_right" suffix that _merge_exact adds.
                        common_pairs = self._find_common_columns_fuzzy_suffix(
                            left_df,
                            right_df,
                        )
                        if not common_pairs:
                            return {
                                "error": (
                                    f"Step {step_idx}: no matching column names between "
                                    f"'{left_ref}' and '{right_ref}'. "
                                    "Specify left_on/right_on in join_plan."
                                )
                            }
                        # _find_common_columns_fuzzy_suffix returns List[tuple].
                        left_on = common_pairs[0][0]
                        right_on = common_pairs[0][1]

                # Execute the join
                if strategy == "exact":
                    result_df = self._merge_exact(
                        left_df,
                        right_df,
                        jtype,
                        left_on,
                        right_on,
                    )
                    step_info = {
                        "step": step_idx,
                        "left": str(logical_left_ref),
                        "right": str(logical_right_ref),
                        "strategy": "exact",
                        "join_type": jtype,
                        "left_on": left_on,
                        "right_on": right_on,
                        "result_rows": len(result_df),
                    }
                else:
                    _FUZZY_JOIN_TYPE_MAP = {
                        "LEFT": "left",
                        "INNER": "inner",
                        "RIGHT": "right",
                        "FULL OUTER": "outer",
                        "FULL": "outer",
                        "LEFT OUTER": "left",
                        "OUTER": "outer",
                    }
                    fuzzy_result = self.fuzzy_tools.fuzzy_join(
                        left_df,
                        right_df,
                        left_on=left_on,
                        right_on=right_on,
                        strategy=strategy,
                        threshold=threshold,
                        max_matches=1,
                        join_type=_FUZZY_JOIN_TYPE_MAP.get((jtype or "INNER").upper(), "inner"),
                        **self._strategy_kwargs(strategy),
                    )
                    if "error" in fuzzy_result:
                        return {"error": f"Step {step_idx}: {fuzzy_result['error']}"}
                    result_df = pd.DataFrame(fuzzy_result["data"])
                    if step_idx == 0:
                        first_step_candidates = fuzzy_result.get("candidates") or []
                    step_info = {
                        "step": step_idx,
                        "left": str(logical_left_ref),
                        "right": str(logical_right_ref),
                        "strategy": strategy,
                        "threshold": threshold,
                        "join_type": jtype,
                        "left_on": left_on,
                        "right_on": right_on,
                        "result_rows": len(result_df),
                        "match_rate": fuzzy_result.get("statistics", {}).get("match_rate"),
                        "avg_similarity": fuzzy_result.get("statistics", {}).get("avg_similarity"),
                    }

                step_log.append(step_info)
                # Store intermediate result for next step
                result_name = f"_result_{step_idx}"
                df_lookup[result_name] = result_df

            inspect_store = None
            if join_plan:
                inspect_store = self.build_inspect_store_for_plan(
                    parsed,
                    df_lookup,
                    join_plan,
                    default_join_type,
                    match_strategy,
                    fuzzy_threshold,
                    existing_candidates=first_step_candidates,
                )

            return {
                "data": result_df.to_dict(orient="list"),
                "rows": len(result_df),
                "columns": list(result_df.columns),
                "datasets_count": len(datasets),
                "steps_executed": len(step_log),
                "step_log": step_log,
                "original_shapes": {p["name"]: p["original_shape"] for p in parsed},
                "inspect": inspect_store,
            }

        except Exception as e:
            logger.error("multi_join failed: %s", e)
            return {"error": str(e)}

    # ==================================================================
    # Public API: multi-dataset recommend
    # ==================================================================

    async def recommend_multi_join(
        self,
        datasets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Analyse N datasets and recommend a join plan.

        Returns schemas, pairwise overlap analysis, fuzzy quality
        analysis, and an LLM-generated recommendation for the full
        join plan.
        """
        if len(datasets) < 2:
            return {"error": "At least 2 datasets are required"}

        try:
            parsed: List[Dict[str, Any]] = []
            for i, ds in enumerate(datasets):
                name = ds.get("name", f"table_{i}")
                fmt = ds.get("format")
                data = ds.get("data")
                if data is None:
                    return {"error": f"Dataset '{name}' has no data"}
                df = parse_input_to_dataframe(data, fmt=fmt)
                err = self._validate_single(df, name)
                if err:
                    return err
                parsed.append(
                    {
                        "name": name,
                        "df": df,
                        "columns": list(df.columns),
                        "rows": len(df),
                        "dtypes": {col: str(dt) for col, dt in df.dtypes.items()},
                    }
                )

            # Pairwise analysis for all dataset pairs
            pairwise: List[Dict[str, Any]] = []
            for i in range(len(parsed)):
                for j in range(i + 1, len(parsed)):
                    common = self._find_common_columns(parsed[i]["df"], parsed[j]["df"])
                    pair_info: Dict[str, Any] = {
                        "left": parsed[i]["name"],
                        "right": parsed[j]["name"],
                        "common_columns": common,
                    }

                    # Build candidate column pairs:
                    # 1) Same-name matches (exact)
                    same_name_pairs = [(c, c) for c in common[:3]]
                    # 2) Cross-name candidates via simple name-similarity heuristic
                    #    (catches "customer_name" ↔ "client_name", "id" ↔ "cust_id")
                    cross_pairs = self._find_cross_name_candidates(
                        parsed[i]["df"], parsed[j]["df"], limit=3
                    )
                    # Deduplicate: skip cross pairs already covered by same-name
                    seen = {(lc, rc) for lc, rc in same_name_pairs}
                    candidate_pairs = same_name_pairs + [
                        (lc, rc) for lc, rc in cross_pairs if (lc, rc) not in seen
                    ]

                    for left_col, right_col in candidate_pairs[:3]:
                        fa = self.fuzzy_tools.analyze_fuzzy_quality(
                            parsed[i]["df"],
                            parsed[j]["df"],
                            left_col,
                            right_col,
                        )
                        if "error" not in fa:
                            pair_info.setdefault("fuzzy_analyses", []).append(
                                {
                                    "left_col": left_col,
                                    "right_col": right_col,
                                    "analysis": fa,
                                }
                            )

                    pairwise.append(pair_info)

            # Build context for LLM
            schemas = [
                {
                    "name": p["name"],
                    "columns": p["columns"],
                    "rows": p["rows"],
                    "dtypes": p["dtypes"],
                    "sample": p["df"].head(3).to_dict(orient="records"),
                }
                for p in parsed
            ]

            recommendation = await self._llm_recommend_multi(schemas, pairwise)

            return {
                "datasets": [
                    {"name": p["name"], "rows": p["rows"], "columns": p["columns"]} for p in parsed
                ],
                "pairwise_analysis": pairwise,
                "recommendation": recommendation,
            }

        except Exception as e:
            logger.error("recommend_multi_join failed: %s", e)
            return {"error": str(e)}

    # ==================================================================
    # Public API: inspect candidate scores
    # ==================================================================

    def build_inspect_store_for_plan(
        self,
        parsed: List[Dict[str, Any]],
        df_lookup: Dict[str, pd.DataFrame],
        join_plan: List[Dict[str, Any]],
        default_join_type: str,
        match_strategy: str,
        fuzzy_threshold: float,
        existing_candidates: Optional[List[Dict[str, Any]]] = None,
        committed: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Score the first join step and return a persistable inspect store."""
        if not join_plan or not parsed:
            return None
        step = join_plan[0]
        left_ref = step.get("left", parsed[0]["name"])
        right_ref = step.get("right", parsed[1]["name"] if len(parsed) > 1 else None)
        if right_ref is None:
            return None
        left_df = self._resolve_dataset(left_ref, parsed, df_lookup)
        right_df = self._resolve_dataset(right_ref, parsed, df_lookup)
        if left_df is None or right_df is None:
            return None
        left_on = step.get("left_on")
        right_on = step.get("right_on")
        if not left_on or not right_on:
            common = self._find_common_columns(left_df, right_df)
            if common:
                # _find_common_columns returns matching names (same on both sides)
                left_on = common[0] if isinstance(common[0], str) else common[0][0]
                right_on = common[0] if isinstance(common[0], str) else common[0][1]
            else:
                return None
        strategy = step.get("strategy", match_strategy) or "exact"
        threshold = float(step.get("threshold", fuzzy_threshold) or 0.8)
        jtype = step.get("join_type", default_join_type) or "INNER"
        left_name = str(left_ref)
        right_name = str(right_ref)
        candidates = collect_candidates(
            left_df,
            right_df,
            left_on,
            right_on,
            strategy,
            threshold=threshold,
            top_n=CANDIDATE_TOP_N,
            floor=CANDIDATE_FLOOR,
            strategy_kwargs=self._strategy_kwargs("semantic"),
            existing_candidates=existing_candidates,
        )
        return build_inspect_store(
            left_df,
            right_df,
            left_name=left_name,
            right_name=right_name,
            left_on=left_on,
            right_on=right_on,
            strategy=strategy,
            threshold=threshold,
            join_type=jtype,
            candidates=candidates,
            committed=committed,
        )

    def preview_inspect(
        self,
        datasets: List[Dict[str, Any]],
        join_plan: List[Dict[str, Any]],
        default_join_type: str = "INNER",
        match_strategy: str = "exact",
        fuzzy_threshold: float = 0.8,
    ) -> Dict[str, Any]:
        """
        Score candidates for a join plan without mutating caller DataFrames
        or materialising the joined result.
        """
        parsed: List[Dict[str, Any]] = []
        for i, ds in enumerate(datasets):
            name = ds.get("name", f"table_{i}")
            fmt = ds.get("format")
            data = ds.get("data")
            if data is None:
                return {"error": f"Dataset '{name}' has no data"}
            if isinstance(data, pd.DataFrame):
                df = data
            else:
                df = parse_input_to_dataframe(data, fmt=fmt)
            err = self._validate_single(df, name)
            if err:
                return err
            parsed.append({"name": name, "df": df, "original_shape": list(df.shape)})
        df_lookup = {p["name"]: p["df"] for p in parsed}
        store = self.build_inspect_store_for_plan(
            parsed,
            df_lookup,
            join_plan,
            default_join_type,
            match_strategy,
            fuzzy_threshold,
            committed=False,
        )
        if store is None:
            return {"error": "Could not build inspect payload for this join plan"}
        return {"inspect": store, "context": public_context(store)}

    @staticmethod
    def inspect_row(
        store: Dict[str, Any],
        source_row_id: int,
        strategy: Optional[str] = None,
        threshold: Optional[float] = None,
        top_n: int = CANDIDATE_TOP_N,
    ) -> Dict[str, Any]:
        return inspect_source_row(
            store,
            source_row_id,
            strategy=strategy,
            threshold=threshold,
            top_n=top_n,
        )

    # ==================================================================
    # Internal helpers
    # ==================================================================

    @staticmethod
    def _validate_single(df: pd.DataFrame, label: str) -> Optional[Dict[str, Any]]:
        """Validate a single DataFrame."""
        if df is None:
            return {"error": f"Dataset '{label}' is None"}
        if not isinstance(df, pd.DataFrame):
            return {"error": f"Dataset '{label}' is not a DataFrame"}
        if df.empty:
            return {"error": f"Dataset '{label}' is empty"}
        return None

    @staticmethod
    def _validate_inputs(left_df: pd.DataFrame, right_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """Validate both DataFrames. Returns error dict or None."""
        for label, df in [("left", left_df), ("right", right_df)]:
            if df is None:
                return {"error": f"{label} dataset is None"}
            if not isinstance(df, pd.DataFrame):
                return {"error": f"{label} dataset is not a DataFrame"}
            if df.empty:
                return {"error": f"{label} dataset is empty"}
        return None

    @staticmethod
    def _find_candidate_keys(
        left_schema: Dict[str, Any],
        right_schema: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """
        Return column pairs that share the same name (case-insensitive).
        These are the most likely join keys.
        """
        left_cols = {
            c["column_name"].lower(): c["column_name"] for c in left_schema.get("columns", [])
        }
        right_cols = {
            c["column_name"].lower(): c["column_name"] for c in right_schema.get("columns", [])
        }
        common = set(left_cols.keys()) & set(right_cols.keys())
        return [{"left": left_cols[k], "right": right_cols[k]} for k in sorted(common)]

    # ----- LLM integration ---------------------------------------------

    async def _llm_recommend(
        self,
        left_schema: Dict[str, Any],
        right_schema: Dict[str, Any],
        quality_reports: List[Dict[str, Any]],
        fuzzy_analysis: Optional[Dict[str, Any]] = None,
        strategy_comparison: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Ask the LLM to recommend a join strategy based on schema,
        quality, and fuzzy analysis information.
        """
        fuzzy_section = ""
        if fuzzy_analysis:
            fuzzy_section += f"\n\nFuzzy quality analysis:\n{_clip_json(fuzzy_analysis)}\n"
        if strategy_comparison:
            fuzzy_section += (
                f"\n\nFuzzy strategy comparison results:\n{_clip_json(strategy_comparison)}\n"
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    "I need to join two tables. Analyse the schemas and quality "
                    "metrics below and recommend:\n"
                    "1. Whether to use exact SQL join or fuzzy matching\n"
                    "2. Which columns to join on\n"
                    "3. Which JOIN type or fuzzy strategy to use\n"
                    "4. The recommended threshold (if fuzzy)\n"
                    "5. Any data quality issues to watch out for\n"
                    "6. The SQL query or Python code to execute\n\n"
                    f"Left table schema:\n{_clip_json(left_schema)}\n\n"
                    f"Right table schema:\n{_clip_json(right_schema)}\n\n"
                    f"Quality reports for candidate key pairs:\n"
                    f"{_clip_json(quality_reports)}"
                    f"{fuzzy_section}\n\n"
                    "Available fuzzy strategies: "
                    f"{', '.join(_CURRENT_PARITY_STRATEGIES)}\n\n"
                    "Provide a clear, actionable recommendation."
                ),
            },
        ]

        try:
            return await self.generate_response(messages)
        except Exception as e:
            logger.warning("LLM recommendation failed: %s", e)
            return "LLM recommendation unavailable. " "Use the quality_reports to decide manually."

    async def _llm_select_join_strategy(
        self,
        quality_reports: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Structured LLM decision: returns a dict with recommended
        join_type, left_on, right_on.
        Falls back to empty dict on failure.
        """
        prompt = (
            "Given the following join quality reports, select the best "
            "join strategy. Return ONLY a JSON object with keys: "
            '"join_type", "left_on", "right_on", "reasoning".\n\n'
            f"Quality reports:\n{_clip_json(quality_reports)}"
        )
        return await self._llm_decide(prompt)

    async def _llm_recommend_multi(
        self,
        schemas: List[Dict[str, Any]],
        pairwise: List[Dict[str, Any]],
    ) -> str:
        """
        Ask the LLM to recommend a join plan for N datasets.
        """
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"I have {len(schemas)} datasets to link together. "
                    "Analyse the schemas and pairwise overlap below and recommend:\n"
                    "1. The order in which to join the datasets\n"
                    "2. For each join step: which columns to join on\n"
                    "3. Whether to use exact or fuzzy matching for each step\n"
                    "4. Which fuzzy strategy and threshold (if applicable)\n"
                    "5. Any data quality issues to watch out for\n\n"
                    f"Dataset schemas:\n{_clip_json(schemas)}\n\n"
                    f"Pairwise analysis:\n{_clip_json(pairwise)}\n\n"
                    "Available fuzzy strategies: "
                    f"{', '.join(_CURRENT_PARITY_STRATEGIES)}\n\n"
                    "Provide a step-by-step join plan."
                ),
            },
        ]

        try:
            return await self.generate_response(messages)
        except Exception as e:
            logger.warning("LLM multi-recommend failed: %s", e)
            return "LLM recommendation unavailable."

    # ----- Multi-join helpers -------------------------------------------

    @staticmethod
    def _find_common_columns(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
    ) -> List[str]:
        """Find columns with the same name (case-insensitive) between two DataFrames."""
        left_lower = {c.lower(): c for c in left_df.columns}
        right_lower = {c.lower(): c for c in right_df.columns}
        common = set(left_lower.keys()) & set(right_lower.keys())
        return sorted(left_lower[k] for k in common)

    @staticmethod
    def _find_common_columns_fuzzy_suffix(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
    ) -> List[tuple]:
        """
        Find column pairs whose base names match after stripping the
        '_right' suffix that _merge_exact adds.

        Returns a list of (left_col, right_col) tuples.
        """

        def _base_name(col: str) -> str:
            lower = col.lower()
            if lower.endswith("_right"):
                return lower[: -len("_right")]
            return lower

        left_bases = {}
        for c in left_df.columns:
            base = _base_name(c)
            left_bases.setdefault(base, []).append(c)

        right_bases = {}
        for c in right_df.columns:
            base = _base_name(c)
            right_bases.setdefault(base, []).append(c)

        common_bases = set(left_bases.keys()) & set(right_bases.keys())
        pairs = []
        for base in sorted(common_bases):
            pairs.append((left_bases[base][0], right_bases[base][0]))
        return pairs

    @staticmethod
    def _find_cross_name_candidates(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        limit: int = 3,
    ) -> List[tuple]:
        """
        Find candidate column pairs across two DataFrames where the names differ
        but share common tokens or substrings (e.g. "customer_name" ↔ "client_name").

        Returns up to *limit* (left_col, right_col) pairs ranked by name similarity.
        Pairs that already share the same normalized name are excluded (they are
        returned by _find_common_columns instead).
        """
        from difflib import SequenceMatcher

        def _norm(name: str) -> str:
            return name.lower().replace("_", "").replace(" ", "")

        left_cols = list(left_df.columns)
        right_cols = list(right_df.columns)

        candidates = []
        for lc in left_cols:
            for rc in right_cols:
                ln, rn = _norm(lc), _norm(rc)
                if ln == rn:
                    continue  # same-name pair — skip
                ratio = SequenceMatcher(None, ln, rn).ratio()
                if ratio >= 0.5:  # at least 50 % character overlap
                    candidates.append((ratio, lc, rc))

        candidates.sort(key=lambda x: -x[0])
        return [(lc, rc) for _, lc, rc in candidates[:limit]]

    @staticmethod
    def _resolve_dataset(
        ref: Any,
        parsed: List[Dict[str, Any]],
        df_lookup: Dict[str, pd.DataFrame],
    ) -> Optional[pd.DataFrame]:
        """Resolve a dataset reference (index or name) to a DataFrame."""
        # By name (including intermediate results like _result_0)
        if isinstance(ref, str) and ref in df_lookup:
            return df_lookup[ref]
        # By index
        if isinstance(ref, int) and 0 <= ref < len(parsed):
            return parsed[ref]["df"]
        # By name matching parsed list
        if isinstance(ref, str):
            for p in parsed:
                if p["name"] == ref:
                    return p["df"]
        return None

    @staticmethod
    def _auto_join_plan(
        parsed: List[Dict[str, Any]],
        default_join_type: str,
        match_strategy: str,
    ) -> List[Dict[str, Any]]:
        """
        Generate a sequential join plan: 0+1 → result+2 → result+3 ...
        Auto-detects common columns for each step.
        """
        plan: List[Dict[str, Any]] = []
        for i in range(1, len(parsed)):
            left_ref = parsed[0]["name"] if i == 1 else f"_result_{i - 2}"
            right_ref = parsed[i]["name"]
            plan.append(
                {
                    "left": left_ref,
                    "right": right_ref,
                    "join_type": default_join_type,
                    "strategy": match_strategy,
                }
            )
        return plan

    @staticmethod
    def _merge_exact(
        left_df: pd.DataFrame,
        right_df: pd.DataFrame,
        join_type: str,
        left_on: str | List[str],
        right_on: str | List[str],
    ) -> pd.DataFrame:
        """Perform an exact pandas merge."""
        how_map = {
            "INNER": "inner",
            "LEFT": "left",
            "RIGHT": "right",
            "FULL OUTER": "outer",
            "FULL": "outer",
            "LEFT OUTER": "left",
            "OUTER": "outer",
            "CROSS": "cross",
        }
        # Guard against None/null coming from LLM-generated join plans.
        how = how_map.get((join_type or "INNER").upper(), "inner")
        if how == "cross":
            return left_df.merge(right_df, how=how, suffixes=("", "_right"))

        # pandas treats null merge keys as equal, unlike SQL and the source
        # engines. Join only non-null keys, then add null-key rows back for the
        # preserving sides of an outer join.
        left_present = left_df[left_on].notna()
        right_present = right_df[right_on].notna()
        if isinstance(left_present, pd.DataFrame):
            left_present = left_present.all(axis=1)
        if isinstance(right_present, pd.DataFrame):
            right_present = right_present.all(axis=1)
        merge_kwargs = {
            "left_on": left_on,
            "right_on": right_on,
            "suffixes": ("", "_right"),
        }
        result = left_df.loc[left_present].merge(
            right_df.loc[right_present], how=how, **merge_kwargs
        )
        pieces = [result]
        if how in {"left", "outer"} and (~left_present).any():
            pieces.append(
                left_df.loc[~left_present].merge(right_df.iloc[0:0], how="left", **merge_kwargs)
            )
        if how in {"right", "outer"} and (~right_present).any():
            pieces.append(
                left_df.iloc[0:0].merge(right_df.loc[~right_present], how="right", **merge_kwargs)
            )
        return pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else result
