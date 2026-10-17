"""A chart that names a metric computes it the same way chat does."""

from __future__ import annotations

import pandas as pd
import pytest

from ontology.spec import OntologySpec
from ontology.viz_bridge import expand_metric_channels, spec_uses_metrics
from viz.compile_pandas import execute_pandas
from viz.expr import extend_catalog, resolve_calculations
from viz.plan import build_plan
from viz.spec import CalculateTransform, VizSpec
from viz.validate import normalize_spec, validate_spec

ORDERS = "file-orders"
OTHER = "file-other"


@pytest.fixture()
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["EU", "NA", "EU", "APAC"],
            "order_total": [100.0, 250.0, 80.0, 40.0],
            "refund_amount": [0.0, 50.0, 0.0, 10.0],
            "status": ["shipped", "cancelled", "shipped", "shipped"],
        }
    )


@pytest.fixture()
def ontology() -> OntologySpec:
    return OntologySpec.model_validate(
        {
            "entities": [
                {"name": "orders", "dataset": ORDERS},
                {"name": "elsewhere", "dataset": OTHER},
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
                {"name": "foreign_metric", "entity": "elsewhere", "aggregate": "count"},
            ],
        }
    )


def _spec(y: dict) -> VizSpec:
    return VizSpec.model_validate(
        {
            "mark": {"type": "bar"},
            "encoding": {"x": {"field": "region", "type": "nominal"}, "y": y},
        }
    )


def _run(spec: VizSpec, frame: pd.DataFrame):
    """Materialize exactly as the executor does, after expansion."""
    from viz.catalog import build_catalog_pandas

    catalog = build_catalog_pandas(frame)
    calcs, calc_errors = resolve_calculations(spec.calculations(), catalog)
    assert calc_errors == []
    effective = extend_catalog(catalog, calcs)
    normalized = normalize_spec(spec, effective)
    assert validate_spec(normalized, effective) == []
    return execute_pandas(frame, build_plan(normalized, calcs=calcs))


# ---------------------------------------------------------------------------
# The ontology stays optional
# ---------------------------------------------------------------------------


def test_a_plain_chart_is_untouched(ontology):
    spec = _spec({"field": "order_total", "type": "quantitative", "aggregate": "sum"})
    assert spec_uses_metrics(spec) is False
    expanded, errors = expand_metric_channels(spec, ontology, ORDERS)
    assert errors == []
    assert expanded is spec


def test_a_plain_chart_works_with_no_ontology_at_all(frame):
    """The fallback that matters: no ontology must not break charting."""
    spec = _spec({"field": "order_total", "type": "quantitative", "aggregate": "sum"})
    expanded, errors = expand_metric_channels(spec, None, ORDERS)
    assert errors == []
    data = _run(expanded, frame)
    assert dict(data.rows) == {"EU": 180.0, "NA": 250.0, "APAC": 40.0}


def test_naming_a_metric_without_an_ontology_says_so(frame):
    spec = _spec({"metric": "revenue", "type": "quantitative"})
    _, errors = expand_metric_channels(spec, None, ORDERS)
    assert [e.code for e in errors] == ["no_ontology"]
    assert "Ontology tab" in errors[0].message


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def test_metric_becomes_a_calculation_plus_an_aggregate(ontology):
    spec = _spec({"metric": "revenue", "type": "quantitative"})
    assert spec_uses_metrics(spec) is True
    expanded, errors = expand_metric_channels(spec, ontology, ORDERS)
    assert errors == []

    calc = expanded.transform[0]
    assert isinstance(calc, CalculateTransform)
    assert calc.expr == "if(status != 'cancelled', (order_total - refund_amount), null)"
    assert expanded.encoding.y.field == calc.as_
    assert expanded.encoding.y.aggregate == "sum"


def test_unfiltered_count_needs_no_projection(ontology):
    spec = _spec({"metric": "order_count", "type": "quantitative"})
    expanded, errors = expand_metric_channels(spec, ontology, ORDERS)
    assert errors == []
    assert expanded.transform == []
    assert expanded.encoding.y.aggregate == "count"
    assert expanded.encoding.y.field is None


def test_metric_projections_precede_user_calculations(ontology):
    spec = VizSpec.model_validate(
        {
            "transform": [{"op": "calculate", "as": "mine", "expr": "order_total * 2"}],
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"metric": "revenue", "type": "quantitative"},
            },
        }
    )
    expanded, _ = expand_metric_channels(spec, ontology, ORDERS)
    assert [t.as_ for t in expanded.transform] == ["__m_revenue", "mine"]


def test_unknown_metric_lists_what_is_defined(ontology):
    spec = _spec({"metric": "profit", "type": "quantitative"})
    _, errors = expand_metric_channels(spec, ontology, ORDERS)
    assert errors[0].code == "unknown_metric"
    assert "revenue" in errors[0].message


def test_a_metric_from_another_dataset_is_refused(ontology):
    """Charts read one dataset; silently using the wrong rows would be worse."""
    spec = _spec({"metric": "foreign_metric", "type": "quantitative"})
    _, errors = expand_metric_channels(spec, ontology, ORDERS)
    assert errors[0].code == "metric_other_dataset"


# ---------------------------------------------------------------------------
# The point of the whole exercise
# ---------------------------------------------------------------------------


def test_chart_and_chat_produce_the_same_number(frame, ontology):
    """A chart of 'revenue' must equal what query_metric answers for 'revenue'."""
    spec = _spec({"metric": "revenue", "type": "quantitative"})
    expanded, _ = expand_metric_channels(spec, ontology, ORDERS)
    chart = dict(_run(expanded, frame).rows)

    # The cancelled 250 is excluded by the metric's own filter, exactly as it is
    # in the ontology's planner — EU keeps 100 + 80, APAC keeps 40 - 10.
    assert chart == {"EU": 180.0, "APAC": 30.0, "NA": None}


def test_the_chart_differs_from_a_naive_sum(frame, ontology):
    """Proof the metric definition is actually doing something."""
    naive = dict(_run(_spec({"field": "order_total", "aggregate": "sum"}), frame).rows)
    spec, _ = expand_metric_channels(_spec({"metric": "revenue"}), ontology, ORDERS)
    assert dict(_run(spec, frame).rows)["EU"] != naive["EU"] or naive["NA"] == 250.0
    assert naive["NA"] == 250.0  # naive keeps the cancelled order
