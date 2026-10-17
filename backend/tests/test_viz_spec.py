"""VizSpec schema, normalization, and semantic validation tests."""

from __future__ import annotations

import pandas as pd
import pytest

from viz.catalog import build_catalog_pandas
from viz.spec import VizSpec
from viz.validate import normalize_spec, validate_spec


@pytest.fixture()
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_date": pd.to_datetime(
                ["2024-01-05", "2024-01-20", "2024-02-10", "2024-02-15", "2024-03-01"]
            ),
            "region": ["EU", "NA", "EU", "APAC", "NA"],
            "revenue": [100.0, 250.5, 80.0, None, 40.0],
            "qty": [1, 2, 3, 4, 5],
        }
    )


@pytest.fixture()
def catalog(df):
    return build_catalog_pandas(df)


def _spec(**overrides) -> VizSpec:
    base = {
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
        },
    }
    base.update(overrides)
    return VizSpec.model_validate(base)


# ---------------------------------------------------------------------------
# Schema / wire format
# ---------------------------------------------------------------------------


def test_alias_round_trip():
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal", "topK": {"n": 3}},
            },
        }
    )
    assert spec.encoding.x.time_unit == "month"
    assert spec.encoding.color.top_k.n == 3
    wire = spec.to_json_dict()
    assert wire["encoding"]["x"]["timeUnit"] == "month"
    assert wire["encoding"]["color"]["topK"]["n"] == 3
    assert wire["schema_version"] == 1


def test_extra_keys_rejected():
    with pytest.raises(Exception):
        VizSpec.model_validate({"mark": {"type": "bar"}, "bogus": 1})


def test_unknown_schema_version_rejected():
    with pytest.raises(Exception):
        VizSpec.model_validate({"schema_version": 2, "mark": {"type": "bar"}, "encoding": {}})


def test_bad_enum_rejected():
    with pytest.raises(Exception):
        _spec(mark={"type": "treemap"})


# ---------------------------------------------------------------------------
# Catalog inference
# ---------------------------------------------------------------------------


def test_catalog_types(catalog):
    by_name = {f.name: f for f in catalog.fields}
    assert by_name["order_date"].type == "temporal"
    assert by_name["region"].type == "nominal"
    assert by_name["revenue"].type == "quantitative"
    assert by_name["revenue"].role == "measure"
    assert by_name["region"].role == "dimension"
    assert by_name["revenue"].null_pct == 20.0


def test_catalog_fingerprint_changes_on_imputation(df):
    fp1 = build_catalog_pandas(df).fingerprint
    df2 = df.copy()
    df2["revenue"] = df2["revenue"].fillna(0)
    assert build_catalog_pandas(df2).fingerprint != fp1


def test_catalog_detects_text_dates():
    df = pd.DataFrame({"d": ["2024-01-01", "2024-02-01", "2024-03-01"], "v": [1, 2, 3]})
    cat = build_catalog_pandas(df)
    assert cat.get("d").type == "temporal"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_histogram_gets_bin_and_count(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "histogram"},
            "encoding": {"x": {"field": "revenue", "type": "quantitative"}},
        }
    )
    norm = normalize_spec(spec, catalog)
    assert norm.encoding.x.bin is not None
    assert norm.encoding.y is not None and norm.encoding.y.aggregate == "count"
    assert validate_spec(norm, catalog) == []


def test_normalize_syncs_channel_type_from_catalog(catalog):
    spec = _spec()
    spec.encoding.x.type = "quantitative"  # wrong on purpose — region is nominal
    norm = normalize_spec(spec, catalog)
    assert norm.encoding.x.type == "nominal"


def test_normalize_adds_count_when_dims_only(catalog):
    spec = VizSpec.model_validate(
        {"mark": {"type": "bar"}, "encoding": {"x": {"field": "region", "type": "nominal"}}}
    )
    norm = normalize_spec(spec, catalog)
    assert norm.encoding.y is not None and norm.encoding.y.aggregate == "count"
    assert validate_spec(norm, catalog) == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _codes(errors):
    return {e.code for e in errors}


def test_unknown_field_suggests(catalog):
    spec = _spec()
    spec.encoding.x.field = "regian"
    errors = validate_spec(spec, catalog)
    assert "unknown_field" in _codes(errors)
    assert "region" in errors[0].message


def test_aggregate_on_text_rejected(catalog):
    spec = _spec()
    spec.encoding.y.field = "region"
    errors = validate_spec(normalize_spec(spec, catalog), catalog)
    assert "aggregate_type_mismatch" in _codes(errors)


def test_aggregate_type_mismatch_says_the_column_must_change(catalog):
    """The message has to close the door on respecifying.

    Warehouse sources routinely type numeric columns as VARCHAR. An author that
    reads this as an ordinary spec error retries until it exhausts its budget
    and then quietly answers a different question — swapping count for sum. The
    message must say the data is the problem, not the chart.
    """
    spec = _spec()
    spec.encoding.y.field = "region"
    errors = validate_spec(normalize_spec(spec, catalog), catalog)
    message = next(e.message for e in errors if e.code == "aggregate_type_mismatch")
    assert "region" in message
    assert "no edit to the chart" in message.lower()


def test_timeunit_on_numeric_rejected(catalog):
    spec = _spec()
    spec.encoding.x = spec.encoding.x.model_copy(
        update={"field": "qty", "time_unit": "month", "type": "quantitative"}
    )
    errors = validate_spec(normalize_spec(spec, catalog), catalog)
    assert "timeunit_type_mismatch" in _codes(errors)


def test_faceting_rejected(catalog):
    spec = _spec()
    spec.encoding.row = spec.encoding.x.model_copy()
    assert "unsupported_channel" in _codes(validate_spec(spec, catalog))


