"""Static registry and honest capability manifests for shipped connectors."""

from __future__ import annotations

from typing import Any, Dict

from .contracts import (
    Capability,
    CapabilitySupport,
    ConnectorManifest,
    ConnectorMaturity,
    DataFreshness,
    DataScope,
    Executor,
    JoinStrategySupport,
    SameSourceJoinCapability,
    SupportMode,
    VerificationStatus,
)


def _no(reason: str = "not_implemented") -> CapabilitySupport:
    return CapabilitySupport(mode=SupportMode.UNSUPPORTED, reason_code=reason)


def _yes(
    executor: Executor,
    *,
    mode: SupportMode = SupportMode.NATIVE,
    scope: DataScope = DataScope.FULL,
    freshness: DataFreshness = DataFreshness.LIVE,
    limits: dict[str, Any] | None = None,
    caveats: tuple[str, ...] = (),
    join: SameSourceJoinCapability | None = None,
) -> CapabilitySupport:
    return CapabilitySupport(
        mode=mode,
        scope=scope,
        freshness=freshness,
        executor=executor,
        limits=limits or {},
        caveats=caveats,
        join=join,
    )


def _capabilities(**overrides: CapabilitySupport) -> Dict[Capability, CapabilitySupport]:
    result = {capability: _no() for capability in Capability}
    for name, support in overrides.items():
        result[Capability(name)] = support
    return result


def _duckdb(
    *, mode: SupportMode = SupportMode.NATIVE, caveats: tuple[str, ...] = ()
) -> CapabilitySupport:
    return _yes(Executor.DUCKDB, mode=mode, freshness=DataFreshness.SNAPSHOT, caveats=caveats)


def _pandas(
    *, mode: SupportMode = SupportMode.NATIVE, caveats: tuple[str, ...] = ()
) -> CapabilitySupport:
    return _yes(Executor.PANDAS, mode=mode, freshness=DataFreshness.SNAPSHOT, caveats=caveats)


def _source(
    *, mode: SupportMode = SupportMode.NATIVE, caveats: tuple[str, ...] = ()
) -> CapabilitySupport:
    return _yes(Executor.SOURCE, mode=mode, caveats=caveats)


def _sample(
    *, executor: Executor = Executor.DUCKDB, caveats: tuple[str, ...] = ()
) -> CapabilitySupport:
    return _yes(
        executor,
        mode=SupportMode.EMULATED,
        scope=DataScope.SAMPLE,
        freshness=DataFreshness.SNAPSHOT,
        limits={"rows": 10_000},
        caveats=caveats,
    )


_JOIN_TYPES = ("INNER", "LEFT", "RIGHT", "FULL OUTER")
_LEXICAL_JOIN_STRATEGIES = (
    "levenshtein",
    "jaro_winkler",
    "token_sort",
    "token_set",
    "partial_ratio",
    "cosine_tfidf",
    "soundex",
    "metaphone",
)


