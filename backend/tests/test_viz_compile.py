"""Pandas engine golden tests for the viz compiler pipeline."""

from __future__ import annotations

import pandas as pd
import pytest

from viz.catalog import build_catalog_pandas
from viz.compile_pandas import execute_pandas
from viz.plan import build_plan
from viz.spec import VizSpec
from viz.validate import normalize_spec, validate_spec


@pytest.fixture()
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_date": pd.to_datetime(
                [
                    "2024-01-05",
                    "2024-01-20",
                    "2024-02-10",
                    "2024-02-15",
                    "2024-03-01",
                    "2024-03-02",
                    "2024-03-20",
                    None,
                ]
            ),
            "region": ["EU", "NA", "EU", "APAC", "NA", "EU", None, "EU"],
            "item": ["a", "b", "a", "c", "b", "b", "a", "a"],
            "revenue": [100.0, 250.5, 80.0, None, 40.0, 10.0, 5.0, 60.0],
            "qty": [1, 2, 3, 4, 5, 6, 7, 8],
        }
    )


@pytest.fixture()
def catalog(df):
    return build_catalog_pandas(df)


def run(df, catalog, spec_dict):
    spec = normalize_spec(VizSpec.model_validate(spec_dict), catalog)
    assert validate_spec(spec, catalog) == [], validate_spec(spec, catalog)
    return execute_pandas(df, build_plan(spec))


def test_bar_sum_by_region(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert [c.name for c in out.columns] == ["x", "y"]
    # Sorted by measure desc; APAC has only-null revenue → None, NULLS LAST.
    assert out.rows == [["EU", 250.0], ["NA", 290.5]] or out.rows == [
        ["NA", 290.5],
        ["EU", 250.0],
        ["APAC", None],
    ]
    # exact expectation: NA 290.5 > EU 250.0 > APAC None
    assert out.rows[0] == ["NA", 290.5]
    assert out.rows[1] == ["EU", 250.0]
    assert out.rows[2] == ["APAC", None]
    assert out.meta["engine"] == "pandas"


def test_monthly_line(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert out.rows == [
        ["2024-01-01T00:00:00", 350.5],
        ["2024-02-01T00:00:00", 80.0],
        ["2024-03-01T00:00:00", 55.0],
    ]


def test_multi_series_month_by_region(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "line"},
            "encoding": {
                "x": {"field": "order_date", "type": "temporal", "timeUnit": "month"},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    assert [c.name for c in out.columns] == ["x", "color", "y"]
    # x asc, then color asc tiebreak; null region + null date rows dropped.
    assert out.rows == [
        ["2024-01-01T00:00:00", "EU", 1.0],
        ["2024-01-01T00:00:00", "NA", 2.0],
        ["2024-02-01T00:00:00", "APAC", 4.0],
        ["2024-02-01T00:00:00", "EU", 3.0],
        ["2024-03-01T00:00:00", "EU", 6.0],
        ["2024-03-01T00:00:00", "NA", 5.0],
    ]


def test_count_when_no_measure(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    assert out.rows == [["EU", 4], ["NA", 2], ["APAC", 1]]


def test_histogram_bins(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "histogram"},
            "encoding": {"x": {"field": "revenue", "type": "quantitative", "bin": {"maxbins": 5}}},
        },
    )
    # revenue non-null: 5..250.5, width = 49.1
    width = out.meta["bins"]["x"]["width"]
    assert width == pytest.approx(49.1)
    # bins ascend; every non-null revenue lands in exactly one bin.
    assert sum(r[1] for r in out.rows) == 7
    starts = [r[0] for r in out.rows]
    assert starts == sorted(starts)
    assert starts[0] == pytest.approx(5.0)


def test_topk_others_reaggregates_mean(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 1, "others": True}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "mean"},
            },
        },
    )
    rows = {r[0]: r[1] for r in out.rows}
    assert set(rows) == {"a", "Other (remaining)"}
    # 'a' rows qty: 1,3,7,8 → mean 4.75 ; others (b:2,5,6 c:4) → mean 4.25
    assert rows["a"] == pytest.approx(4.75)
    assert rows["Other (remaining)"] == pytest.approx(4.25)


def test_topk_real_other_does_not_merge_with_remainder():
    frame = pd.DataFrame({"item": ["Other", "A", "B"], "qty": [5, 4, 3]})
    own_catalog = build_catalog_pandas(frame)
    out = run(
        frame,
        own_catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 1, "others": True}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert {row[0]: row[1] for row in out.rows} == {
        "Other": 5.0,
        "Other (remaining)": 7.0,
    }


