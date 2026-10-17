"""Field-catalog semantic inference tests."""

from __future__ import annotations

import pandas as pd

from viz.catalog import build_catalog_mongo, build_catalog_pandas, build_catalog_sql
from viz.compile_pandas import execute_pandas
from viz.compile_sql import compile_spec_sql
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec


def test_catalog_detects_dirty_numeric_text():
    dirty = pd.DataFrame(
        {
            "quantity": ["2", "1", "ERROR", "3", "4"],
            "total_spent": ["4.0", "3.0", "UNKNOWN", "6.0", "8.0"],
            "item": ["Coffee", "Cake", "Tea", "Coffee", "Sandwich"],
        }
    )
    catalog = build_catalog_pandas(dirty)

    assert catalog.get("quantity").role == "measure"
    assert catalog.get("quantity").type == "quantitative"
    assert catalog.get("total_spent").default_aggregate == "sum"
    assert catalog.get("item").role == "dimension"


def test_mongo_catalog_labels_sampled_schema_and_live_engine():
    preview = pd.DataFrame({"item": ["Coffee", "Tea"], "revenue": [4.0, None]})
    catalog = build_catalog_mongo(
        "cafe",
        "sales",
        [
            {"column_name": "item", "data_type": "object", "sampled": True},
            {"column_name": "revenue", "data_type": "float64", "sampled": True},
        ],
        row_count=25_000,
        preview_df=preview,
    )

    assert catalog.engine == "mongodb"
    assert catalog.row_count == 25_000
    assert catalog.scope == "full"
    assert catalog.freshness == "live"
    assert catalog.schema_complete is False
    assert catalog.truncated is False
    assert catalog.get("revenue").default_aggregate == "sum"


def test_sql_catalog_keeps_authoritative_numeric_roles_for_high_cardinality_preview():
    preview = pd.DataFrame({"order_id": range(100), "revenue": range(100)})
    catalog = build_catalog_sql(
        "stych_temp",
        "orders",
        [
            {"column_name": "order_id", "data_type": "bigint"},
            {"column_name": "revenue", "data_type": "numeric"},
        ],
        row_count=100,
        preview_df=preview,
    )

    assert catalog.get("order_id").role == "measure"
    assert catalog.get("revenue").role == "measure"
    assert catalog.get("revenue").default_aggregate == "sum"


def test_sql_catalog_infers_dirty_text_semantics_from_preview():
    preview = pd.DataFrame(
        {
            "transaction_id": ["T1", "T2", "T3", "T4", "T5"],
            "quantity": ["2", "1", "ERROR", "3", "4"],
            "total_spent": ["4.0", "3.0", "UNKNOWN", "6.0", "8.0"],
            "transaction_date": [
                "2023-01-01",
                "2023-01-02",
                "ERROR",
                "2023-01-03",
                "2023-01-04",
            ],
        }
    )
    columns = [{"column_name": name, "data_type": "text"} for name in preview.columns]
    catalog = build_catalog_sql("public", "dirty_cafe_sales", columns, 10_000, preview)

    assert catalog.get("transaction_id").role == "dimension"
    assert catalog.get("quantity").role == "measure"
    assert catalog.get("total_spent").type == "quantitative"
    assert catalog.get("transaction_date").type == "temporal"


def test_dirty_numeric_text_aggregates_safely_in_both_engines():
    frame = pd.DataFrame(
        {
            "item": ["Coffee", "Cake", "Tea", "Coffee", "Sandwich"],
            "total_spent": ["4.0", "3.0", "UNKNOWN", "6.0", "8.0"],
        }
    )
    pandas_catalog = build_catalog_pandas(frame)
    spec = normalize_spec(
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "item", "type": "nominal"},
                    "y": {
                        "field": "total_spent",
                        "type": "quantitative",
                        "aggregate": "sum",
                    },
                },
            }
        ),
        pandas_catalog,
    )

    result = execute_pandas(frame, build_plan(spec))
    assert result.rows == [
        ["Coffee", 10.0],
        ["Sandwich", 8.0],
        ["Cake", 3.0],
        ["Tea", None],
    ]

    sql_catalog = build_catalog_sql(
        "public",
        "dirty_cafe_sales",
        [
            {"column_name": "item", "data_type": "text"},
            {"column_name": "total_spent", "data_type": "text"},
        ],
        row_count=len(frame),
        preview_df=frame,
    )
    sql = compile_spec_sql(build_plan(spec), sql_catalog, "public", "dirty_cafe_sales")
    assert 'CASE WHEN CAST("total_spent" AS TEXT) ~' in sql
    assert 'THEN CAST(CAST("total_spent" AS TEXT) AS NUMERIC) END' in sql