def _join_capability(
    *,
    source_side: bool,
    top_level_executor: Executor,
    top_level_mode: SupportMode,
    top_level_freshness: DataFreshness,
    top_level_caveats: tuple[str, ...],
    exact_caveats: tuple[str, ...] = (),
) -> CapabilitySupport:
    """Declare the full current join matrix without hiding execution paths."""

    if source_side:
        exact = JoinStrategySupport(
            strategy="exact",
            mode=SupportMode.NATIVE,
            scope=DataScope.FULL,
            freshness=DataFreshness.LIVE,
            executor=Executor.SOURCE,
            egress="preview_only",
            result_mode="source_scratch",
            caveats=(
                "Full-data equality join executes in the source engine; only the result "
                "preview leaves the source; the complete result stays in Stych scratch.",
                *exact_caveats,
            ),
        )
        lexical = tuple(
            JoinStrategySupport(
                strategy=strategy,
                mode=SupportMode.EMULATED,
                scope=DataScope.FULL,
                freshness=DataFreshness.SNAPSHOT,
                executor=Executor.PANDAS,
                egress="full",
                result_mode="pandas_materialized",
                caveats=(
                    "The full source tables are streamed to Stych for matching; the loaded "
                    "preview is never used as a silent fallback.",
                    "Execution is subject to the full-transfer budget and approval for "
                    "over-budget inputs.",
                ),
            )
            for strategy in _LEXICAL_JOIN_STRATEGIES
        )
    else:
        exact = JoinStrategySupport(
            strategy="exact",
            mode=SupportMode.EMULATED,
            scope=DataScope.FULL,
            freshness=DataFreshness.SNAPSHOT,
            executor=Executor.DUCKDB,
            egress="none",
            result_mode="duckdb_snapshot",
            caveats=("The complete uploaded snapshot is joined locally.",),
        )
        lexical = tuple(
            JoinStrategySupport(
                strategy=strategy,
                mode=SupportMode.EMULATED,
                scope=DataScope.FULL,
                freshness=DataFreshness.SNAPSHOT,
                executor=Executor.PANDAS,
                egress="none",
                result_mode="pandas_materialized",
                caveats=("The complete uploaded snapshot is matched locally.",),
            )
            for strategy in _LEXICAL_JOIN_STRATEGIES
        )

    return _yes(
        top_level_executor,
        mode=top_level_mode,
        scope=DataScope.FULL,
        freshness=top_level_freshness,
        caveats=top_level_caveats,
        join=SameSourceJoinCapability(
            strategies=(exact, *lexical),
            join_types=_JOIN_TYPES,
        ),
    )


SOURCE_WORKING_COPY_IDS = frozenset(
    {
        "postgres",
        "databricks",
        "bigquery",
        "snowflake",
        "redshift",
        "sqlserver",
        "mongodb",
    }
)


UPLOAD = ConnectorManifest(
    id="upload",
    display_name="File upload",
    category="file",
    version="1",
    maturity=ConnectorMaturity.PRODUCTION,
    verification=VerificationStatus.CONTRACT_TESTED,
    user_summary="Snapshot · Full-data analysis",
    capabilities=_capabilities(
        describe_schema=_duckdb(),
        preview_page=_duckdb(),
        exact_count=_duckdb(),
        column_stats=_duckdb(),
        full_scan=_duckdb(),
        projection_pushdown=_duckdb(mode=SupportMode.EMULATED),
        predicate_pushdown=_duckdb(mode=SupportMode.EMULATED),
        sort_pushdown=_duckdb(mode=SupportMode.EMULATED),
        limit_pushdown=_duckdb(mode=SupportMode.EMULATED),
        aggregation=_duckdb(mode=SupportMode.EMULATED),
        same_source_join=_join_capability(
            source_side=False,
            top_level_executor=Executor.DUCKDB,
            top_level_mode=SupportMode.EMULATED,
            top_level_freshness=DataFreshness.SNAPSHOT,
            top_level_caveats=(
                "All current exact and lexical strategies run over the complete uploaded snapshot.",
            ),
        ),
        create_working_copy=_pandas(mode=SupportMode.EMULATED),
        mutate_working_copy=_pandas(mode=SupportMode.EMULATED),
        export_snapshot=_pandas(),
        stable_row_identity=_duckdb(mode=SupportMode.EMULATED),
        schema_is_complete=_duckdb(),
    ),
)


POSTGRES = ConnectorManifest(
    id="postgres",
    display_name="PostgreSQL",
    category="database",
    version="1",
    maturity=ConnectorMaturity.PRODUCTION,
    verification=VerificationStatus.INTEGRATION_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(),
        preview_page=_source(),
        exact_count=_source(),
        column_stats=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        create_working_copy=_source(
            caveats=("Working copies live in a Stych-owned scratch schema (stych_temp).",)
        ),
        mutate_working_copy=_source(
            caveats=("The source table is never updated; DML is confined to the scratch schema.",)
        ),
        stream_arrow=_source(),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=("Row identity is synthesized when no key is selected.",),
        ),
        nested_values=_source(),
        schema_is_complete=_source(),
    ),
)