def test_multiple_topk_dimensions_rank_independently():
    # x=A is the strongest x group, while color=u wins globally across B+C.
    # Independent ranking therefore has no row in the intersection. Sequential
    # ranking would incorrectly pick color=v after first filtering to x=A.
    frame = pd.DataFrame(
        {"segment": ["A", "B", "C"], "kind": ["v", "u", "u"], "value": [100, 60, 60]}
    )
    own_catalog = build_catalog_pandas(frame)
    out = run(
        frame,
        own_catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "segment", "type": "nominal", "topK": {"n": 1}},
                "y": {"field": "value", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "kind", "type": "nominal", "topK": {"n": 1}},
            },
        },
    )
    assert out.rows == []


def test_all_null_histogram_metadata_is_json_safe():
    import json

    frame = pd.DataFrame({"value": pd.Series([None, None], dtype="float64")})
    own_catalog = build_catalog_pandas(frame)
    out = run(
        frame,
        own_catalog,
        {
            "mark": {"type": "histogram"},
            "encoding": {"x": {"field": "value", "type": "quantitative"}},
        },
    )
    assert out.rows == []
    assert out.meta["bins"]["x"] == {"min": None, "max": None, "width": None}
    json.dumps(out.model_dump(), allow_nan=False)


def test_topk_without_others_drops_tail(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "topK": {"n": 2, "others": False}},
                "y": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert {r[0] for r in out.rows} == {"a", "b"}


@pytest.mark.parametrize(
    ("predicate", "value", "expected_regions"),
    [
        ("eq", "EU", {"EU"}),
        ("neq", "EU", {"NA", "APAC"}),
        ("in", ["EU", "NA"], {"EU", "NA"}),
        ("notin", ["EU"], {"NA", "APAC"}),
        ("contains", "a", {"NA", "APAC"}),
    ],
)
def test_filters_on_text(df, catalog, predicate, value, expected_regions):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "region", "predicate": predicate, "value": value}
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    assert {r[0] for r in out.rows} == expected_regions


def test_numeric_and_null_filters(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "qty", "predicate": "between", "value": [2, 5]},
                {"op": "filter", "field": "revenue", "predicate": "notnull"},
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    # qty in [2..5] and revenue not null → rows qty 2(NA),3(EU),5(NA)
    assert dict((r[0], r[1]) for r in out.rows) == {"NA": 2, "EU": 1}


def test_numeric_in_filter_matches_numerically(df, catalog):
    # Membership on numeric fields compares numerically: 5.0 must match the
    # int qty 5 (SQL float8::text renders "5", pandas str() renders "5.0" —
    # text comparison would silently diverge between engines).
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "qty", "predicate": "in", "value": [5.0, 7, "2"]}
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    # qty 5 (NA), 7 (None region → dropped key), 2 (NA)
    assert dict((r[0], r[1]) for r in out.rows) == {"NA": 2}

    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "qty", "predicate": "notin", "value": [5.0, 7, "2"]}
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    # remaining qty 1,3,4,6,8 → EU×4, APAC×1
    assert dict((r[0], r[1]) for r in out.rows) == {"EU": 4, "APAC": 1}


def test_temporal_filter(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "filter", "field": "order_date", "predicate": "gte", "value": "2024-03-01"}
            ],
            "encoding": {"x": {"field": "item", "type": "nominal"}},
        },
    )
    assert dict((r[0], r[1]) for r in out.rows) == {"b": 2, "a": 1}


def test_scatter_raw(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative"},
                "y": {"field": "revenue", "type": "quantitative"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    # Raw pairs; rows with null revenue dropped (y required); qty=4 row gone.
    assert len(out.rows) == 7
    assert [c.name for c in out.columns] == ["x", "y", "color"]
    assert all(isinstance(r[0], float) and isinstance(r[1], float) for r in out.rows)


def test_scatter_aggregates_measures_by_color(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative", "aggregate": "sum"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
                "color": {"field": "region", "type": "nominal"},
            },
        },
    )
    assert [c.name for c in out.columns] == ["color", "x", "y"]
    assert {row[0]: row[1:] for row in out.rows} == {
        "APAC": [4.0, None],
        "EU": [18.0, 250.0],
        "NA": [7.0, 290.5],
    }


def test_distinct_aggregate(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "item", "type": "nominal", "aggregate": "distinct"},
            },
        },
    )
    assert dict((r[0], r[1]) for r in out.rows) == {"EU": 2, "NA": 1, "APAC": 1}


def test_heatmap_counts(df, catalog):
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "heatmap"},
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "item", "type": "nominal"},
                "color": {"aggregate": "count", "type": "quantitative"},
            },
        },
    )
    assert [c.name for c in out.columns] == ["x", "y", "color"]
    as_map = {(r[0], r[1]): r[2] for r in out.rows}
    assert as_map[("EU", "a")] == 3
    assert as_map[("NA", "b")] == 2


def test_row_cap_and_truncated_flag(catalog):
    big = pd.DataFrame({"qty": range(30000), "revenue": [1.0] * 30000})
    cat = build_catalog_pandas(big)
    out = run(
        big,
        cat,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative"},
                "y": {"field": "revenue", "type": "quantitative"},
            },
        },
    )
    assert len(out.rows) == 10000
    assert out.meta["truncated"] is True


