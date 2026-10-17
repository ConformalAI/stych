"""Pandas filter engine: predicate masks, builtin analyzers, page assembly."""

from __future__ import annotations

import pandas as pd
import pytest

from filters import builtins as fb
from filters.annotate_pandas import run_annotate_steps
from filters.predicate_pandas import UnknownColumn, compile_mask
from filters.service import build_filter_page, resolve_row_identity
from filters.spec import parse_spec


def _pred(payload):
    return parse_spec({"name": "t", "select": payload}).select


def _resolver(df):
    cols = {str(c): df[c] for c in df.columns}

    def resolve(name):
        if name not in cols:
            raise UnknownColumn(name)
        return cols[name]

    return resolve


# ------------------------------------------------------------------ predicates


def test_eq_excludes_nulls_ne_matches_them():
    df = pd.DataFrame({"x": [1.0, 2.0, None]})
    resolve = _resolver(df)
    assert compile_mask(_pred({"op": "eq", "column": "x", "value": 2}), resolve).tolist() == [
        False,
        True,
        False,
    ]
    # IS DISTINCT FROM: the null row IS distinct from 2.
    assert compile_mask(_pred({"op": "ne", "column": "x", "value": 2}), resolve).tolist() == [
        True,
        False,
        True,
    ]


def test_ordering_with_numeric_coercion_on_object_column():
    df = pd.DataFrame({"x": ["10", "3", "oops", None]})
    mask = compile_mask(_pred({"op": "gt", "column": "x", "value": 5}), _resolver(df))
    assert mask.tolist() == [True, False, False, False]


def test_between_and_membership():
    df = pd.DataFrame({"x": [1, 5, 9, None]})
    resolve = _resolver(df)
    between = _pred({"op": "between", "column": "x", "low": 2, "high": 9})
    assert compile_mask(between, resolve).tolist() == [False, True, True, False]
    not_in = _pred({"op": "not_in", "column": "x", "values": [5]})
    assert compile_mask(not_in, resolve).tolist() == [True, False, True, False]


def test_string_ops_case_and_regex():
    df = pd.DataFrame({"s": ["Alpha", "beta", None, 12]})
    resolve = _resolver(df)
    contains = _pred({"op": "contains", "column": "s", "pattern": "ALPHA"})
    assert compile_mask(contains, resolve).tolist() == [True, False, False, False]
    starts = _pred({"op": "startswith", "column": "s", "pattern": "Be", "case_sensitive": True})
    assert compile_mask(starts, resolve).tolist() == [False, False, False, False]
    regex = _pred({"op": "regex", "column": "s", "pattern": r"^\d+$"})
    assert compile_mask(regex, resolve).tolist() == [False, False, False, True]


def test_column_vs_column_and_boolean_nesting():
    df = pd.DataFrame({"a": [1, 5, None], "b": [2, 4, 7]})
    resolve = _resolver(df)
    lt = _pred({"op": "lt", "column": "a", "value": {"column": "b"}})
    assert compile_mask(lt, resolve).tolist() == [True, False, False]
    combo = _pred(
        {
            "op": "or",
            "children": [
                {"op": "lt", "column": "a", "value": {"column": "b"}},
                {"op": "not", "child": {"op": "not_null", "column": "a"}},
            ],
        }
    )
    assert compile_mask(combo, resolve).tolist() == [True, False, True]


def test_unknown_column_raises():
    df = pd.DataFrame({"x": [1]})
    with pytest.raises(UnknownColumn):
        compile_mask(_pred({"op": "eq", "column": "nope", "value": 1}), _resolver(df))


# ------------------------------------------------------------------ builtins


def test_fuzzy_groups_transitively_and_scores_exact_dupes():
    # At 0.95: A~B (0.968) and B~C (0.966) pass, A~C (0.933) does not —
    # only transitive closure can put all three in one group.
    values = pd.Series(
        [
            "Acme Corporation",
            "Acme Corporatio",
            "Acme Corporati",
            "Zed Ltd",
            "Zed Ltd",
            "Solo Inc",
        ],
        index=pd.Index(range(6)),
    )
    frame, degraded = fb.fuzzy_duplicate_groups(
        values, strategy="token_sort", threshold=0.95, max_rows=1000
    )
    assert not degraded
    groups = frame[fb.GROUP_COL]
    assert groups.iloc[0] == groups.iloc[1] == groups.iloc[2]
    # The exact-duplicate Zed rows form another group with score 1.0…
    assert groups.iloc[3] == groups.iloc[4] != groups.iloc[0]
    assert frame[fb.SCORE_COL].iloc[3] == 1.0
    assert frame[fb.MATCH_KEY_COL].iloc[3] == "4"
    # …and the singleton stays unannotated.
    assert pd.isna(groups.iloc[5])