SQLSERVER = ConnectorManifest(
    id="sqlserver",
    display_name="SQL Server",
    category="database",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.CONTRACT_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(
            caveats=("Custom-query schemas are inferred from the loaded sample.",)
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        stream_arrow=_source(),
        create_working_copy=_source(
            caveats=("Working copies live in a Stych-owned scratch schema (default stych_temp).",)
        ),
        mutate_working_copy=_source(
            caveats=("The source table is never updated; DML is confined to the scratch schema.",)
        ),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=(
                "Working copies are stamped with a row id, so diffing a copy against the "
                "pinned snapshot of the source is exact. Comparing against the live source "
                "table to detect upstream drift needs a key column.",
            ),
        ),
        schema_is_complete=_source(),
    ),
)


MONGODB = ConnectorManifest(
    id="mongodb",
    display_name="MongoDB",
    category="document_database",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.INTEGRATION_TESTED,
    user_summary="Live · Full-data analysis (sampled schema)",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_yes(
            executor=Executor.SOURCE,
            mode=SupportMode.EMULATED,
            scope=DataScope.SAMPLE,
            freshness=DataFreshness.LIVE,
            limits={"documents": 1_000},
            caveats=("Fields are inferred from sampled documents, never declared.",),
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(
            caveats=("Aggregation pipelines; $out, $merge and server-side JavaScript are refused.",)
        ),
        predicate_pushdown=_source(),
        sort_pushdown=_source(
            caveats=("Missing values do not have SQL-style NULLS LAST semantics.",)
        ),
        limit_pushdown=_source(),
        projection_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Two-collection exact joins run in MongoDB; multi-collection and lexical "
                "plans stream full documents to Stych for pandas matching.",
                "A replica set or sharded cluster provides point-in-time snapshot export; "
                "standalone MongoDB uses one _id-ordered cursor, so concurrent inserts or "
                "deletes can be observed inconsistently during a full-stream fallback.",
            ),
            exact_caveats=(
                "Native $lookup currently covers two collections in one MongoDB database; "
                "larger plans use the confirmed complete-input fallback.",
            ),
        ),
        create_working_copy=_source(
            caveats=("Working copies are collections in a Stych-owned scratch database.",)
        ),
        mutate_working_copy=_source(
            caveats=("The source collection is never updated; writes are confined to scratch.",)
        ),
        export_snapshot=_source(
            caveats=(
                "One cursor sorted on the immutable _id, snapshot-isolated on a replica "
                "set or sharded cluster; a standalone mongod reads without a snapshot.",
            )
        ),
        stable_row_identity=_source(
            caveats=(
                "Every document carries _id, so a working copy is aligned with its source "
                "without a stamped row id or a pinned snapshot.",
            )
        ),
        nested_values=_source(
            caveats=("Documents are flattened to dotted columns; arrays become JSON text.",)
        ),
        schema_is_complete=_no("sample_inferred_schema"),
    ),
)


