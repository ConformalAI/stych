"""Resolution + execution: named metrics become a QueryPlan and real numbers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd
import pytest

from agents.state import DataContext
from ontology.executor import OntologyValidationError, run_query
from ontology.resolve import QueryDimension, ResolutionError, find_join_path, resolve_query
from ontology.spec import OntologySpec
from viz.catalog import build_catalog_pandas

ORDERS = "file-orders"
CUSTOMERS = "file-customers"
PRODUCTS = "file-products"


def _orders() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4, 5],
            "customer_id": [10, 11, 10, 12, 11],
            "product_id": [100, 100, 101, 101, 102],
            "order_total": [100.0, 250.0, 80.0, 40.0, 60.0],
            "refund_amount": [0.0, 50.0, 0.0, 10.0, 0.0],
            "status": ["shipped", "cancelled", "shipped", "shipped", "cancelled"],
            "order_date": pd.to_datetime(
                ["2024-01-05", "2024-01-20", "2024-02-10", "2024-03-01", "2024-03-15"]
            ),
        }
    )


def _customers() -> pd.DataFrame:
    return pd.DataFrame(
        {"id": [10, 11, 12], "region": ["EU", "NA", "EU"], "name": ["Ada", "Grace", "Alan"]}
    )


def _products() -> pd.DataFrame:
    return pd.DataFrame({"sku": [100, 101, 102], "category": ["widget", "gadget", "widget"]})


class FakeState:
    """Minimal AgentState stand-in: the data contexts an ontology resolves against."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        contexts = [
            DataContext(
                file_id=file_id,
                filename=f"{file_id}.csv",
                raw_dataframe=df,
                working_dataframe=df,
                uploaded_at=datetime.now(timezone.utc),
                metadata={},
            )
            for file_id, df in frames.items()
        ]
        self.data_context = contexts[0]
        self.additional_data_contexts = contexts[1:]
        self.metadata: dict = {}
        # The session data plane resolves file sessions through a workspace,
        # which is keyed on the session id.
        self.session_id = f"ontology-resolve-{uuid.uuid4()}"

    def get_all_data_contexts(self):
        return [self.data_context, *self.additional_data_contexts]


@pytest.fixture()
def state() -> FakeState:
    return FakeState({ORDERS: _orders(), CUSTOMERS: _customers(), PRODUCTS: _products()})


@pytest.fixture()
def spec() -> OntologySpec:
    return OntologySpec.model_validate(
        {
            "entities": [
                {"name": "orders", "dataset": ORDERS, "primaryKey": "order_id"},
                {"name": "customers", "dataset": CUSTOMERS, "primaryKey": "id"},
                {"name": "products", "dataset": PRODUCTS, "primaryKey": "sku"},
            ],
            "relationships": [
                {
                    "name": "order_customer",
                    "fromEntity": "orders",
                    "fromField": "customer_id",
                    "toEntity": "customers",
                    "toField": "id",
                    "cardinality": "many_to_one",
                },
                {
                    "name": "order_product",
                    "fromEntity": "orders",
                    "fromField": "product_id",
                    "toEntity": "products",
                    "toField": "sku",
                    "cardinality": "many_to_one",
                },
            ],
            "dimensions": [
                {"name": "region", "entity": "customers", "field": "region", "certified": True},
                {"name": "status", "entity": "orders", "field": "status"},
                {"name": "category", "entity": "products", "field": "category"},
                {"name": "order_date", "entity": "orders", "field": "order_date"},
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "entity": "orders",
                    "expr": "order_total - refund_amount",
                    "aggregate": "sum",
                    "filterExpr": "status != 'cancelled'",
                    "certified": True,
                },
                {"name": "order_count", "entity": "orders", "aggregate": "count"},
                {
                    "name": "shipped_count",
                    "entity": "orders",
                    "aggregate": "count",
                    "filterExpr": "status != 'cancelled'",
                },
                {
                    "name": "gross",
                    "entity": "orders",
                    "expr": "order_total",
                    "aggregate": "sum",
                    "certified": True,
                },
            ],
        }
    )