def test_fuzzy_row_cap_degrades():
    values = pd.Series([f"name {i}" for i in range(200)])
    _, degraded = fb.fuzzy_duplicate_groups(
        values, strategy="levenshtein", threshold=0.99, max_rows=100
    )
    assert degraded


def test_duplicate_values_ignores_null_keys():
    df = pd.DataFrame({"email": ["a@x.com", "a@x.com", None, None, "b@x.com"]})
    frame = fb.duplicate_values_pandas(df, pd.Series(range(5)), ["email"])
    assert frame[fb.COUNT_COL].iloc[0] == 2
    assert frame[fb.GROUP_COL].iloc[0] == frame[fb.GROUP_COL].iloc[1] == 0
    # shared missing values never count as duplicates
    assert frame[fb.COUNT_COL].isna().iloc[2] and frame[fb.COUNT_COL].isna().iloc[3]
    assert frame[fb.COUNT_COL].isna().iloc[4]


def test_pandas_step_runs_in_sandbox(tmp_path):
    """Agent-written code: input contract in, declared outputs out."""
    from workspace.runner import WorkspaceSandboxRunner

    df = pd.DataFrame({"id": range(6), "price": [1.0, 2.0, 3.0, 100.0, 4.0, 5.0]})
    code = (
        "import pandas as pd\n"
        "df = pd.read_parquet('filter_input.parquet')\n"
        "mean, std = df['price'].mean(), df['price'].std()\n"
        "out = pd.DataFrame({'__row_key__': df['__row_key__'],"
        " 'z_score': (df['price'] - mean) / std})\n"
        "out.to_parquet('filter_annotations/z.parquet', index=False)\n"
    )
    spec = parse_spec(
        {
            "name": "outliers",
            "annotate": [
                {
                    "kind": "pandas",
                    "id": "z",
                    "code": code,
                    "outputs": [{"name": "z_score", "dtype": "number", "role": "score"}],
                }
            ],
        }
    )
    ann = run_annotate_steps(
        df,
        spec,
        row_keys=pd.Series(range(6)),
        workspace=tmp_path,
        runner=WorkspaceSandboxRunner(timeout_seconds=60),
    )
    assert ann.statuses == [
        {"step_id": "z", "status": "ok", "rows": 6, "coverage": 1.0, "error": None, "cached": False}
    ]
    assert ann.frame["z_score"].idxmax() == 3


def test_pandas_step_missing_output_reports_error(tmp_path):
    from workspace.runner import WorkspaceSandboxRunner

    df = pd.DataFrame({"id": [1]})
    spec = parse_spec(
        {
            "name": "t",
            "annotate": [
                {
                    "kind": "pandas",
                    "id": "bad",
                    "code": "print('no output written')",
                    "outputs": [{"name": "flag"}],
                }
            ],
        }
    )
    ann = run_annotate_steps(
        df,
        spec,
        row_keys=pd.Series([0]),
        workspace=tmp_path,
        runner=WorkspaceSandboxRunner(timeout_seconds=60),
    )
    assert ann.statuses[0]["status"] == "error"
    assert "did not write" in ann.statuses[0]["error"]
    # failed step's declared outputs still exist as all-null columns
    assert ann.frame["flag"].isna().all()


# ------------------------------------------------------------------ identity


def test_identity_auto_prefers_id_like_unique_column():
    df = pd.DataFrame({"val": [1, 1, 2], "user_id": ["a", "b", "c"]})
    keys, label, warning = resolve_row_identity(df, parse_spec({"name": "t"}).identity)
    assert label == "key:user_id" and warning is None
    assert keys.tolist() == ["a", "b", "c"]


def test_identity_explicit_key_falls_back_with_warning():
    df = pd.DataFrame({"x": [1, 1]})
    identity = parse_spec({"name": "t", "identity": {"mode": "key", "key": "x"}}).identity
    keys, label, warning = resolve_row_identity(df, identity)
    assert label == "positional" and "not unique" in warning


# ------------------------------------------------------------------ page


