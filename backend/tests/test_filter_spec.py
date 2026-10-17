"""FilterSpec validation: shapes, defaults, and cross-field rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from filters.spec import parse_spec, validation_errors


def _minimal(**overrides):
    payload = {"name": "test", **overrides}
    return payload


def test_minimal_spec_parses():
    spec = parse_spec(_minimal())
    assert spec.version == 1
    assert spec.select is None
    assert spec.present.rules == []


def test_fuzzy_step_fills_default_outputs():
    spec = parse_spec(
        _minimal(
            annotate=[{"kind": "fuzzy_duplicates", "id": "dup", "on": "name", "threshold": 0.9}]
        )
    )
    names = [o.name for o in spec.annotate[0].outputs]
    assert names == ["dup_group", "dup_score", "dup_match_key"]
    roles = [o.role for o in spec.annotate[0].outputs]
    assert roles == ["group", "score", "ref"]


def test_duplicate_step_ids_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_spec(
            _minimal(
                annotate=[
                    {"kind": "duplicate_values", "id": "a", "columns": ["x"]},
                    {"kind": "duplicate_values", "id": "a", "columns": ["y"]},
                ]
            )
        )
    assert any("unique" in e["message"] for e in validation_errors(exc.value))


def test_output_name_collision_across_steps_rejected():
    with pytest.raises(ValidationError):
        parse_spec(
            _minimal(
                annotate=[
                    {
                        "kind": "pandas",
                        "id": "a",
                        "code": "x",
                        "outputs": [{"name": "score"}],
                    },
                    {
                        "kind": "pandas",
                        "id": "b",
                        "code": "y",
                        "outputs": [{"name": "score"}],
                    },
                ]
            )
        )


def test_group_by_must_be_declared_output():
    with pytest.raises(ValidationError) as exc:
        parse_spec(_minimal(present={"group_by": "nope"}))
    assert any("group_by" in e["message"] for e in validation_errors(exc.value))


def test_bad_regex_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_spec(_minimal(select={"op": "regex", "column": "x", "pattern": "("}))
    assert any("regex" in e["message"] for e in validation_errors(exc.value))


def test_unknown_strategy_rejected():
    with pytest.raises(ValidationError):
        parse_spec(
            _minimal(
                annotate=[{"kind": "fuzzy_duplicates", "id": "d", "on": "n", "strategy": "nope"}]
            )
        )


def test_null_compare_literal_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_spec(_minimal(select={"op": "eq", "column": "x", "value": None}))
    assert any("is_null" in e["message"] for e in validation_errors(exc.value))


def test_cells_rule_requires_columns():
    with pytest.raises(ValidationError):
        parse_spec(_minimal(present={"rules": [{"id": "r1", "target": "cells"}]}))


def test_multi_column_cells_rule_requires_explicit_predicate():
    """Defaulting to the row-level select predicate would paint every listed column
    even when only one of them is individually the reason the row matched."""
    with pytest.raises(ValidationError) as exc:
        parse_spec(
            _minimal(
                select={
                    "op": "or",
                    "children": [
                        {"op": "is_null", "column": "a"},
                        {"op": "is_null", "column": "b"},
                    ],
                },
                present={"rules": [{"id": "r1", "target": "cells", "columns": ["a", "b"]}]},
            )
        )
    assert any("explicit predicate" in e["message"] for e in validation_errors(exc.value))


def test_multi_column_cells_rule_with_explicit_predicate_parses():
    spec = parse_spec(
        _minimal(
            present={
                "rules": [
                    {
                        "id": "r1",
                        "target": "cells",
                        "columns": ["a", "b"],
                        "predicate": {"op": "gt", "column": "score", "value": 0.9},
                    }
                ]
            }
        )
    )
    assert spec.present.rules[0].columns == ["a", "b"]


def test_single_column_cells_rule_predicate_still_optional():
    spec = parse_spec(
        _minimal(present={"rules": [{"id": "r1", "target": "cells", "columns": ["a"]}]})
    )
    assert spec.present.rules[0].predicate is None


def test_reserved_row_key_output_rejected():
    with pytest.raises(ValidationError):
        parse_spec(
            _minimal(
                annotate=[
                    {
                        "kind": "pandas",
                        "id": "a",
                        "code": "x",
                        "outputs": [{"name": "__row_key__"}],
                    }
                ]
            )
        )


def test_nested_predicate_parses():
    spec = parse_spec(
        _minimal(
            select={
                "op": "and",
                "children": [
                    {"op": "gt", "column": "amount", "value": 100},
                    {
                        "op": "not",
                        "child": {"op": "is_null", "column": "email"},
                    },
                    {"op": "lt", "column": "start", "value": {"column": "end"}},
                ],
            }
        )
    )
    assert spec.select.op == "and"
    assert spec.select.children[2].value.column == "end"


def test_fingerprint_tracks_annotate_changes():
    a = parse_spec(_minimal(annotate=[{"kind": "duplicate_values", "id": "d", "columns": ["x"]}]))
    b = parse_spec(_minimal(annotate=[{"kind": "duplicate_values", "id": "d", "columns": ["y"]}]))
    c = parse_spec(
        _minimal(
            annotate=[{"kind": "duplicate_values", "id": "d", "columns": ["x"]}],
            present={"matches_only": True},
        )
    )
    assert a.annotate_fingerprint() != b.annotate_fingerprint()
    # present-stage changes don't invalidate annotation caches
    assert a.annotate_fingerprint() == c.annotate_fingerprint()


def test_validation_errors_shape():
    with pytest.raises(ValidationError) as exc:
        parse_spec({"name": ""})
    errs = validation_errors(exc.value)
    assert errs and set(errs[0]) == {"path", "code", "message"}
