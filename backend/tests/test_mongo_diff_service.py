"""The MongoDB diff: flattening, narrowing, and pipeline shape.

That a real server accepts these pipelines and agrees with them is covered by
tests/integration/test_mongo_diff.py; this covers what runs without one.
"""

from __future__ import annotations

import pytest

from api.mongo_diff_pipelines import (
    ADDED,
    CHANGED,
    REMOVED,
    build_added_pipeline,
    build_aligned_pipeline,
    build_cell_counts_pipeline,
    build_summary_pipeline,
)
from api.mongo_diff_service import (
    MongoDiffError,
    build_narrow_expr,
    deep_flatten,
    resolve_diff_target,
)

COLUMNS = ["_id", "sku", "cust.city"]


def test_deep_flatten_reaches_leaves_through_objects_and_arrays():
    assert deep_flatten({"a": {"b": 1}, "c": ["x", "y"]}) == {"a.b": 1, "c.0": "x", "c.1": "y"}


def test_deep_flatten_indexes_arrays_of_objects():
    assert deep_flatten({"items": [{"id": 1}, {"id": 2}]}) == {"items.0.id": 1, "items.1.id": 2}


def test_deep_flatten_keeps_a_scalar_at_its_own_path():
    assert deep_flatten({"qty": 3}) == {"qty": 3}


def test_an_empty_container_contributes_no_leaves():
    """An empty list has nothing to compare, so it must not invent a path."""
    assert deep_flatten({"tags": [], "meta": {}}) == {}


def test_no_narrowing_when_the_grid_asks_for_nothing():
    assert build_narrow_expr(COLUMNS) is None


def test_changes_only_accepts_every_kind_of_change():
    expr = build_narrow_expr(COLUMNS, changes_only=True)
    assert expr == {"$or": [f"${ADDED}", f"${REMOVED}", f"${CHANGED}"]}


@pytest.mark.parametrize(
    "kind,flag", [("added", ADDED), ("removed", REMOVED), ("changed", CHANGED)]
)
def test_change_kind_selects_one_flag(kind, flag):
    assert build_narrow_expr(COLUMNS, change_kind=kind) == f"${flag}"


def test_a_column_filter_is_a_literal_substring_not_a_regex():
    """The grid's per-column box is a substring box; a metacharacter must match
    itself rather than being interpreted."""
    expr = build_narrow_expr(COLUMNS, filters={"sku": "a.b"})
    assert expr["$regexMatch"]["regex"] == r"a\.b"


def test_a_filter_on_an_unsampled_column_is_ignored_rather_than_matching_nothing():
    assert build_narrow_expr(COLUMNS, filters={"not_sampled": "x"}) is None


def test_every_generated_pipeline_passes_the_write_guard():
    """A read pipeline must never trip the guard, and the diff reaches into a
    second collection — exactly the shape the guard scrutinises."""
    from agents.mongo_agent.pipeline_guard import validate_pipeline

    for stages in (
        build_summary_pipeline("stych_base", "stych_work"),
        build_added_pipeline("stych_base"),
        build_aligned_pipeline("stych_base", "stych_work"),
        build_cell_counts_pipeline("stych_base", "stych_work", COLUMNS),
    ):
        validate_pipeline(
            stages, scratch_db="stych_temp", scratch_prefix="stych_", running_db="stych_temp"
        )


def test_the_aligned_pipeline_unions_the_base_only_remainder():
    """A join from either side alone drops one of added/removed, so the removed
    documents must arrive through a union."""
    stages = build_aligned_pipeline("stych_base", "stych_work")
    union = next(s["$unionWith"] for s in stages if "$unionWith" in s)
    assert union["coll"] == "stych_base"


def test_an_unknown_variant_is_refused_rather_than_guessed():
    with pytest.raises(MongoDiffError, match="Unknown source variant"):
        resolve_diff_target({}, "sideways", "stych_temp")


def test_a_working_copy_that_does_not_exist_yet_says_so():
    with pytest.raises(MongoDiffError, match="No working copy exists yet"):
        resolve_diff_target({"source": "mongodb"}, "temp", "stych_temp")
