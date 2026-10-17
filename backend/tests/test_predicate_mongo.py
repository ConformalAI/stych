"""MongoDB predicate compilation, checked for parity with the pandas engine.

The FilterSpec predicate tree now has three compilers. What matters is not that
each works alone but that they *agree* — above all on nulls, where SQL's natural
three-valued logic and Mongo's BSON type ordering both differ from the total
booleans the pandas engine produces.

Pipelines run against mongomock, an in-process MongoDB stand-in. It is a partial
implementation, so this proves agreement on semantics, not that a real server
accepts every stage.
"""

from __future__ import annotations

import pandas as pd
import pytest

mongomock = pytest.importorskip("mongomock")

from filters.predicate_mongo import compile_match  # noqa: E402
from filters.predicate_pandas import compile_mask  # noqa: E402
from filters.spec import (  # noqa: E402
    BetweenPred,
    BoolPred,
    ColumnOperand,
    ComparePred,
    MembershipPred,
    NotPred,
    NullPred,
    StringPred,
)

# Deliberately mixes present values, explicit nulls, and absent fields.
ROWS = [
    {"_id": 1, "n": 10, "s": "alpha", "m": 5},
    {"_id": 2, "n": 0, "s": "Beta", "m": 0},
    {"_id": 3, "n": None, "s": "gamma", "m": 1},
    {"_id": 4, "s": "delta", "m": 2},
    {"_id": 5, "n": 7, "s": None, "m": 7},
    {"_id": 6, "n": -3, "s": "alphabet", "m": 1},
]
COLUMNS = {"n", "s", "m", "_id"}

FRAME = pd.DataFrame(
    [{"_id": r["_id"], "n": r.get("n"), "s": r.get("s"), "m": r.get("m")} for r in ROWS]
)


@pytest.fixture()
def collection():
    client = mongomock.MongoClient()
    col = client.db.docs
    col.insert_many([dict(r) for r in ROWS])
    return col


def _mongo_ids(collection, pred) -> list:
    expr = compile_match(pred, COLUMNS)
    return sorted(d["_id"] for d in collection.aggregate([{"$match": {"$expr": expr}}]))


def _pandas_ids(pred) -> list:
    mask = compile_mask(pred, lambda name: FRAME[name] if name in FRAME else None)
    return sorted(FRAME.loc[mask, "_id"].tolist())


CASES = [
    ("gt", ComparePred(op="gt", column="n", value=0)),
    ("ge", ComparePred(op="ge", column="n", value=0)),
    ("lt", ComparePred(op="lt", column="n", value=7)),
    ("eq", ComparePred(op="eq", column="n", value=10)),
    ("ne", ComparePred(op="ne", column="n", value=10)),
    ("between", BetweenPred(op="between", column="n", low=0, high=10)),
    ("in", MembershipPred(op="in", column="n", values=[0, 7])),
    ("not_in", MembershipPred(op="not_in", column="n", values=[0, 7])),
    ("is_null", NullPred(op="is_null", column="n")),
    ("not_null", NullPred(op="not_null", column="n")),
    ("contains", StringPred(op="contains", column="s", pattern="alpha")),
    ("startswith", StringPred(op="startswith", column="s", pattern="alpha")),
    ("endswith", StringPred(op="endswith", column="s", pattern="a")),
    ("case_sensitive", StringPred(op="contains", column="s", pattern="B", case_sensitive=True)),
    ("regex", StringPred(op="regex", column="s", pattern="^alpha")),
    ("not", NotPred(op="not", child=ComparePred(op="gt", column="n", value=0))),
    (
        "and",
        BoolPred(
            op="and",
            children=[
                ComparePred(op="gt", column="n", value=0),
                StringPred(op="contains", column="s", pattern="a"),
            ],
        ),
    ),
    (
        "or",
        BoolPred(
            op="or",
            children=[
                ComparePred(op="lt", column="n", value=0),
                NullPred(op="is_null", column="s"),
            ],
        ),
    ),
    (
        "nested not/or",
        NotPred(
            op="not",
            child=BoolPred(
                op="or",
                children=[
                    ComparePred(op="eq", column="n", value=0),
                    NullPred(op="is_null", column="n"),
                ],
            ),
        ),
    ),
]


@pytest.mark.parametrize(("label", "pred"), CASES, ids=[c[0] for c in CASES])
def test_mongo_agrees_with_pandas(collection, label, pred):
    assert _mongo_ids(collection, pred) == _pandas_ids(pred), label


def test_column_to_column_null_pairs_match_pandas(collection):
    pred = ComparePred(op="eq", column="n", value=ColumnOperand(column="m"))
    assert _mongo_ids(collection, pred) == _pandas_ids(pred)
    ne = ComparePred(op="ne", column="n", value=ColumnOperand(column="m"))
    assert _mongo_ids(collection, ne) == _pandas_ids(ne)


def test_a_missing_field_never_matches_a_comparison(collection):
    """Mongo orders BSON types rather than refusing to compare them, so an
    unguarded comparison against a missing field can return true."""
    ids = _mongo_ids(collection, ComparePred(op="gt", column="n", value=-100))
    assert 4 not in ids  # _id 4 has no "n" at all
    assert 3 not in ids  # _id 3 has n = null


def test_unknown_columns_are_rejected(collection):
    from filters.predicate_pandas import UnknownColumn

    with pytest.raises(UnknownColumn):
        compile_match(ComparePred(op="eq", column="nope", value=1), COLUMNS)


def test_negation_avoids_the_not_operator():
    """$not's argument arity is ambiguous across implementations, and guessing
    wrong silently inverts a filter instead of failing."""
    expr = compile_match(NotPred(op="not", child=NullPred(op="is_null", column="n")), COLUMNS)
    assert "$not" not in str(expr)
    assert "$eq" in str(expr)
