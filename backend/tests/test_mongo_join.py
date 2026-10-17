"""Unit coverage for the isolated MongoDB exact-join compiler."""

from __future__ import annotations

import pytest

from agents.linking.mongo_join import (
    MONGO_FULL,
    MONGO_LEFT,
    MongoJoinError,
    MongoRelation,
    build_mongo_join_pipeline,
    execute_mongo_join,
    validate_mongo_join,
)

URL = "mongodb://localhost:27017/analytics"


def _relation(collection: str, *, database: str = "analytics", **kwargs) -> MongoRelation:
    return MongoRelation(
        database=database,
        collection=collection,
        connection_url=kwargs.pop("connection_url", URL),
        **kwargs,
    )


def test_native_lookup_requires_same_connection_and_database():
    left, right = _relation("orders"), _relation("customers")
    assert validate_mongo_join(left, right, [("customer_id", "id")]).eligible
    assert not validate_mongo_join(
        left, _relation("customers", database="other"), [("customer_id", "id")]
    ).eligible
    assert not validate_mongo_join(
        left,
        _relation("customers", connection_url="mongodb://other/analytics"),
        [("customer_id", "id")],
    ).eligible
    assert not validate_mongo_join(
        _relation("orders", connection_url="[REDACTED]"), right, [("customer_id", "id")]
    ).eligible


def test_pipeline_is_full_data_namespaced_and_scratch_only():
    pipeline = build_mongo_join_pipeline(
        _relation("orders"),
        _relation("customers"),
        [("customer_id", "id"), ("tenant", "tenant")],
        output_database="stych_temp",
        output_collection="stych_session_join",
    )

    lookup = next(stage["$lookup"] for stage in pipeline if "$lookup" in stage)
    assert lookup["from"] == "customers"
    assert lookup["let"] == {"stych_key_0": "$customer_id", "stych_key_1": "$tenant"}
    predicates = lookup["pipeline"][-1]["$match"]["$expr"]["$and"]
    assert predicates
    assert predicates[0]["$and"][0] == {"$ne": ["$$stych_key_0", None]}
    assert predicates[0]["$and"][1] == {"$ne": ["$id", None]}
    assert pipeline[-1] == {"$out": {"db": "stych_temp", "coll": "stych_session_join"}}
    assert all("$out" not in stage or stage["$out"]["db"] == "stych_temp" for stage in pipeline)
    assert any(stage.get("$project") == {"_id": 0, "left": 1, "right": 1} for stage in pipeline)


def test_left_and_full_outer_pipelines_preserve_unmatched_sides():
    left_pipeline = build_mongo_join_pipeline(
        _relation("orders"),
        _relation("customers"),
        [("customer_id", "id")],
        join_type=MONGO_LEFT,
        output_database="stych_temp",
        output_collection="stych_left",
    )
    assert any(
        stage.get("$unwind", {}).get("preserveNullAndEmptyArrays") for stage in left_pipeline
    )

    full_pipeline = build_mongo_join_pipeline(
        _relation("orders"),
        _relation("customers"),
        [("customer_id", "id")],
        join_type=MONGO_FULL,
        output_database="stych_temp",
        output_collection="stych_full",
    )
    union = next(stage["$unionWith"] for stage in full_pipeline if "$unionWith" in stage)
    assert union["coll"] == "customers"
    assert any(stage.get("$set", {}).get("left") is None for stage in union["pipeline"])


def test_invalid_fields_and_non_scratch_output_fail_closed():
    with pytest.raises(MongoJoinError):
        build_mongo_join_pipeline(
            _relation("orders"),
            _relation("customers"),
            [("$where", "id")],
            output_database="stych_temp",
            output_collection="stych_join",
        )
    with pytest.raises(MongoJoinError, match="scratch"):
        build_mongo_join_pipeline(
            _relation("orders"),
            _relation("customers"),
            [("customer_id", "id")],
            output_database="analytics",
            output_collection="orders_joined",
        )


class _FakeRunner:
    database = "analytics"
    connection_url = URL
    temp_schema = "stych_temp"

    def __init__(self):
        self.calls = []

    def ensure_temp_schema(self):
        self.calls.append(("ensure",))

    def run_pipeline(self, collection, pipeline, **kwargs):
        self.calls.append((collection, pipeline, kwargs))
        # The second call is the bounded read of the scratch result.
        if collection.startswith("stych_"):
            return [{"left": {"customer_id": 1}, "right": {"id": 1}}]
        return []

    def get_row_count(self, database, collection):
        assert database == "stych_temp"
        assert collection.startswith("stych_")
        return 1


def test_execute_returns_bounded_flattened_preview_and_scratch_result():
    runner = _FakeRunner()
    result = execute_mongo_join(
        runner,
        _relation("orders"),
        _relation("customers"),
        [("customer_id", "id")],
        session_id="session-1",
        preview_rows=25,
    )
    assert result.rows == 1
    assert result.collection.startswith("stych_")
    assert list(result.preview.columns) == ["left.customer_id", "right.id"]
    assert result.preview.iloc[0].to_dict() == {"left.customer_id": 1, "right.id": 1}
    assert runner.calls[1][2]["allow_writes"] is True
    assert runner.calls[2][2]["allow_writes"] is False
    assert runner.calls[2][2]["allow_other_collections"] is False


def test_execute_preserves_known_input_schema_when_preview_lacks_a_field():
    result = execute_mongo_join(
        _FakeRunner(),
        _relation("orders", columns=("customer_id", "late_order_field")),
        _relation("customers", columns=("id", "late_customer_field")),
        [("customer_id", "id")],
        session_id="session-schema",
    )

    assert list(result.preview.columns) == [
        "left.customer_id",
        "left.late_order_field",
        "right.id",
        "right.late_customer_field",
    ]


def test_preview_limit_is_bounded():
    with pytest.raises(MongoJoinError, match="preview_rows"):
        execute_mongo_join(
            _FakeRunner(),
            _relation("orders"),
            _relation("customers"),
            [("customer_id", "id")],
            session_id="s",
            preview_rows=501,
        )