def test_topk_on_temporal_rejected(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {
                    "field": "order_date",
                    "type": "temporal",
                    "timeUnit": "month",
                    "topK": {"n": 3},
                },
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        }
    )
    assert "topk_on_continuous" in _codes(validate_spec(spec, catalog))


def test_topk_without_measure_rejected(catalog):
    # A raw (non-aggregated) spec with topK on a dimension has no measure to rank
    # by; the SQL compiler would crash on it (plan.measures[0]) and pandas would
    # silently ignore it. Validation must reject it instead.
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "revenue", "type": "quantitative"},
                "y": {"field": "qty", "type": "quantitative"},
                "color": {"field": "region", "type": "nominal", "topK": {"n": 3}},
            },
        }
    )
    assert "topk_needs_measure" in _codes(validate_spec(normalize_spec(spec, catalog), catalog))


def test_pie_rules(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "pie"},
            "encoding": {"y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"}},
        }
    )
    assert "pie_needs_dimension" in _codes(validate_spec(spec, catalog))


def test_heatmap_rules(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "heatmap"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
            },
        }
    )
    errors = validate_spec(spec, catalog)
    assert "heatmap_needs_two_dims" in _codes(errors)
    assert "heatmap_needs_measure_color" in _codes(errors)


def test_color_binding_must_match_encoded_field(catalog):
    spec = _spec(
        config={
            "color": {
                "mode": "categorical",
                "binding": {"channel": "x", "field": "order_date"},
            }
        }
    )
    assert "color_binding_field_mismatch" in _codes(validate_spec(spec, catalog))


def test_continuous_color_requires_measure_binding(catalog):
    spec = _spec(
        config={
            "color": {
                "mode": "continuous",
                "binding": {"channel": "color", "field": "region"},
                "palette": ["#FFFFFF", "#2563EB"],
            }
        },
        encoding={
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            "color": {"field": "region", "type": "nominal"},
        },
    )
    assert "continuous_color_needs_measure" in _codes(validate_spec(spec, catalog))


def test_two_measure_axes_rejected(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        }
    )
    assert "two_measure_axes" in _codes(validate_spec(spec, catalog))


def test_scatter_accepts_aggregated_measure_axes(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "mean"},
                "color": {"field": "region", "type": "nominal"},
            },
        }
    )
    assert validate_spec(spec, catalog) == []


def test_scatter_rejects_mixed_raw_and_aggregate_axes(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "y": {"field": "revenue", "type": "quantitative"},
            },
        }
    )
    assert "scatter_numeric_axes" in _codes(validate_spec(spec, catalog))


@pytest.mark.parametrize(
    ("mark", "encoding"),
    [
        (
            "bar",
            {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
                "size": {"field": "qty", "type": "quantitative"},
            },
        ),
        (
            "line",
            {
                "x": {"field": "order_date", "type": "temporal"},
                "y": {"field": "revenue", "type": "quantitative"},
            },
        ),
        (
            "scatter",
            {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "qty", "type": "quantitative"},
            },
        ),
        (
            "heatmap",
            {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "qty", "type": "quantitative"},
                "color": {"field": "revenue", "type": "quantitative"},
            },
        ),
    ],
)
def test_rejects_channel_shapes_renderer_cannot_consume(catalog, mark, encoding):
    spec = normalize_spec(
        VizSpec.model_validate({"mark": {"type": mark}, "encoding": encoding}), catalog
    )
    assert _codes(validate_spec(spec, catalog)) & {
        "invalid_channel_for_mark",
        "heatmap_needs_measure_color",
        "scatter_numeric_axes",
    }


def test_horizontal_bar_normalizes_implicit_count(catalog):
    spec = VizSpec.model_validate(
        {
            "mark": {"type": "bar"},
            "encoding": {"y": {"field": "region", "type": "nominal"}},
        }
    )
    normalized = normalize_spec(spec, catalog)
    assert normalized.encoding.x is not None
    assert normalized.encoding.x.aggregate == "count"
    assert validate_spec(normalized, catalog) == []


def test_topk_rejects_numeric_dimensions(catalog):
    spec = normalize_spec(
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {"field": "qty", "type": "quantitative", "topK": {"n": 2}},
                    "y": {"aggregate": "count", "type": "quantitative"},
                },
            }
        ),
        catalog,
    )
    assert "topk_on_continuous" in _codes(validate_spec(spec, catalog))


def test_aggregated_tooltip_rejected_instead_of_silently_dropped(catalog):
    spec = _spec()
    spec.encoding.tooltip = [spec.encoding.x.model_copy()]
    assert "unsupported_aggregated_tooltip" in _codes(validate_spec(spec, catalog))


@pytest.mark.parametrize(
    ("mark", "option"),
    [("bar", "smooth"), ("pie", "point"), ("line", "stacked")],
)
def test_hidden_mark_options_are_rejected(catalog, mark, option):
    spec = _spec(mark={"type": mark, option: True})
    assert "invalid_mark_option" in _codes(validate_spec(normalize_spec(spec, catalog), catalog))


def test_filter_validation(catalog):
    spec = _spec(
        transform=[{"op": "filter", "field": "region", "predicate": "between", "value": [1]}]
    )
    assert "bad_between" in _codes(validate_spec(spec, catalog))
    spec2 = _spec(transform=[{"op": "filter", "field": "region", "predicate": "gt", "value": 5}])
    assert "range_on_text" in _codes(validate_spec(spec2, catalog))
    spec3 = _spec(transform=[{"op": "filter", "field": "region", "predicate": "in", "value": "EU"}])
    assert "value_not_list" in _codes(validate_spec(spec3, catalog))


def test_valid_spec_has_no_errors(catalog):
    assert validate_spec(normalize_spec(_spec(), catalog), catalog) == []
