"""Complete join inputs never reuse a connected source's planning sample."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from datasources.adapters.mongodb import _batched
from execution.join_inputs import (
    JoinInputError,
    join_input_plan_from_payload,
    join_input_plan_payload,
    materialize_full_join_datasets,
    plan_full_join_inputs,
)


def _ctx(name: str, source: str, rows: int = 2, *, file_id: str = "file-1"):
    frame = pd.DataFrame({"id": range(rows)})
    return SimpleNamespace(
        filename=name,
        file_id=file_id,
        dataframe=frame,
        working_dataframe=frame,
        metadata={
            "source": source,
            "connection_url": "mongodb://example/test",
            "database": "test",
            "collection": name,
        },
    )


def test_upload_plan_is_full_without_transfer():
    plan = plan_full_join_inputs([_ctx("a.csv", "upload")])

    assert plan.inputs[0].scope.value == "full"
    assert not plan.inputs[0].transfer_required
    assert plan.estimated_transfer_bytes == 0


def test_connected_plan_counts_the_live_adapter_not_the_sample(monkeypatch):
    ctx = _ctx("orders", "mongodb", rows=2)
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=50))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)

    plan = plan_full_join_inputs([ctx])

    assert plan.inputs[0].rows == 50
    assert plan.inputs[0].transfer_required
    assert plan.estimated_transfer_bytes > 0
    assert join_input_plan_from_payload(join_input_plan_payload(plan)) == plan


def test_missing_connected_credentials_fail_instead_of_using_the_sample(monkeypatch):
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: False)

    with pytest.raises(JoinInputError, match="Reconnect"):
        plan_full_join_inputs([_ctx("orders", "mongodb")])


def test_materialization_streams_every_connected_row(monkeypatch):
    ctx = _ctx("orders", "mongodb", rows=2)
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=3))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)
    monkeypatch.setattr(
        "execution.join_inputs._iter_source_batches",
        lambda *_args, **_kwargs: iter([pd.DataFrame({"id": [1, 2]}), pd.DataFrame({"id": [3]})]),
    )
    plan = plan_full_join_inputs([ctx])

    datasets = materialize_full_join_datasets([ctx], plan)

    assert datasets[0]["data"]["id"].tolist() == [1, 2, 3]
    assert datasets[0]["metadata"]["join_input_scope"] == "full"


def test_over_budget_requires_the_confirmation_flag(monkeypatch):
    ctx = _ctx("orders", "mongodb", rows=2)
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=3))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)
    plan = plan_full_join_inputs([ctx], max_input_bytes=1)

    with pytest.raises(JoinInputError, match="explicit approval"):
        materialize_full_join_datasets([ctx], plan)


def test_changed_asset_is_rejected_before_reading(monkeypatch):
    original = _ctx("orders", "mongodb")
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=2))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)
    plan = plan_full_join_inputs([original])

    with pytest.raises(JoinInputError, match="changed"):
        materialize_full_join_datasets([_ctx("customers", "mongodb")], plan)


def test_upload_replacement_with_same_name_is_rejected():
    original = _ctx("orders.csv", "upload", file_id="upload-1")
    plan = plan_full_join_inputs([original])
    replacement = _ctx("orders.csv", "upload", file_id="upload-1")
    replacement.working_dataframe.loc[0, "id"] = 999

    with pytest.raises(JoinInputError, match="changed"):
        materialize_full_join_datasets([replacement], plan)


def test_connected_source_identity_change_is_rejected_before_streaming(monkeypatch):
    original = _ctx("orders", "mongodb")
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=2))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)
    plan = plan_full_join_inputs([original])
    replacement = _ctx("orders", "mongodb")
    replacement.metadata["database"] = "other_database"

    with pytest.raises(JoinInputError, match="changed"):
        materialize_full_join_datasets([replacement], plan)


def test_source_growth_is_detected_instead_of_truncated(monkeypatch):
    ctx = _ctx("orders", "mongodb", rows=2)
    adapter = SimpleNamespace(count=lambda: SimpleNamespace(value=3))
    monkeypatch.setattr("execution.join_inputs.table_adapters.supports_pushdown", lambda _: True)
    monkeypatch.setattr("execution.join_inputs.table_adapters.resolve_adapter", lambda *_: adapter)
    monkeypatch.setattr(
        "execution.join_inputs._iter_source_batches",
        lambda *_args, **_kwargs: iter([pd.DataFrame({"id": [1, 2, 3, 4]})]),
    )
    plan = plan_full_join_inputs([ctx])

    with pytest.raises(JoinInputError, match="changed while it was being read"):
        materialize_full_join_datasets([ctx], plan)


def test_mongo_full_export_unions_fields_that_appear_after_the_sample():
    frames = list(
        _batched(
            iter([{"_id": 1, "name": "first"}, {"_id": 2, "late_field": "kept"}]),
            None,
            batch_rows=1,
            max_rows=None,
        )
    )

    combined = pd.concat(frames, ignore_index=True)
    assert combined["late_field"].iloc[1] == "kept"
