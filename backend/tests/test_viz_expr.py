"""Expression engine tests: parsing, typing, pandas semantics, SQL emission."""

from __future__ import annotations

import pandas as pd
import pytest

from viz.catalog import build_catalog_pandas
from viz.expr import (
    ExprError,
    calc_field_info,
    infer_type,
    materialize_pandas,
    materialize_sql,
    parse,
    resolve_calculations,
)
from viz.spec import CalculateTransform


@pytest.fixture()
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "revenue": [100.0, 250.0, None, 40.0],
            "cost": [60.0, 0.0, 10.0, None],
            "region": ["EU", "NA", None, "APAC"],
            "order_date": pd.to_datetime(["2024-01-05", "2024-02-20", "2024-03-10", None]),
        }
    )


@pytest.fixture()
def catalog(df):
    return build_catalog_pandas(df)


def resolver(catalog):
    return lambda name: (catalog.get(name).type if catalog.get(name) else None)


def typed(src: str, catalog):
    ast = parse(src)
    infer_type(ast, resolver(catalog))
    return ast


def evaluate(src: str, catalog, df):
    return materialize_pandas(typed(src, catalog), df)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "revenue - cost",
        "(revenue - cost) / revenue",
        "if(revenue > 100, 'big', 'small')",
        "coalesce(revenue, 0)",
        "[order_date] >= '2024-02-01'",
        "not (revenue > 10 and cost < 5)",
        "round(revenue / 3, 2)",
        "lower(region)",
    ],
)
def test_parses(src, catalog):
    assert typed(src, catalog) is not None


@pytest.mark.parametrize(
    "src",
    [
        "",
        "revenue +",
        "revenue ++ cost",
        "if(revenue > 1, 2)",  # arity
        "unknownfn(revenue)",
        "'unterminated",
        "[unclosed",
        "1.2.3",
    ],
)
def test_parse_errors(src, catalog):
    with pytest.raises(ExprError):
        typed(src, catalog)


@pytest.mark.parametrize(
    "src",
    [
        "revenue + region",  # arithmetic on text
        "region > 'a'",  # range compare on text
        "revenue and cost",  # logic on numbers
        "not revenue",
        "if(revenue, 1, 2)",  # non-boolean condition
        "if(revenue > 1, 1, 'x')",  # branch type mismatch
        "revenue == null",  # null comparison
        "lower(revenue)",
        "order_date + 1",  # temporal arithmetic
        "nope + 1",  # unknown field
    ],
)
def test_type_errors(src, catalog):
    with pytest.raises(ExprError):
        typed(src, catalog)


def test_type_inference(catalog):
    assert typed("revenue - cost", catalog).type_ == "quantitative"
    assert typed("revenue > cost", catalog).type_ == "boolean"
    assert typed("if(revenue > 1, 'a', 'b')", catalog).type_ == "nominal"
    assert typed("coalesce(region, 'none')", catalog).type_ == "nominal"
    assert typed("coalesce(order_date, order_date)", catalog).type_ == "temporal"
    assert typed("length(region)", catalog).type_ == "quantitative"


# ---------------------------------------------------------------------------
# Pandas semantics (the parity-critical bits)
# ---------------------------------------------------------------------------


def test_arithmetic_null_propagation(catalog, df):
    out = evaluate("revenue - cost", catalog, df)
    assert list(out[:2]) == [40.0, 250.0]
    assert out[2:].isna().all()  # null operand → null


def test_division_by_zero_is_null(catalog, df):
    out = evaluate("revenue / cost", catalog, df)
    assert out[0] == pytest.approx(100.0 / 60.0)
    assert pd.isna(out[1])  # 250 / 0 → null (SQL NULLIF parity)


def test_round_half_away_from_zero(catalog):
    d = pd.DataFrame({"revenue": [0.5, 1.5, 2.5, -0.5, -2.5, None], "cost": [0] * 6})
    cat = build_catalog_pandas(d)
    out = evaluate("round(revenue)", cat, d)
    assert list(out[:5]) == [1.0, 2.0, 3.0, -1.0, -3.0]  # NOT banker's rounding
    assert pd.isna(out[5])


def test_boolean_materializes_as_text(catalog, df):
    out = evaluate("revenue > 100", catalog, df)
    assert list(out[:2]) == ["false", "true"]
    assert pd.isna(out[2])  # null revenue → null, not 'false'


def test_comparison_null_preserved_through_logic(catalog, df):
    # NULL > 100 → NULL; NULL or TRUE → TRUE (Kleene), NULL or FALSE → NULL
    out = evaluate("revenue > 100 or cost >= 0", catalog, df)
    assert out[2] == "true"  # null or true
    out2 = evaluate("revenue > 100 and cost >= 0", catalog, df)
    assert pd.isna(out2[2])  # null and true → null