def _page(df, spec_payload, **kwargs):
    spec = parse_spec(spec_payload)
    keys, label, warning = resolve_row_identity(df, spec.identity)
    ann = run_annotate_steps(df, spec, row_keys=keys)
    return build_filter_page(
        df, spec, ann, identity_label=label, identity_warning=warning, **kwargs
    )


def test_page_select_rules_and_meta():
    df = pd.DataFrame(
        {"id": range(4), "balance": [-5.0, 10.0, -1.0, None], "email": ["a", None, "c", "d"]}
    )
    page = _page(
        df,
        {
            "name": "flags",
            "select": {"op": "lt", "column": "balance", "value": 0},
            "present": {
                "rules": [
                    {
                        "id": "neg",
                        "target": "cells",
                        "columns": ["balance"],
                        "severity": "danger",
                        "label": "negative",
                        "tooltip": "balance is {balance}",
                    },
                    {
                        "id": "noemail",
                        "target": "row",
                        "predicate": {"op": "is_null", "column": "email"},
                        "severity": "warning",
                    },
                ]
            },
        },
    )
    assert page["summary"]["matched_rows"] == 2
    assert page["summary"]["rule_counts"] == {"neg": 2, "noemail": 1}
    balance_col = [c["name"] for c in page["columns"]].index("balance")
    cell = page["cell_meta"][0][balance_col]
    assert cell["k"] == "danger" and cell["l"] == "negative"
    assert cell["t"] == "balance is -5.0"
    assert page["row_meta"][1]["s"] == "warning" and page["row_meta"][1]["m"] is False
    assert page["row_meta"][0]["m"] is True


def test_page_matches_only_and_enabled_rules():
    df = pd.DataFrame({"id": range(4), "x": [1, -1, 2, -2]})
    payload = {
        "name": "neg",
        "select": {"op": "lt", "column": "x", "value": 0},
        "present": {"rules": [{"id": "r", "severity": "danger"}]},
    }
    page = _page(df, payload, matches_only=True)
    assert page["filtered_rows"] == 2 and page["total_rows"] == 4
    page = _page(df, payload, enabled_rules=[])
    assert page["filtered_rows"] == 0


def test_page_groups_are_adjacent_and_banded():
    df = pd.DataFrame(
        {
            "id": range(5),
            "name": ["Acme Corp", "Solo Inc", "Acme Corp.", "Beta LLC", "Beta LLC"],
        }
    )
    page = _page(
        df,
        {
            "name": "dups",
            "annotate": [
                {
                    "kind": "fuzzy_duplicates",
                    "id": "dup",
                    "on": "name",
                    "strategy": "token_sort",
                    "threshold": 0.8,
                }
            ],
            "select": {"op": "not_null", "column": "dup_group"},
            "present": {"group_by": "dup_group", "matches_only": True},
        },
    )
    # Grouped rows come back adjacent: the two Acmes, then the two Betas.
    ids = [row[0] for row in page["rows"]]
    assert ids == [0, 2, 3, 4]
    bands = [m["g"] for m in page["row_meta"]]
    assert bands == [0, 0, 1, 1]
    assert page["summary"]["group_count"] == 2
    ann_cols = [c["name"] for c in page["columns"] if c.get("origin") == "annotation"]
    assert ann_cols == ["dup_group", "dup_score", "dup_match_key"]


def test_page_unknown_rule_column_skips_rule_with_warning():
    df = pd.DataFrame({"id": [1], "x": [1]})
    page = _page(
        df,
        {
            "name": "t",
            "present": {
                "rules": [{"id": "bad", "predicate": {"op": "eq", "column": "nope", "value": 1}}]
            },
        },
    )
    assert "bad" not in page["summary"]["rule_counts"]
    assert "unknown column" in page["summary"]["warning"]


def test_page_sort_disabled_while_grouping():
    df = pd.DataFrame({"id": [0, 1, 2, 3], "v": ["a", "a", "b", "c"]})
    page = _page(
        df,
        {
            "name": "t",
            "annotate": [{"kind": "duplicate_values", "id": "d", "columns": ["v"]}],
            "present": {"group_by": "d_group"},
        },
        sort_by="id",
        sort_dir="desc",
    )
    # group order wins over the requested sort
    assert [row[0] for row in page["rows"]] == [0, 1, 2, 3]
    assert page["summary"]["group_by"] == "d_group"
