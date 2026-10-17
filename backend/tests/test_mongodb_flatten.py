"""Mongo document flattening for display and comparison."""

from __future__ import annotations

import pandas as pd

from datasources.mongodb import coerce_bson, flatten_documents


class _Oid:
    def __init__(self, value: str):
        self.value = value

    def __str__(self):
        return self.value


def test_flatten_documents_dotted_columns_and_id_as_text():
    docs = [
        {"_id": _Oid("abc"), "user": {"name": "Ada", "city": "London"}, "n": 1},
        {"_id": _Oid("def"), "user": {"name": "Bob", "city": "Paris"}, "n": 2},
    ]
    frame = flatten_documents(docs)
    assert set(frame.columns) == {"_id", "n", "user.name", "user.city"}
    assert frame["_id"].tolist() == ["abc", "def"]
    assert frame["user.city"].tolist() == ["London", "Paris"]


def test_flatten_documents_json_encodes_leftover_arrays():
    docs = [
        {"_id": "1", "name": "a", "tags": ["red", "blue"], "extra": {"ok": True}},
        {"_id": "2", "name": "b", "tags": ["green"], "extra": {"ok": False}},
    ]
    frame = flatten_documents(docs)
    assert "extra.ok" in frame.columns
    assert frame["tags"].tolist() == ['["red", "blue"]', '["green"]']
    assert frame["tags"].map(lambda value: isinstance(value, str)).all()


def test_coerce_bson_stringifies_unserialisable_values():
    assert coerce_bson(_Oid("xyz")) == "xyz"
    assert coerce_bson({"nested": _Oid("1")}) == {"nested": "1"}


def test_empty_docs_yield_empty_frame():
    frame = flatten_documents([])
    assert isinstance(frame, pd.DataFrame)
    assert frame.empty