def test_if_and_coalesce(catalog, df):
    labels = evaluate("if(revenue >= 100, 'big', 'small')", catalog, df)
    assert list(labels[:2]) == ["big", "big"]
    assert labels[3] == "small"
    assert labels[2] == "small"  # null condition falls to else (SQL CASE parity)
    filled = evaluate("coalesce(revenue, 0)", catalog, df)
    assert filled[2] == 0.0


def test_temporal_comparison_with_string_literal(catalog, df):
    out = evaluate("order_date >= '2024-02-01'", catalog, df)
    assert list(out[:3]) == ["false", "true", "true"]
    assert pd.isna(out[3])


def test_string_functions(catalog, df):
    assert evaluate("upper(region)", catalog, df)[0] == "EU"
    assert evaluate("length(region)", catalog, df)[3] == 4.0


# ---------------------------------------------------------------------------
# SQL emission
# ---------------------------------------------------------------------------


def test_sql_numeric_field_and_null_safe_division(catalog):
    ast = typed("revenue / cost", catalog)
    sql = materialize_sql(ast, catalog)
    assert "NULLIF" in sql
    assert '"revenue"' in sql and '"cost"' in sql


def test_sql_string_literal_escaped(catalog):
    ast = typed("if(region == 'O''Brien', 'a', 'b')", catalog)
    sql = materialize_sql(ast, catalog)
    assert "'O''Brien'" in sql
    assert "CASE WHEN" in sql


def test_sql_boolean_materializes_as_text(catalog):
    sql = materialize_sql(typed("revenue > 100", catalog), catalog)
    assert "AS TEXT" in sql.upper()


def test_a_boolean_on_sql_server_keeps_null_out_of_the_false_bucket(catalog):
    """T-SQL has no boolean value, so the predicate resolves through a CASE.
    A plain ELSE would fold NULL into 'false', but a null dimension key is
    meant to drop its row."""
    from datasources.sql_dialect import use_dialect

    with use_dialect("sqlserver"):
        sql = materialize_sql(typed("revenue > 100", catalog), catalog)
    assert "'true'" in sql and "'false'" in sql
    assert "ELSE NULL" in sql


def test_sql_temporal_literal_cast(catalog):
    sql = materialize_sql(typed("order_date >= '2024-02-01'", catalog), catalog)
    assert "TIMESTAMP" in sql.upper()


def test_sql_round_keeps_its_digit_count(catalog):
    sql = materialize_sql(typed("round(revenue, 2)", catalog), catalog)
    assert "ROUND" in sql and ", 2)" in sql


# ---------------------------------------------------------------------------
# resolve_calculations
# ---------------------------------------------------------------------------


def calc(name: str, expr: str) -> CalculateTransform:
    return CalculateTransform.model_validate({"op": "calculate", "as": name, "expr": expr})


def test_resolve_ok_and_field_info(catalog):
    resolved, errors = resolve_calculations(
        [
            calc("margin", "(revenue - cost) / revenue"),
            calc("tier", "if(margin > 0.5, 'hi', 'lo')"),
        ],
        catalog,
    )
    assert errors == []
    assert [c.name for c in resolved] == ["margin", "tier"]
    info = calc_field_info(resolved[0])
    assert info.type == "quantitative" and info.role == "measure" and info.is_calculated
    assert calc_field_info(resolved[1]).role == "dimension"


def test_resolve_inlines_earlier_calcs(catalog, df):
    resolved, errors = resolve_calculations(
        [calc("margin", "revenue - cost"), calc("double_margin", "margin * 2")], catalog
    )
    assert errors == []
    # inlined: evaluable directly against the base frame
    out = materialize_pandas(resolved[1].ast, df)
    assert out[0] == 80.0


def test_resolve_errors(catalog):
    _, errors = resolve_calculations(
        [
            calc("revenue", "cost * 2"),  # collides with dataset column
            calc("", "1 + 1"),  # empty name
            calc("bad", "revenu + 1"),  # unknown field (typo)
            calc("worse", "region +"),  # syntax
            calc("nul", "null"),  # always-null
        ],
        catalog,
    )
    codes = [e.code for e in errors]
    assert codes == [
        "duplicate_field",
        "bad_calc_name",
        "expr_invalid",
        "expr_invalid",
        "expr_invalid",
    ]
    assert "Did you mean" in errors[1 + 1].message


def test_resolve_never_mutates_catalog(catalog):
    n = len(catalog.fields)
    resolved, _ = resolve_calculations([calc("m", "revenue * 2")], catalog)
    from viz.expr import extend_catalog

    eff = extend_catalog(catalog, resolved)
    assert len(catalog.fields) == n
    assert eff.get("m") is not None and eff.get("m").db_type == "double precision"
