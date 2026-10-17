from types import SimpleNamespace

import pandas as pd
import pytest

from agents.linking.execution import (
    execute_native_join,
    native_join_eligible,
    validate_confirmed_physical_plan,
)
from datasources.join_planner import plan_join
from execution.join_inputs import JoinInputError, join_context_fingerprint


def _mongo(name: str):
    frame = pd.DataFrame({"id": [1]})
    return SimpleNamespace(
        filename=name,
        file_id=f"mongo-{name}",
        dataframe=frame,
        working_dataframe=frame,
        metadata={
            "source": "mongodb",
            "connection_url": "mongodb://localhost:27017/analytics",
            "database": "analytics",
            "collection": name,
        },
    )


def _step(strategy="exact"):
    return {
        "left": "orders",
        "right": "customers",
        "left_on": "id",
        "right_on": "id",
        "strategy": strategy,
        "join_type": "FULL OUTER",
    }


def test_native_eligibility_keeps_agent_strategy_but_routes_only_exact():
    plan = plan_join([_mongo("orders"), _mongo("customers")])
    assert native_join_eligible(plan, [_step("exact")])
    assert not native_join_eligible(plan, [_step("levenshtein")])
    assert not native_join_eligible(
        plan_join([_mongo("orders"), _mongo("customers"), _mongo("accounts")]),
        [_step("exact")],
    )


def test_mongodb_native_result_becomes_anchor_working_copy(monkeypatch):
    contexts = [_mongo("orders"), _mongo("customers")]
    plan = plan_join(contexts)
    monkeypatch.setattr(
        "agents.linking.mongo_join.execute_mongo_join",
        lambda *_args, **_kwargs: SimpleNamespace(
            database="stych_temp",
            collection="stych_session_join",
            rows=3,
            columns=["left.id", "right.id"],
            join_type="FULL OUTER",
            preview=pd.DataFrame({"left.id": [1], "right.id": [1]}),
        ),
    )

    result = execute_native_join(
        plan,
        [_step()],
        session_id="session",
        state=SimpleNamespace(),
    )

    assert result.connector == "mongodb"
    assert result.table == "stych_temp.stych_session_join"
    assert result.rows == 3
    assert contexts[0].metadata["temp_table"] == "stych_session_join"
    assert contexts[0].metadata["temp_schema"] == "stych_temp"
    assert list(contexts[0].working_dataframe.columns) == ["left.id", "right.id"]
    assert contexts[0].metadata["snapshot_total_rows"] == 3


def test_native_execution_rejects_relation_changed_after_confirmation():
    contexts = [_mongo("orders"), _mongo("customers")]
    physical = {
        "native_primary": True,
        "connector": "mongodb",
        "context_fingerprints": [join_context_fingerprint(ctx) for ctx in contexts],
    }
    contexts[1].metadata["collection"] = "other_customers"

    with pytest.raises(JoinInputError, match="changed after confirmation"):
        validate_confirmed_physical_plan(contexts, [_step()], physical)
