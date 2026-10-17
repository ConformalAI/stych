"""Contract, registry, resolver, and generated-doc tests for data connectors."""

from __future__ import annotations

import inspect
from datetime import datetime

import pandas as pd
import pytest
from pydantic import ValidationError

from agents.state import DataContext
from datasources.capability_docs import DOC_PATH, render_json, render_markdown
from datasources.contracts import (
    AssetRef,
    Capability,
    CapabilitySupport,
    Connector,
    DataFreshness,
    DataScope,
    Executor,
    FeatureAvailability,
    ProductFeature,
    SourceRef,
    SupportMode,
    UnsupportedCapability,
)
from datasources.contracts.requests import ConnectionTestResult, DiscoveryRequest, PreviewRequest
from datasources.feature_resolver import capability_summary, resolve_features
from datasources.registry import connector_ids, get_manifest, manifests


def test_registry_is_complete_and_every_manifest_declares_every_capability():
    assert connector_ids() == (
        "upload",
        "postgres",
        "sqlserver",
        "mongodb",
        "snowflake",
        "redshift",
        "bigquery",
        "databricks",
    )
    for manifest in manifests():
        assert set(manifest.capabilities) == set(Capability)
        assert manifest.user_summary


def test_same_source_join_matrix_is_explicit_and_full_data_for_every_connector():
    expected_strategies = {
        "exact",
        "levenshtein",
        "jaro_winkler",
        "token_sort",
        "token_set",
        "partial_ratio",
        "cosine_tfidf",
        "soundex",
        "metaphone",
    }
    expected_types = {"INNER", "LEFT", "RIGHT", "FULL OUTER"}
    for manifest in manifests():
        support = manifest.capabilities[Capability.SAME_SOURCE_JOIN]
        assert support.join is not None
        assert {item.strategy for item in support.join.strategies} == expected_strategies
        assert set(support.join.join_types) == expected_types
        assert all(item.scope == DataScope.FULL for item in support.join.strategies)
        exact = next(item for item in support.join.strategies if item.strategy == "exact")
        lexical = next(item for item in support.join.strategies if item.strategy == "metaphone")
        if manifest.id == "upload":
            assert exact.executor is Executor.DUCKDB
            assert exact.egress == "none"
            assert lexical.executor is Executor.PANDAS
            assert lexical.egress == "none"
        else:
            assert exact.executor is Executor.SOURCE
            assert exact.egress == "preview_only"
            assert lexical.executor is Executor.PANDAS
            assert lexical.egress == "full"
            assert "full source" in " ".join(lexical.caveats)


def test_unsupported_capability_cannot_carry_join_details():
    from datasources.contracts import JoinStrategySupport, SameSourceJoinCapability

    exact = JoinStrategySupport(
        strategy="exact",
        mode=SupportMode.NATIVE,
        scope=DataScope.FULL,
        freshness=DataFreshness.LIVE,
        executor=Executor.SOURCE,
        egress="preview_only",
        result_mode="source_scratch",
    )
    with pytest.raises(ValidationError, match="cannot declare join semantics"):
        CapabilitySupport(
            mode=SupportMode.UNSUPPORTED,
            reason_code="not_available",
            join=SameSourceJoinCapability(strategies=(exact,), join_types=("INNER",)),
        )


def test_capability_support_rejects_ambiguous_declarations():
    with pytest.raises(ValidationError, match="reason_code"):
        CapabilitySupport(mode=SupportMode.UNSUPPORTED)
    with pytest.raises(ValidationError, match="scope, freshness, and executor"):
        CapabilitySupport(mode=SupportMode.NATIVE)
    with pytest.raises(ValidationError, match="cannot declare execution semantics"):
        CapabilitySupport(
            mode=SupportMode.UNSUPPORTED,
            reason_code="no",
            scope=DataScope.FULL,
            freshness=DataFreshness.LIVE,
            executor=Executor.SOURCE,
        )


def test_source_ref_is_credential_safe_and_forbids_secret_fields():
    source = SourceRef(
        source_id="src-1",
        connector_id="postgres",
        connection_ref="credential-store:abc",
        asset=AssetRef(namespace="public", name="orders"),
    )
    assert "password" not in source.model_dump()
    with pytest.raises(ValidationError, match="password"):
        SourceRef(
            source_id="src-1",
            connector_id="postgres",
            asset=AssetRef(name="orders"),
            password="secret",
        )