def _rows(data) -> dict:
    """{dimension value: measure value} for a one-dim one-measure result."""
    return {row[0]: row[1] for row in data.rows}


# ---------------------------------------------------------------------------
# Join paths
# ---------------------------------------------------------------------------


def test_plan_join_builds_adjacency_once(spec, monkeypatch):
    """Several targets share one adjacency map instead of rebuilding it per hop."""
    from ontology import resolve as resolve_mod
    from ontology.resolve import plan_join

    calls: list[int] = []
    real = resolve_mod._adjacency

    def _counted(s):
        calls.append(1)
        return real(s)

    monkeypatch.setattr(resolve_mod, "_adjacency", _counted)
    base, joins, errors = plan_join(
        spec,
        ["revenue", "order_count"],
        [QueryDimension(name="region"), QueryDimension(name="category")],
    )
    assert base is not None and not errors
    assert len(calls) == 1


def test_direct_join_path(spec):
    path = find_join_path(spec, "orders", "customers")
    assert [s.right_entity for s in path] == ["customers"]
    assert path[0].left_field == "customer_id" and path[0].right_field == "id"


def test_reverse_join_path_flips_the_fields(spec):
    path = find_join_path(spec, "customers", "orders")
    assert path[0].left_field == "id" and path[0].right_field == "customer_id"


def test_transitive_join_path(spec):
    path = find_join_path(spec, "customers", "products")
    assert [s.right_entity for s in path] == ["orders", "products"]


def test_no_path_when_unrelated():
    spec = OntologySpec.model_validate(
        {
            "entities": [
                {"name": "a", "dataset": ORDERS},
                {"name": "b", "dataset": CUSTOMERS},
            ]
        }
    )
    assert find_join_path(spec, "a", "b") is None


def test_same_entity_needs_no_join(spec):
    assert find_join_path(spec, "orders", "orders") == []


# ---------------------------------------------------------------------------
# Plan shape
# ---------------------------------------------------------------------------


def test_channels_are_named_after_definitions(spec):
    catalog = build_catalog_pandas(_orders())
    resolved = resolve_query(spec, ["revenue"], [QueryDimension("status")], catalog)
    assert resolved.plan.channels() == ["status", "revenue"]
    assert resolved.plan.aggregated is True


def test_filtered_count_becomes_a_summed_indicator(spec):
    catalog = build_catalog_pandas(_orders())
    resolved = resolve_query(spec, ["shipped_count"], [], catalog)
    measure = resolved.plan.measures[0]
    assert measure.aggregate == "sum"
    assert resolved.plan.calcs[0].expr == "if(status != 'cancelled', 1, 0)"


def test_unfiltered_count_stays_a_count(spec):
    catalog = build_catalog_pandas(_orders())
    resolved = resolve_query(spec, ["order_count"], [], catalog)
    assert resolved.plan.measures[0].aggregate == "count"
    assert resolved.plan.calcs == []


def test_uncertified_definitions_are_reported(spec):
    catalog = build_catalog_pandas(_orders())
    resolved = resolve_query(spec, ["revenue", "order_count"], [QueryDimension("status")], catalog)
    # revenue + region are certified; order_count and status are not.
    assert resolved.uncertified == ["order_count", "status"]


def test_unknown_metric_raises_structured_error(spec):
    catalog = build_catalog_pandas(_orders())
    with pytest.raises(ResolutionError) as exc:
        resolve_query(spec, ["profit"], [], catalog)
    assert exc.value.errors[0].code == "unknown_metric"


def test_query_without_metrics_is_rejected(spec):
    catalog = build_catalog_pandas(_orders())
    with pytest.raises(ResolutionError) as exc:
        resolve_query(spec, [], [QueryDimension("status")], catalog)
    assert exc.value.errors[0].code == "no_measures"