def test_exact_row_cap_is_not_reported_as_truncated():
    exact = pd.DataFrame({"qty": range(10000), "revenue": [1.0] * 10000})
    cat = build_catalog_pandas(exact)
    out = run(
        exact,
        cat,
        {
            "mark": {"type": "scatter"},
            "encoding": {
                "x": {"field": "qty", "type": "quantitative"},
                "y": {"field": "revenue", "type": "quantitative"},
            },
        },
    )
    assert len(out.rows) == 10000
    assert out.meta["truncated"] is False


# ---------------------------------------------------------------------------
# Calculated fields (transform op="calculate")
# ---------------------------------------------------------------------------


def run_with_calcs(df, catalog, spec_dict):
    from viz.expr import extend_catalog, resolve_calculations

    spec = VizSpec.model_validate(spec_dict)
    calcs, calc_errors = resolve_calculations(spec.calculations(), catalog)
    assert calc_errors == [], calc_errors
    eff = extend_catalog(catalog, calcs)
    spec = normalize_spec(spec, eff)
    assert validate_spec(spec, eff) == [], validate_spec(spec, eff)
    return execute_pandas(df, build_plan(spec, calcs=calcs))


def test_calc_measure_aggregates(df, catalog):
    out = run_with_calcs(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [{"op": "calculate", "as": "rev2", "expr": "revenue * 2"}],
            "encoding": {
                "x": {"field": "region", "type": "nominal"},
                "y": {"field": "rev2", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert out.rows[0] == ["NA", 581.0]  # (250.5 + 40) * 2
    assert out.rows[1] == ["EU", 500.0]


def test_calc_dimension_via_if(df, catalog):
    out = run_with_calcs(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "calculate", "as": "size_band", "expr": "if(qty >= 5, 'large', 'small')"}
            ],
            "encoding": {"x": {"field": "size_band", "type": "nominal"}},
        },
    )
    assert dict((r[0], r[1]) for r in out.rows) == {"large": 4, "small": 4}


def test_filter_on_calc_field(df, catalog):
    out = run_with_calcs(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "transform": [
                {"op": "calculate", "as": "rev_per_qty", "expr": "revenue / qty"},
                {"op": "filter", "field": "rev_per_qty", "predicate": "gte", "value": 20},
            ],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        },
    )
    # rev/qty: EU 100, NA 125.25, EU 26.67, NA 8, EU 1.67, a 0.71, EU 7.5
    assert dict((r[0], r[1]) for r in out.rows) == {"EU": 2, "NA": 1}


def test_sort_by_encoding_orders_bars_by_measure(df, catalog):
    # item sums: a=245.0, b=300.5, c=None(all-null). By value desc → b, a, c (NULLS LAST).
    out = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {
                    "field": "item",
                    "type": "nominal",
                    "sort": {"encoding": "y", "order": "desc"},
                },
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert [r[0] for r in out.rows] == ["b", "a", "c"]
    assert out.rows[0] == ["b", 300.5]


def test_plain_sort_still_orders_dimension_by_its_own_label(df, catalog):
    # A bare "asc"/"desc" keeps the by-label behavior (alphabetical), unchanged.
    asc = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "sort": "asc"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert [r[0] for r in asc.rows] == ["a", "b", "c"]
    desc = run(
        df,
        catalog,
        {
            "mark": {"type": "bar"},
            "encoding": {
                "x": {"field": "item", "type": "nominal", "sort": "desc"},
                "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
            },
        },
    )
    assert [r[0] for r in desc.rows] == ["c", "b", "a"]


def test_sort_by_encoding_missing_target_is_rejected(catalog):
    # Sorting by an unset channel would silently no-op — validation must catch it.
    spec = normalize_spec(
        VizSpec.model_validate(
            {
                "mark": {"type": "bar"},
                "encoding": {
                    "x": {
                        "field": "item",
                        "type": "nominal",
                        "sort": {"encoding": "size", "order": "desc"},
                    },
                    "y": {"field": "revenue", "type": "quantitative", "aggregate": "sum"},
                },
            }
        ),
        catalog,
    )
    errors = validate_spec(spec, catalog)
    assert any(e.code == "sort_target_missing" for e in errors), errors


def test_calc_validation_errors_reported():
    from viz.expr import resolve_calculations
    from viz.spec import VizSpec as VS

    spec = VS.model_validate(
        {
            "mark": {"type": "bar"},
            "transform": [{"op": "calculate", "as": "bad", "expr": "revenue +"}],
            "encoding": {"x": {"field": "region", "type": "nominal"}},
        }
    )
    d = pd.DataFrame({"region": ["a"], "revenue": [1.0]})
    cat = build_catalog_pandas(d)
    _, errors = resolve_calculations(spec.calculations(), cat)
    assert len(errors) == 1 and errors[0].code == "expr_invalid"
    assert errors[0].path == "transform[0].expr"
