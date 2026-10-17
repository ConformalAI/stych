"""OntologySpec schema + validation against real field catalogs."""

from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from ontology.spec import OntologySpec
from ontology.validate import normalize_spec, validate_spec
from viz.catalog import build_catalog_pandas

ORDERS = "file-orders"
CUSTOMERS = "file-customers"


@pytest.fixture()
def catalogs():
    orders = pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4],
            "customer_id": [10, 11, 10, 12],
            "order_total": [100.0, 250.0, 80.0, 40.0],
            "refund_amount": [0.0, 50.0, 0.0, 10.0],
            "status": ["shipped", "cancelled", "shipped", "shipped"],
            "order_date": pd.to_datetime(["2024-01-05", "2024-01-20", "2024-02-10", "2024-03-01"]),
        }
    )
    customers = pd.DataFrame(
        {
            "id": [10, 11, 12],
            "region": ["EU", "NA", "EU"],
            "name": ["Ada", "Grace", "Alan"],
        }
    )
    return {ORDERS: build_catalog_pandas(orders), CUSTOMERS: build_catalog_pandas(customers)}


def _spec(**overrides) -> OntologySpec:
    base = {
        "entities": [
            {"name": "orders", "dataset": ORDERS, "primaryKey": "order_id"},
            {"name": "customers", "dataset": CUSTOMERS, "primaryKey": "id"},
        ],
        "relationships": [
            {
                "name": "order_customer",
                "fromEntity": "orders",
                "fromField": "customer_id",
                "toEntity": "customers",
                "toField": "id",
                "cardinality": "many_to_one",
            }
        ],
        "dimensions": [
            {"name": "region", "entity": "customers", "field": "region"},
            {"name": "status", "entity": "orders", "field": "status"},
        ],
        "metrics": [
            {
                "name": "revenue",
                "entity": "orders",
                "expr": "order_total - refund_amount",
                "aggregate": "sum",
                "filterExpr": "status != 'cancelled'",
            },
            {"name": "order_count", "entity": "orders", "aggregate": "count"},
        ],
    }
    base.update(overrides)
    return OntologySpec.model_validate(base)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_valid_spec_round_trips_through_aliases(catalogs):
    spec = _spec()
    wire = spec.to_json_dict()
    assert wire["relationships"][0]["fromEntity"] == "orders"
    assert wire["metrics"][0]["filterExpr"] == "status != 'cancelled'"
    assert OntologySpec.model_validate(wire).metric("revenue").expr == "order_total - refund_amount"


def test_names_must_be_snake_case():
    with pytest.raises(ValidationError):
        OntologySpec.model_validate({"entities": [{"name": "Orders", "dataset": ORDERS}]})


def test_one_namespace_across_kinds():
    with pytest.raises(ValidationError, match="both a dimension and a metric"):
        OntologySpec.model_validate(
            {
                "entities": [{"name": "orders", "dataset": ORDERS}],
                "dimensions": [{"name": "region", "entity": "orders", "field": "status"}],
                "metrics": [{"name": "region", "entity": "orders", "aggregate": "count"}],
            }
        )


def test_relationship_cannot_be_self_referential():
    with pytest.raises(ValidationError, match="two different entities"):
        OntologySpec.model_validate(
            {
                "entities": [{"name": "orders", "dataset": ORDERS}],
                "relationships": [
                    {
                        "name": "loop",
                        "fromEntity": "orders",
                        "fromField": "a",
                        "toEntity": "orders",
                        "toField": "b",
                    }
                ],
            }
        )


def test_sum_requires_an_expression():
    with pytest.raises(ValidationError, match="requires an expression"):
        OntologySpec.model_validate(
            {
                "entities": [{"name": "orders", "dataset": ORDERS}],
                "metrics": [{"name": "revenue", "entity": "orders", "aggregate": "sum"}],
            }
        )


def test_count_needs_no_expression():
    spec = OntologySpec.model_validate(
        {
            "entities": [{"name": "orders", "dataset": ORDERS}],
            "metrics": [{"name": "n", "entity": "orders", "aggregate": "count"}],
        }
    )
    assert spec.metric("n").expr is None


def test_synonyms_dedupe_case_insensitively():
    spec = OntologySpec.model_validate(
        {
            "entities": [{"name": "orders", "dataset": ORDERS}],
            "metrics": [
                {
                    "name": "n",
                    "entity": "orders",
                    "aggregate": "count",
                    "synonyms": ["Orders", "orders", " volume "],
                }
            ],
        }
    )
    assert spec.metric("n").synonyms == ["Orders", "volume"]


def test_is_certified_requires_every_definition(catalogs):
    spec = _spec()
    assert spec.is_certified() is False
    for item in [*spec.entities, *spec.relationships, *spec.dimensions, *spec.metrics]:
        item.certified = True
    assert spec.is_certified() is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_clean_spec_has_no_errors(catalogs):
    assert validate_spec(_spec(), catalogs) == []


