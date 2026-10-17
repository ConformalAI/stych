# Connector capabilities

> Generated from `backend/datasources/registry.py`. Do not edit by hand. Run
> `cd backend && uv run python -m datasources.capability_docs --write` after manifest changes,
> and use `--check` to detect drift. The backend contract test enforces the same check.

This document describes what Stych implements today, not what the upstream data source
could theoretically support. `native` executes in the source or local engine directly;
`emulated` is implemented by Stych. Scope and freshness are explicit so a sampled snapshot
cannot be mistaken for a live full-data path.

## Connectors

| Connector | ID | Maturity | Verification | User summary |
| --- | --- | --- | --- | --- |
| File upload | upload | production | contract_tested | Snapshot · Full-data analysis |
| PostgreSQL | postgres | production | integration_tested | Live · Full-data analysis |
| SQL Server | sqlserver | beta | contract_tested | Live · Full-data analysis |
| MongoDB | mongodb | beta | integration_tested | Live · Full-data analysis (sampled schema) |
| Snowflake | snowflake | beta | contract_tested | Live · Full-data analysis |
| Amazon Redshift | redshift | beta | contract_tested | Live · Full-data analysis |
| BigQuery | bigquery | beta | integration_tested | Live · Full-data analysis |
| Databricks | databricks | beta | contract_tested | Live · Full-data analysis |

Verification is separate from declared support: `contract_tested` validates the shared
contract and local behavior; `integration_tested` also has real-engine parity tests. Cloud
tests may still require environment-provided credentials.

## Product features

| Feature | File upload | PostgreSQL | SQL Server | MongoDB | Snowflake | Amazon Redshift | BigQuery | Databricks |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| preview | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| profile | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| analyze | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| visualize | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| filter | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| clean | available (full/snapshot; pandas) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| diff | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| export | available (full/snapshot; pandas) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| same_source_join | available (full/snapshot; duckdb) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) | available (full/live; source) |
| cross_source_join | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |

## Same-source join matrix

| Connector | Exact | Lexical strategies | Join types | Top-level caveat |
| --- | --- | --- | --- | --- |
| File upload | emulated, duckdb, full, none egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, none egress | INNER, LEFT, RIGHT, FULL OUTER | All current exact and lexical strategies run over the complete uploaded snapshot. |
| PostgreSQL | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| SQL Server | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| MongoDB | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Two-collection exact joins run in MongoDB; multi-collection and lexical plans stream full documents to Stych for pandas matching. A replica set or sharded cluster provides point-in-time snapshot export; standalone MongoDB uses one _id-ordered cursor, so concurrent inserts or deletes can be observed inconsistently during a full-stream fallback. |
| Snowflake | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| Amazon Redshift | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| BigQuery | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| Databricks | native, source, full, preview_only egress | levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, metaphone: pandas, full, full egress | INNER, LEFT, RIGHT, FULL OUTER | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |

The lexical list is the current Stych set: levenshtein, jaro_winkler, token_sort, token_set, partial_ratio, cosine_tfidf, soundex, and metaphone. Full egress means the complete source inputs are streamed to Stych for matching; it never means a bounded preview is substituted. Exact source joins write only to Stych scratch.

## Primitive capabilities

| Capability | File upload | PostgreSQL | SQL Server | MongoDB | Snowflake | Amazon Redshift | BigQuery | Databricks |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| discover_namespaces | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| discover_relations | — (not_implemented) | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| describe_schema | native · full · snapshot · duckdb | native · full · live · source | native · full · live · source | emulated · sample · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| preview_page | native · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| exact_count | native · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| column_stats | native · full · snapshot · duckdb | native · full · live · source | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| full_scan | native · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| stream_arrow | — (not_implemented) | native · full · live · source | native · full · live · source | — (not_implemented) | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| execute_native_query | — (not_implemented) | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| estimate_query | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| cancel_query | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| projection_pushdown | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| predicate_pushdown | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| sort_pushdown | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| limit_pushdown | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| aggregation | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| same_source_join | emulated · full · snapshot · duckdb | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| create_working_copy | emulated · full · snapshot · pandas | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| mutate_working_copy | emulated · full · snapshot · pandas | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| write_back | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| export_snapshot | native · full · snapshot · pandas | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |
| incremental_read | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| cdc | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) | — (not_implemented) |
| stable_row_identity | emulated · full · snapshot · duckdb | emulated · full · live · source | emulated · full · live · source | native · full · live · source | emulated · full · live · source | emulated · full · live · source | emulated · full · live · source | emulated · full · live · source |
| nested_values | — (not_implemented) | native · full · live · source | — (not_implemented) | native · full · live · source | — (not_implemented) | — (not_implemented) | — (not_implemented) | native · full · live · source |
| schema_is_complete | native · full · snapshot · duckdb | native · full · live · source | native · full · live · source | — (sample_inferred_schema) | native · full · live · source | native · full · live · source | native · full · live · source | native · full · live · source |

## Connector author contract

1. Implement `datasources.contracts.Connector`; unsupported optional methods must raise
   the typed `UnsupportedCapability` error.
2. Register one exhaustive `ConnectorManifest` in `datasources.registry`. Every primitive
   must be declared as `native`, `emulated`, or `unsupported`, including scope, freshness,
   executor, limits, and caveats where applicable.