@pytest.mark.parametrize(
    ("connector_id", "feature", "availability", "scope", "executor"),
    [
        ("upload", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "duckdb"),
        ("upload", ProductFeature.PREVIEW, FeatureAvailability.AVAILABLE, "full", "duckdb"),
        ("postgres", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("sqlserver", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("mongodb", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("sqlserver", ProductFeature.CLEAN, FeatureAvailability.AVAILABLE, "full", "source"),
        ("mongodb", ProductFeature.CLEAN, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.CLEAN, FeatureAvailability.AVAILABLE, "full", "source"),
        ("mongodb", ProductFeature.PROFILE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("sqlserver", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("sqlserver", ProductFeature.DIFF, FeatureAvailability.AVAILABLE, "full", "source"),
        ("sqlserver", ProductFeature.VISUALIZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("mongodb", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.DIFF, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.EXPORT, FeatureAvailability.AVAILABLE, "full", "source"),
        ("redshift", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("snowflake", ProductFeature.PREVIEW, FeatureAvailability.AVAILABLE, "full", "source"),
        ("redshift", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("redshift", ProductFeature.DIFF, FeatureAvailability.AVAILABLE, "full", "source"),
        ("redshift", ProductFeature.EXPORT, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.DIFF, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.CLEAN, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.EXPORT, FeatureAvailability.AVAILABLE, "full", "source"),
        ("bigquery", ProductFeature.PREVIEW, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.ANALYZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.FILTER, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.CLEAN, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.VISUALIZE, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.DIFF, FeatureAvailability.AVAILABLE, "full", "source"),
        ("databricks", ProductFeature.EXPORT, FeatureAvailability.AVAILABLE, "full", "source"),
        ("postgres", ProductFeature.EXPORT, FeatureAvailability.AVAILABLE, "full", "source"),
        (
            "postgres",
            ProductFeature.SAME_SOURCE_JOIN,
            FeatureAvailability.AVAILABLE,
            "full",
            "source",
        ),
        (
            "databricks",
            ProductFeature.SAME_SOURCE_JOIN,
            FeatureAvailability.AVAILABLE,
            "full",
            "source",
        ),
        (
            "databricks",
            ProductFeature.CROSS_SOURCE_JOIN,
            FeatureAvailability.UNAVAILABLE,
            None,
            None,
        ),
        (
            "postgres",
            ProductFeature.CROSS_SOURCE_JOIN,
            FeatureAvailability.UNAVAILABLE,
            None,
            None,
        ),
        (
            "upload",
            ProductFeature.CROSS_SOURCE_JOIN,
            FeatureAvailability.UNAVAILABLE,
            None,
            None,
        ),
    ],
)
def test_feature_resolution_matches_current_execution_paths(
    connector_id, feature, availability, scope, executor
):
    decision = resolve_features(connector_id)[feature]
    assert decision.availability == availability
    assert (decision.scope.value if decision.scope else None) == scope
    assert (decision.execution_path.value if decision.execution_path else None) == executor


def test_unknown_connectors_fail_closed_but_get_a_safe_ui_summary():
    with pytest.raises(KeyError, match="Unknown connector"):
        get_manifest("future-source")
    assert capability_summary("future-source") == "Snapshot · Current loaded data"
    assert capability_summary("postgres", working_copy=True) == "Working copy · Full-data analysis"


def test_existing_sql_dispatch_is_driven_by_capability_contract():
    frame = pd.DataFrame({"x": [1]})

    def context(source: str, with_url: bool = True) -> DataContext:
        return DataContext(
            file_id="f1",
            filename="data",
            raw_dataframe=frame,
            working_dataframe=frame.copy(),
            uploaded_at=datetime.now(),
            metadata={
                "source": source,
                **({"connection_url": "credential://reference"} if with_url else {}),
            },
        )

    assert context("postgres").is_sql_datasource() is True
    assert context("postgres", with_url=False).is_sql_datasource() is False
    # SQL Server runs source-side now, so a live URL makes it a SQL datasource.
    assert context("sqlserver").is_sql_datasource() is True
    assert context("sqlserver", with_url=False).is_sql_datasource() is False
    # MongoDB runs source-side too, through aggregation rather than SQL.
    assert context("mongodb").is_sql_datasource() is True
    assert context("mongodb", with_url=False).is_sql_datasource() is False
    assert context("future-source").is_sql_datasource() is False

    encoded = '{"server_hostname":"h","http_path":"/p","access_token":"t","catalog":"main"}'
    dbx = DataContext(
        file_id="f1",
        filename="data",
        raw_dataframe=frame,
        working_dataframe=frame.copy(),
        uploaded_at=datetime.now(),
        metadata={"source": "databricks", "connection_config": encoded},
    )
    assert dbx.is_sql_datasource() is True
    dbx_restored = DataContext(
        file_id="f1",
        filename="data",
        raw_dataframe=frame,
        working_dataframe=frame.copy(),
        uploaded_at=datetime.now(),
        metadata={"source": "databricks", "connection_config": "[REDACTED]"},
    )
    assert dbx_restored.is_sql_datasource() is False


def test_analysis_dispatch_does_not_regress_to_connector_name_checks():
    implementation = inspect.getsource(DataContext.is_sql_datasource)
    assert '== "postgres"' not in implementation
    assert "resolve_feature" in implementation


class _MinimalConnector(Connector):
    @property
    def manifest(self):
        return get_manifest("upload")

    def test_connection(self, connection_ref):
        return ConnectionTestResult(success=True, message="ok")

    def discover(self, connection_ref, request: DiscoveryRequest):
        return []

    def describe(self, source):
        return []

    def preview(self, source, request: PreviewRequest):
        return []


def test_optional_connector_methods_fail_with_typed_capability_error():
    connector = _MinimalConnector()
    source = SourceRef(source_id="f1", connector_id="upload", asset=AssetRef(name="orders.csv"))
    with pytest.raises(UnsupportedCapability) as exc:
        connector.estimate(source, None)
    assert exc.value.capability == Capability.ESTIMATE_QUERY
    assert exc.value.connector_id == "upload"


def test_generated_capability_doc_and_json_are_deterministic_and_current():
    first = render_markdown()
    assert first == render_markdown()
    assert DOC_PATH.read_text() == first
    # The doc must keep saying the MongoDB schema is sampled, since that is the
    # one thing source-side execution does not fix.
    assert "sampled schema" in first.lower()
    assert '"connector_id"' not in render_json()  # manifests contain no runtime/credential refs