def test_time_unit_on_a_non_date_is_rejected(spec):
    catalog = build_catalog_pandas(_orders())
    with pytest.raises(ResolutionError) as exc:
        resolve_query(spec, ["revenue"], [QueryDimension("status", time_unit="month")], catalog)
    assert exc.value.errors[0].code == "not_temporal"


# ---------------------------------------------------------------------------
# Execution — single entity
# ---------------------------------------------------------------------------


async def test_metric_filter_folds_into_its_own_aggregate(state, spec):
    """revenue excludes cancelled orders; gross does not — in ONE query."""
    _, data = await run_query(state, spec, ["revenue", "gross"], [])
    columns = [c.name for c in data.columns]
    row = dict(zip(columns, data.rows[0]))
    # shipped: (100-0) + (80-0) + (40-10) = 210 ; gross over ALL rows = 530
    assert row["revenue"] == pytest.approx(210.0)
    assert row["gross"] == pytest.approx(530.0)


async def test_group_by_own_entity_dimension(state, spec):
    _, data = await run_query(state, spec, ["order_count"], [QueryDimension("status")])
    assert _rows(data) == {"shipped": 3, "cancelled": 2}


async def test_filtered_count_matches_the_filter(state, spec):
    _, data = await run_query(state, spec, ["shipped_count"], [])
    assert data.rows[0][0] == 3


async def test_time_unit_buckets_by_month(state, spec):
    _, data = await run_query(
        state, spec, ["order_count"], [QueryDimension("order_date", time_unit="month")]
    )
    assert sum(row[1] for row in data.rows) == 5
    assert len(data.rows) == 3


# ---------------------------------------------------------------------------
# Execution — cross entity
# ---------------------------------------------------------------------------


async def test_metric_grouped_by_a_joined_dimension(state, spec):
    """The whole point: revenue lives on orders, region lives on customers."""
    resolved, data = await run_query(state, spec, ["revenue"], [QueryDimension("region")])
    assert resolved.spans_entities is True
    # EU = customers 10 and 12 → orders 1 (100), 3 (80), 4 (30 after refund) = 210
    # NA = customer 11 → orders 2 (cancelled) and 5 (cancelled) = 0 contribution
    assert _rows(data)["EU"] == pytest.approx(210.0)


async def test_transitive_join_reaches_a_second_hop(state, spec):
    resolved, data = await run_query(state, spec, ["order_count"], [QueryDimension("category")])
    assert len(resolved.joins) == 1
    assert _rows(data) == {"widget": 3, "gadget": 2}


async def test_joined_columns_do_not_collide(state, spec):
    """orders.order_id and customers.id both exist; the prefix keeps them apart."""
    from ontology.executor import build_joined_frame
    from ontology.resolve import plan_join

    base, joins, errors = plan_join(spec, ["revenue"], [QueryDimension("region")])
    assert errors == []
    frame = build_joined_frame(state, spec, base.name, joins)
    assert "order_id" in frame.columns
    assert "customers__id" in frame.columns
    assert len(frame) == len(_orders())


async def test_unrelated_entity_reports_no_join_path(state):
    spec = OntologySpec.model_validate(
        {
            "entities": [
                {"name": "orders", "dataset": ORDERS},
                {"name": "products", "dataset": PRODUCTS},
            ],
            "dimensions": [{"name": "category", "entity": "products", "field": "category"}],
            "metrics": [{"name": "n", "entity": "orders", "aggregate": "count"}],
        }
    )
    with pytest.raises(OntologyValidationError) as exc:
        await run_query(state, spec, ["n"], [QueryDimension("category")])
    assert exc.value.errors[0].code == "no_join_path"