def test_missing_dataset_reports_once(catalogs):
    spec = _spec(entities=[{"name": "orders", "dataset": "file-gone"}])
    errors = validate_spec(spec, catalogs)
    assert [e.code for e in errors].count("unknown_dataset") == 1


def test_unknown_column_on_dimension(catalogs):
    spec = _spec(dimensions=[{"name": "region", "entity": "customers", "field": "nope"}])
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "unknown_field" and "dimensions[0].field" in e.path for e in errors)


def test_relationship_field_must_exist(catalogs):
    spec = _spec(
        relationships=[
            {
                "name": "bad",
                "fromEntity": "orders",
                "fromField": "missing_col",
                "toEntity": "customers",
                "toField": "id",
            }
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "unknown_field" and "fromField" in e.path for e in errors)


def test_relationship_entity_must_exist(catalogs):
    spec = _spec(
        relationships=[
            {
                "name": "bad",
                "fromEntity": "orders",
                "fromField": "customer_id",
                "toEntity": "ghosts",
                "toField": "id",
            }
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "unknown_entity" for e in errors)


def test_metric_expression_unknown_field(catalogs):
    spec = _spec(
        metrics=[
            {"name": "revenue", "entity": "orders", "expr": "nope * 2", "aggregate": "sum"},
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "unknown_field" and e.path == "metrics[0].expr" for e in errors)


def test_metric_filter_must_be_boolean(catalogs):
    spec = _spec(
        metrics=[
            {
                "name": "revenue",
                "entity": "orders",
                "expr": "order_total",
                "aggregate": "sum",
                "filterExpr": "order_total",
            }
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "not_boolean" for e in errors)


def test_sum_of_text_is_rejected(catalogs):
    spec = _spec(
        metrics=[{"name": "bad", "entity": "orders", "expr": "status", "aggregate": "sum"}]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "not_numeric" for e in errors)


def test_default_time_field_must_be_temporal(catalogs):
    spec = _spec(
        metrics=[
            {
                "name": "revenue",
                "entity": "orders",
                "expr": "order_total",
                "aggregate": "sum",
                "defaultTimeField": "status",
            }
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "not_temporal" for e in errors)


def test_duplicate_relationship_is_flagged(catalogs):
    rel = {
        "fromEntity": "orders",
        "fromField": "customer_id",
        "toEntity": "customers",
        "toField": "id",
    }
    spec = _spec(relationships=[{"name": "a", **rel}, {"name": "b", **rel}])
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "duplicate_relationship" for e in errors)


def test_normalize_fills_dataset_label(catalogs):
    normalized = normalize_spec(_spec(), catalogs, {ORDERS: "orders.csv"})
    assert normalized.entity("orders").dataset_label == "orders.csv"
    # Datasets with no label supplied are left alone rather than guessed at.
    assert normalized.entity("customers").dataset_label is None


def test_normalize_never_overwrites_an_explicit_label(catalogs):
    spec = _spec(
        entities=[
            {"name": "orders", "dataset": ORDERS, "datasetLabel": "Q3 orders"},
            {"name": "customers", "dataset": CUSTOMERS},
        ]
    )
    normalized = normalize_spec(spec, catalogs, {ORDERS: "orders.csv"})
    assert normalized.entity("orders").dataset_label == "Q3 orders"


def test_datasets_are_deduped_in_entity_order(catalogs):
    assert _spec().datasets() == [ORDERS, CUSTOMERS]


# ---------------------------------------------------------------------------
# Incomplete-but-savable definitions
#
# The editor creates a dimension before its column is chosen, and blanks a
# relationship's field when the user switches entity. Those states must SAVE —
# a schema rejection would throw away the user's whole in-progress ontology —
# and come back as structured errors the inspector can render inline.
# ---------------------------------------------------------------------------


def test_dimension_without_a_column_is_accepted_by_the_schema(catalogs):
    spec = _spec(dimensions=[{"name": "region", "entity": "customers", "field": ""}])
    assert spec.dimension("region").field == ""


def test_dimension_without_a_column_reports_missing_field(catalogs):
    spec = _spec(dimensions=[{"name": "region", "entity": "customers", "field": ""}])
    errors = validate_spec(spec, catalogs)
    assert [e.code for e in errors] == ["missing_field"]
    assert errors[0].path == "dimensions[0].field"


def test_relationship_mid_edit_is_accepted_and_flagged(catalogs):
    spec = _spec(
        relationships=[
            {
                "name": "order_customer",
                "fromEntity": "orders",
                "fromField": "",
                "toEntity": "customers",
                "toField": "id",
            }
        ]
    )
    errors = validate_spec(spec, catalogs)
    assert any(e.code == "missing_field" and e.path.endswith("fromField") for e in errors)


def test_blank_column_does_not_masquerade_as_an_unknown_column(catalogs):
    """A missing choice and a wrong choice are different problems."""
    spec = _spec(dimensions=[{"name": "region", "entity": "customers", "field": ""}])
    assert all(e.code != "unknown_field" for e in validate_spec(spec, catalogs))