3. Pass only an opaque `connection_ref` through `SourceRef`; credentials and connection URLs
   are not part of the public source contract.
4. Route product behavior through `datasources.feature_resolver` and add conformance, resolver,
   API, and real-engine tests appropriate to the declared verification level.
5. Regenerate this document and run `--check` before merging.

A connector is not considered feature-complete merely because its upstream database supports
an operation. The manifest records only paths that Stych implements and tests.

## Declared caveats

| Connector | Capability | Caveat |
| --- | --- | --- |
| File upload | same_source_join | All current exact and lexical strategies run over the complete uploaded snapshot. |
| PostgreSQL | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| PostgreSQL | create_working_copy | Working copies live in a Stych-owned scratch schema (stych_temp). |
| PostgreSQL | mutate_working_copy | The source table is never updated; DML is confined to the scratch schema. |
| PostgreSQL | stable_row_identity | Row identity is synthesized when no key is selected. |
| SQL Server | describe_schema | Custom-query schemas are inferred from the loaded sample. |
| SQL Server | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| SQL Server | create_working_copy | Working copies live in a Stych-owned scratch schema (default stych_temp). |
| SQL Server | mutate_working_copy | The source table is never updated; DML is confined to the scratch schema. |
| SQL Server | stable_row_identity | Working copies are stamped with a row id, so diffing a copy against the pinned snapshot of the source is exact. Comparing against the live source table to detect upstream drift needs a key column. |
| MongoDB | describe_schema | Fields are inferred from sampled documents, never declared. |
| MongoDB | execute_native_query | Aggregation pipelines; $out, $merge and server-side JavaScript are refused. |
| MongoDB | sort_pushdown | Missing values do not have SQL-style NULLS LAST semantics. |
| MongoDB | same_source_join | Two-collection exact joins run in MongoDB; multi-collection and lexical plans stream full documents to Stych for pandas matching. |
| MongoDB | same_source_join | A replica set or sharded cluster provides point-in-time snapshot export; standalone MongoDB uses one _id-ordered cursor, so concurrent inserts or deletes can be observed inconsistently during a full-stream fallback. |
| MongoDB | create_working_copy | Working copies are collections in a Stych-owned scratch database. |
| MongoDB | mutate_working_copy | The source collection is never updated; writes are confined to scratch. |
| MongoDB | export_snapshot | One cursor sorted on the immutable _id, snapshot-isolated on a replica set or sharded cluster; a standalone mongod reads without a snapshot. |
| MongoDB | stable_row_identity | Every document carries _id, so a working copy is aligned with its source without a stamped row id or a pinned snapshot. |
| MongoDB | nested_values | Documents are flattened to dotted columns; arrays become JSON text. |
| Snowflake | describe_schema | Custom-query schemas are inferred from the loaded snapshot. |
| Snowflake | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| Snowflake | create_working_copy | Working copies live in a Stych-owned scratch schema (default STYCH_TEMP). |
| Snowflake | mutate_working_copy | The source table is never updated; DML is confined to the scratch schema. Snowflake auto-commits DDL, so a failed statement sequence is not rolled back. |
| Snowflake | stable_row_identity | Working copies are stamped with a row id, so diffing a copy against the pinned snapshot of the source is exact. Comparing against the live source table to detect upstream drift needs a key column. |
| Amazon Redshift | describe_schema | Custom-query schemas are inferred from the loaded snapshot. |
| Amazon Redshift | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| Amazon Redshift | create_working_copy | Working copies live in a Stych-owned scratch schema (default stych_temp). |
| Amazon Redshift | mutate_working_copy | The source table is never updated; DML is confined to the scratch schema. Statements commit individually, so a failed sequence is not rolled back. |
| Amazon Redshift | stable_row_identity | Redshift defines no physical row address, so identity comes from a row id stamped onto working copies. Diffing a copy against the pinned snapshot is exact; comparing against the live source table needs a key column. |
| BigQuery | describe_schema | Custom-query schemas are inferred from the loaded snapshot. |
| BigQuery | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| BigQuery | create_working_copy | Working copies live in a Stych-owned scratch dataset (default stych_temp), created in the same location as the source dataset. |
| BigQuery | mutate_working_copy | The source table is never updated; DML is confined to the scratch dataset. Each statement is its own job, so a failed sequence is not rolled back. |
| BigQuery | mutate_working_copy | Requires service-account credentials; the Google sign-in path is read-only and is no longer offered for new connections. |
| BigQuery | stable_row_identity | Working copies are stamped with a row id, so diffing a copy against the pinned snapshot of the source is exact. Comparing against the live source table to detect upstream drift needs a key column. |
| Databricks | describe_schema | Custom-query schemas are inferred from the loaded snapshot. |
| Databricks | same_source_join | Exact joins run in the source; lexical strategies stream full inputs to Stych for pandas matching. |
| Databricks | create_working_copy | Working copies live in a Stych-owned scratch schema (default stych_temp). |
| Databricks | mutate_working_copy | The source table is never updated; DML is confined to the scratch schema. |
| Databricks | stable_row_identity | Delta tables expose no physical row address, so identity needs a primary key or a unique id-like column. Without one: diff cannot align rows and reports schema changes only, annotation steps that materialize are skipped, and page order is not stable across requests. Predicates, highlight rules, and duplicate-value annotations still work. |
