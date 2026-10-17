"""The MongoDB filter page: pipeline construction and the shared page shape.

Pipeline *shape* is asserted here; that a real server accepts and agrees with it
is covered by tests/integration/test_mongo_scratch.py.
"""

from __future__ import annotations

import pytest

from filters.mongo_service import (
    GROUP_FIELD,
    MATCH_FIELD,
    build_narrow_expr,
    build_page_pipeline,
    build_pipeline,
    build_summary_pipeline,
)
from filters.spec import ComparePred, FilterSpec, Presentation, PresentRule

COLUMNS = ["_id", "sku", "qty"]


def _spec(**present) -> FilterSpec:
    return FilterSpec(
        id="f1",
        name="f",
        source={"kind": "sql", "id": "x"},
        select=ComparePred(op="gt", column="qty", value=2),
        present=Presentation(**present),
    )


def test_select_becomes_a_computed_flag_not_a_filter():
    """One pass has to serve both the summary counts and the page, so the match
    is a field rather than a $match."""
    stages = build_pipeline(_spec(), COLUMNS)
    assert list(stages[0]) == ["$addFields"]
    assert MATCH_FIELD in stages[0]["$addFields"]


def test_a_rule_without_its_own_predicate_follows_the_select():
    spec = _spec(rules=[PresentRule(id="r1", target="row")])
    added = build_pipeline(spec, COLUMNS)[0]["$addFields"]
    assert added["__stych_r0"] == added[MATCH_FIELD]


def test_a_rule_with_a_predicate_is_evaluated_independently():
    spec = _spec(
        rules=[
            PresentRule(
                id="r1", target="row", predicate=ComparePred(op="eq", column="sku", value="A")
            )
        ]
    )
    added = build_pipeline(spec, COLUMNS)[0]["$addFields"]
    assert added["__stych_r0"] != added[MATCH_FIELD]


def test_summary_counts_every_rule_over_the_whole_collection():
    spec = _spec(rules=[PresentRule(id="a"), PresentRule(id="b")])
    group = build_summary_pipeline(spec, COLUMNS)[-1]["$group"]
    assert group["total"] == {"$sum": 1}
    assert "__stych_r0" in group and "__stych_r1" in group


def test_grouping_awaits_the_annotate_stage():
    """group_by names an annotation output, and MongoDB has no annotate stage
    yet — so no grouping field is produced and row order stays identity-based."""
    stages = build_pipeline(_spec(), COLUMNS)
    assert GROUP_FIELD not in stages[0]["$addFields"]


def test_sorting_falls_back_to_document_identity():
    """_id makes paging deterministic without asking the user for a key."""
    stages = build_page_pipeline(_spec(), COLUMNS, offset=0, limit=10)
    assert next(s["$sort"] for s in stages if "$sort" in s) == {"_id": 1}


def test_a_user_sort_is_honoured_and_tie_broken_by_identity():
    stages = build_page_pipeline(
        _spec(), COLUMNS, offset=20, limit=10, sort_by="qty", sort_dir="desc"
    )
    assert next(s["$sort"] for s in stages if "$sort" in s) == {"qty": -1, "_id": 1}
    assert {"$skip": 20} in stages and {"$limit": 10} in stages


def test_matches_only_filters_on_the_computed_flag():
    stages = build_page_pipeline(
        _spec(),
        COLUMNS,
        offset=0,
        limit=10,
        narrow=build_narrow_expr(_spec(), COLUMNS, matches_only=True),
    )
    assert {"$match": {"$expr": f"${MATCH_FIELD}"}} in stages


def test_a_predicate_over_an_unsampled_field_fails_loudly():
    """Collections are schemaless, so a field can be absent from the sample that
    defined the column set. Silently matching nothing would look like a real
    answer; raising says the column is unknown."""
    from filters.predicate_pandas import UnknownColumn

    with pytest.raises(UnknownColumn):
        build_pipeline(_spec(), ["_id", "sku"])  # the spec filters on "qty"


def test_internal_flag_names_cannot_collide_with_customer_fields():
    added = build_pipeline(_spec(rules=[PresentRule(id="r")]), COLUMNS)[0]["$addFields"]
    assert all(name.startswith("__stych_") for name in added)


def test_every_generated_pipeline_passes_the_guard():
    """A read pipeline must never trip the write guard."""
    from agents.mongo_agent.pipeline_guard import validate_pipeline

    spec = _spec(rules=[PresentRule(id="r")])
    for stages in (
        build_summary_pipeline(spec, COLUMNS),
        build_page_pipeline(
            spec,
            COLUMNS,
            offset=0,
            limit=10,
            narrow=build_narrow_expr(spec, COLUMNS, matches_only=True),
        ),
    ):
        validate_pipeline(
            stages, scratch_db="stych_temp", scratch_prefix="stych_", running_db="analytics"
        )


def test_grouped_pages_sort_null_groups_last():
    spec = FilterSpec.model_construct(
        id="f1",
        name="f",
        source={"kind": "sql", "id": "x"},
        present=Presentation(group_by="dup_group"),
    )
    stages = build_page_pipeline(spec, [*COLUMNS, "dup_group"], offset=0, limit=10)
    assert {"$sort": {"__stych_g_null": 1, GROUP_FIELD: 1, "_id": 1}} in stages


def test_dollar_prefixed_viz_filter_values_are_literals():
    from types import SimpleNamespace

    from viz.mongo_expr import _comparable

    catalog = SimpleNamespace(get=lambda _fld: SimpleNamespace(type="nominal"))
    _left, lit = _comparable("promo", "nominal", "$100off", catalog, "$promo")
    assert lit("$100off") == {"$literal": "$100off"}
    assert lit("$price") == {"$literal": "$price"}