REDSHIFT = ConnectorManifest(
    id="redshift",
    display_name="Amazon Redshift",
    category="cloud_warehouse",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.CONTRACT_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(
            caveats=("Custom-query schemas are inferred from the loaded snapshot.",)
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        stream_arrow=_source(),
        create_working_copy=_source(
            caveats=("Working copies live in a Stych-owned scratch schema (default stych_temp).",)
        ),
        mutate_working_copy=_source(
            caveats=(
                "The source table is never updated; DML is confined to the scratch schema. "
                "Statements commit individually, so a failed sequence is not rolled back.",
            )
        ),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=(
                "Redshift defines no physical row address, so identity comes from a row id "
                "stamped onto working copies. Diffing a copy against the pinned snapshot is "
                "exact; comparing against the live source table needs a key column.",
            ),
        ),
        schema_is_complete=_source(),
    ),
)
SNOWFLAKE = ConnectorManifest(
    id="snowflake",
    display_name="Snowflake",
    category="cloud_warehouse",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.CONTRACT_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(
            caveats=("Custom-query schemas are inferred from the loaded snapshot.",)
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        stream_arrow=_source(),
        create_working_copy=_source(
            caveats=("Working copies live in a Stych-owned scratch schema (default STYCH_TEMP).",)
        ),
        mutate_working_copy=_source(
            caveats=(
                "The source table is never updated; DML is confined to the scratch schema. "
                "Snowflake auto-commits DDL, so a failed statement sequence is not rolled back.",
            )
        ),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=(
                "Working copies are stamped with a row id, so diffing a copy against the "
                "pinned snapshot of the source is exact. Comparing against the live source "
                "table to detect upstream drift needs a key column.",
            ),
        ),
        schema_is_complete=_source(),
    ),
)
BIGQUERY = ConnectorManifest(
    id="bigquery",
    display_name="BigQuery",
    category="cloud_warehouse",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.INTEGRATION_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(
            caveats=("Custom-query schemas are inferred from the loaded snapshot.",)
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        stream_arrow=_source(),
        create_working_copy=_source(
            caveats=(
                "Working copies live in a Stych-owned scratch dataset (default stych_temp), "
                "created in the same location as the source dataset.",
            )
        ),
        mutate_working_copy=_source(
            caveats=(
                "The source table is never updated; DML is confined to the scratch dataset. "
                "Each statement is its own job, so a failed sequence is not rolled back.",
                "Requires service-account credentials; the Google sign-in path is read-only "
                "and is no longer offered for new connections.",
            )
        ),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=(
                "Working copies are stamped with a row id, so diffing a copy against the "
                "pinned snapshot of the source is exact. Comparing against the live source "
                "table to detect upstream drift needs a key column.",
            ),
        ),
        schema_is_complete=_source(),
    ),
)
DATABRICKS = ConnectorManifest(
    id="databricks",
    display_name="Databricks",
    category="cloud_warehouse",
    version="1",
    maturity=ConnectorMaturity.BETA,
    verification=VerificationStatus.CONTRACT_TESTED,
    user_summary="Live · Full-data analysis",
    capabilities=_capabilities(
        discover_relations=_source(),
        describe_schema=_source(
            caveats=("Custom-query schemas are inferred from the loaded snapshot.",)
        ),
        preview_page=_source(),
        exact_count=_source(),
        full_scan=_source(),
        execute_native_query=_source(),
        projection_pushdown=_source(),
        predicate_pushdown=_source(),
        sort_pushdown=_source(),
        limit_pushdown=_source(),
        aggregation=_source(),
        same_source_join=_join_capability(
            source_side=True,
            top_level_executor=Executor.SOURCE,
            top_level_mode=SupportMode.NATIVE,
            top_level_freshness=DataFreshness.LIVE,
            top_level_caveats=(
                "Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching.",
            ),
        ),
        stream_arrow=_source(),
        create_working_copy=_source(
            caveats=("Working copies live in a Stych-owned scratch schema (default stych_temp).",)
        ),
        mutate_working_copy=_source(
            caveats=("The source table is never updated; DML is confined to the scratch schema.",)
        ),
        export_snapshot=_source(),
        stable_row_identity=_source(
            mode=SupportMode.EMULATED,
            caveats=(
                "Delta tables expose no physical row address, so identity needs a primary "
                "key or a unique id-like column. Without one: diff cannot align rows and "
                "reports schema changes only, annotation steps that materialize are "
                "skipped, and page order is not stable across requests. Predicates, "
                "highlight rules, and duplicate-value annotations still work.",
            ),
        ),
        nested_values=_source(),
        schema_is_complete=_source(),
    ),
)


_MANIFESTS = {
    manifest.id: manifest
    for manifest in (
        UPLOAD,
        POSTGRES,
        SQLSERVER,
        MONGODB,
        SNOWFLAKE,
        REDSHIFT,
        BIGQUERY,
        DATABRICKS,
    )
}


def connector_ids() -> tuple[str, ...]:
    return tuple(_MANIFESTS)


def manifests() -> tuple[ConnectorManifest, ...]:
    return tuple(_MANIFESTS.values())


def get_manifest(connector_id: str) -> ConnectorManifest:
    """Resolve a connector id; unknown ids fail closed instead of inheriting support."""
    try:
        return _MANIFESTS[connector_id]
    except KeyError as exc:
        raise KeyError(f"Unknown connector '{connector_id}'") from exc