async def test_querying_a_dimension_with_no_column_explains_itself(state, spec):
    """An incomplete dimension must fail with its own reason, not a blank-column lookup.

    Asserted through ``preview_query`` because that is the wrapper the routes and
    the agent tools call — it normalizes ResolutionError into the structured
    OntologyValidationError they both render.
    """
    from ontology.executor import preview_query

    unset = type(spec.dimensions[0])(name="unset", entity="orders", field="", certified=False)
    spec = spec.model_copy(update={"dimensions": [*spec.dimensions, unset]})
    with pytest.raises(OntologyValidationError) as exc:
        await preview_query(state, spec, ["revenue"], [QueryDimension("unset")])
    assert exc.value.errors[0].code == "missing_field"
    assert "no column set" in exc.value.errors[0].message


# ---------------------------------------------------------------------------
# Fan-out detection
#
# A join that matches many rows per base row duplicates the base rows, which
# silently inflates every additive measure. The query still runs — a fanned-out
# count is sometimes the question — but no surface may report the number
# without saying so.
# ---------------------------------------------------------------------------


async def test_many_to_one_traversed_forward_does_not_fan_out(state, spec):
    """orders → customers: at most one customer per order."""
    resolved, _ = await run_query(state, spec, ["revenue"], [QueryDimension("region")])
    assert resolved.spans_entities is True
    assert resolved.fan_out_relationships == []


def test_the_same_relationship_backwards_does_fan_out(spec):
    """customers → orders is one-to-many: each customer duplicates per order."""
    from ontology.resolve import fans_out, find_join_path

    path = find_join_path(spec, "customers", "orders")
    assert [fans_out(s) for s in path] == [True]


async def test_one_to_many_is_flagged_on_the_resolved_query(state, spec):
    spec.relationships[0].cardinality = "one_to_many"  # orders → customers
    resolved, _ = await run_query(state, spec, ["revenue"], [QueryDimension("region")])
    assert resolved.fan_out_relationships == ["order_customer"]


async def test_many_to_many_always_fans_out(state, spec):
    spec.relationships[0].cardinality = "many_to_many"
    resolved, _ = await run_query(state, spec, ["revenue"], [QueryDimension("region")])
    assert resolved.fan_out_relationships == ["order_customer"]


async def test_one_to_one_never_fans_out(state, spec):
    spec.relationships[0].cardinality = "one_to_one"
    resolved, _ = await run_query(state, spec, ["revenue"], [QueryDimension("region")])
    assert resolved.fan_out_relationships == []


def test_single_entity_query_has_nothing_to_fan_out(spec):
    catalog = build_catalog_pandas(_orders())
    resolved = resolve_query(spec, ["revenue"], [QueryDimension("status")], catalog)
    assert resolved.fan_out_relationships == []


async def test_fan_out_actually_inflates_the_number(state, spec):
    """The reason this warning exists, demonstrated end to end."""
    # Truth: 5 orders, so a count over orders is 5.
    _, honest = await run_query(state, spec, ["order_count"], [])
    assert honest.rows[0][0] == 5

    # Declaring the join backwards makes customers the base and duplicates it
    # per order — the same count now reports a different, inflated figure.
    customer_count = type(spec.metrics[1])(
        name="customer_count", entity="customers", aggregate="count"
    )
    spec = spec.model_copy(update={"metrics": [*spec.metrics, customer_count]})
    resolved, inflated = await run_query(
        state, spec, ["customer_count"], [QueryDimension("status")]
    )
    assert resolved.fan_out_relationships == ["order_customer"]
    # 3 customers, but 5 order rows — the count follows the duplication.
    assert sum(row[1] for row in inflated.rows) == 5


def test_lookups_follow_a_model_copy_update(spec):
    """The name index is rebuilt on copy, so an updated list is what lookups see."""
    renamed = spec.entity("orders").model_copy(update={"dataset_label": "Orders (2024)"})
    others = [e for e in spec.entities if e.name != "orders"]
    copied = spec.model_copy(update={"entities": [renamed, *others]})

    assert copied.entity("orders") is renamed
    assert copied.entity("orders").dataset_label == "Orders (2024)"
    assert spec.entity("orders").dataset_label != "Orders (2024)"  # original untouched
    assert copied.metric("revenue") is not None and copied.entity("nope") is None
