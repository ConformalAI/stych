"""Real-MongoDB checks for the scratch-collection runner and pipeline guard.

Skipped unless a server is reachable:

    STYCH_TEST_MONGODB_URL=mongodb://localhost:27017 \
        uv run pytest tests/integration/test_mongo_scratch.py

A local ``mongod`` is enough — MongoDB Community installs natively, so unlike
the cloud warehouses this needs no account. Every database these tests create is
dropped in the fixture, including on failure.

These cover what ``mongomock`` structurally cannot: that a real server accepts
the stages and agrees on semantics. The stand-in is a partial implementation —
it lacks ``$type`` in expressions and disagrees with the documentation on
``$not`` — so a green unit run is necessary, not sufficient.
"""

from __future__ import annotations

import os
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")

from agents.mongo_agent import MongoScratchRunner, PipelineGuardError  # noqa: E402
from agents.mongo_agent.runner import KEEPALIVE  # noqa: E402


def _url() -> str:
    url = os.getenv("STYCH_TEST_MONGODB_URL", "")
    if not url:
        pytest.skip("Set STYCH_TEST_MONGODB_URL for MongoDB integration tests")
    return url


@pytest.fixture()
def mongo():
    """A throwaway source database plus a runner on its own scratch database."""
    url = _url()
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=4000)
    try:
        client.server_info()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB not reachable at {url}: {exc}")

    source_db = f"stych_src_{uuid.uuid4().hex[:6]}"
    scratch_db = f"stych_tmp_{uuid.uuid4().hex[:6]}"
    client[source_db].orders.insert_many(
        [
            {"_id": 1, "sku": "A", "qty": 2, "cust": {"city": "Leeds"}},
            {"_id": 2, "sku": "B", "qty": 0},
            {"_id": 3, "sku": "A", "qty": 5, "cust": {"city": "Hull"}},
        ]
    )
    runner = MongoScratchRunner(
        {"connection_url": url, "database": source_db, "scratch_database": scratch_db}
    )
    try:
        yield runner, client, source_db, scratch_db
    finally:
        client.drop_database(source_db)
        client.drop_database(scratch_db)


def test_working_copy_preserves_document_identity(mongo):
    """MongoDB needs no stamped row id: $out carries _id through, so a copy is
    aligned with its source by construction."""
    runner, client, source_db, scratch_db = mongo
    db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    assert db == scratch_db
    assert runner.get_row_count(db, name) == 3
    assert [d["_id"] for d in client[db][name].find().sort("_id")] == [1, 2, 3]


def test_branching_stays_aligned_with_its_parent(mongo):
    runner, client, source_db, _scratch = mongo
    db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    branch = f"{name}_b"
    runner.duplicate_temp_table(name, branch)
    original = [d["_id"] for d in client[db][name].find().sort("_id")]
    assert [d["_id"] for d in client[db][branch].find().sort("_id")] == original


def test_nested_documents_flatten_to_dotted_fields(mongo):
    runner, _client, source_db, _scratch = mongo
    db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    columns = {c["column_name"] for c in runner.get_table_columns(db, name)}
    assert "cust.city" in columns
    # The schema is inferred, never declared; callers must be able to see that.
    assert all(c["sampled"] for c in runner.get_table_columns(db, name))


@pytest.mark.parametrize(
    "pipeline_for",
    [
        lambda src, scratch: [{"$out": {"db": src, "coll": "orders"}}],
        lambda src, scratch: [{"$out": {"db": scratch, "coll": "orders"}}],
        lambda src, scratch: [{"$merge": {"into": {"db": src, "coll": "orders"}}}],
    ],
)
def test_writes_outside_the_scratch_namespace_are_refused(mongo, pipeline_for):
    runner, _client, source_db, scratch_db = mongo
    with pytest.raises(PipelineGuardError):
        runner.run_pipeline("orders", pipeline_for(source_db, scratch_db), database=source_db)


def test_server_side_javascript_is_refused(mongo):
    """$where runs JS inside the customer's database."""
    runner, _client, source_db, _scratch = mongo
    with pytest.raises(PipelineGuardError):
        runner.run_pipeline("orders", [{"$match": {"$where": "true"}}], database=source_db)


def test_the_source_collection_is_never_modified(mongo):
    runner, client, source_db, _scratch = mongo
    db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    client[db][name].update_many({}, {"$set": {"sku": "CHANGED"}})
    assert {d["sku"] for d in client[source_db].orders.find()} == {"A", "B"}


def test_native_cleaning_update_delete_and_deduplicate_stay_in_scratch(mongo):
    runner, client, source_db, _scratch = mongo
    db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    client[db][name].insert_one({"_id": 4, "sku": "A", "qty": 5})

    assert runner.update_many(name, {"qty": 0}, {"$set": {"qty": None}}) == 1
    assert runner.delete_many(name, {"qty": None}) == 1
    assert runner.deduplicate(name, ["sku", "qty"]) == 1

    assert runner.get_row_count(db, name) == 2
    assert client[source_db].orders.count_documents({}) == 3
    assert client[source_db].orders.count_documents({"qty": 0}) == 1


async def test_cleaning_tools_refresh_sample_and_route_named_copies(mongo):
    from datetime import datetime

    import pandas as pd

    from agents.sql_agent.agent import SQLAgent
    from agents.state import AgentState, DataContext
    from tools.cleaning_tools import CleaningReactToolRegistry

    runner, client, source_db, scratch_db = mongo
    state = AgentState(session_id=f"mongo-tools-{uuid.uuid4().hex}")
    sample = pd.DataFrame([{"_id": 1, "sku": "A", "qty": 2}])
    state.set_data_context(
        DataContext(
            file_id="mongo-live",
            filename=f"MongoDB: {source_db}.orders",
            raw_dataframe=sample.copy(deep=True),
            working_dataframe=sample.copy(deep=True),
            uploaded_at=datetime.now(),
            metadata={
                "source": "mongodb",
                "connection_url": _url(),
                "database": source_db,
                "collection": "orders",
                "scratch_database": scratch_db,
            },
        )
    )
    agent = SQLAgent(llm_router=None)
    tools = CleaningReactToolRegistry(
        sql_agent=agent,
        workspace_manager=None,
        workspace_runner=None,
        is_mongo_available=True,
    )

    updated = await tools.call(
        "mongo_update",
        {"filter": "{}", "update": '{"$rename": {"sku": "product"}}'},
        state,
    )
    listed = await tools.call("list_working_copies", {}, state)
    created = await tools.call(
        "create_working_copy", {"name": "alternate", "from": "working"}, state
    )

    assert "modified 3" in updated
    assert "product" in state.data_context.working_dataframe.columns
    assert "sku" not in state.data_context.working_dataframe.columns
    assert state.data_context.metadata["schema_sample_documents"] == 1_000
    assert "MongoDB cleaning tools are not available" not in listed
    assert "alternate" in created
    assert client[source_db].orders.count_documents({"sku": {"$exists": True}}) == 3
    assert client[source_db].orders.count_documents({"product": {"$exists": True}}) == 0


def test_listing_excludes_the_keepalive_marker(mongo):
    """The marker forces the scratch database to exist; it is not a working copy."""
    runner, _client, source_db, _scratch = mongo
    runner.create_temp_table_from_source("sess", source_db, "orders")
    assert KEEPALIVE not in runner.list_temp_tables_with_prefix("stych_")


def test_predicates_agree_with_the_pandas_engine_on_a_real_server(mongo):
    """The parity cases, run against a real server rather than the stand-in."""
    import pandas as pd

    from filters.predicate_mongo import compile_match
    from filters.predicate_pandas import compile_mask
    from filters.spec import ComparePred, MembershipPred, NotPred, NullPred

    runner, client, source_db, _scratch = mongo
    docs = [
        {"_id": 1, "n": 10},
        {"_id": 2, "n": 0},
        {"_id": 3, "n": None},
        {"_id": 4},
        {"_id": 5, "n": 7},
    ]
    client[source_db].nulls.insert_many([dict(d) for d in docs])
    frame = pd.DataFrame([{"_id": d["_id"], "n": d.get("n")} for d in docs])
    columns = {"_id", "n"}

    for pred in (
        ComparePred(op="gt", column="n", value=0),
        ComparePred(op="ne", column="n", value=10),
        NullPred(op="is_null", column="n"),
        MembershipPred(op="not_in", column="n", values=[0, 7]),
        NotPred(op="not", child=ComparePred(op="gt", column="n", value=0)),
    ):
        expr = compile_match(pred, columns)
        got = sorted(
            d["_id"] for d in client[source_db].nulls.aggregate([{"$match": {"$expr": expr}}])
        )
        mask = compile_mask(pred, lambda name: frame[name] if name in frame else None)
        assert got == sorted(frame.loc[mask, "_id"].tolist()), pred.op


# ---------------------------------------------------------------------------
# Filter page — the whole select/present path against a real server
# ---------------------------------------------------------------------------


def _filter_spec(**present):
    from filters.spec import ComparePred, FilterSpec, Presentation

    return FilterSpec(
        id="f1",
        name="big orders",
        source={"kind": "sql", "id": "s1"},
        select=ComparePred(op="gt", column="qty", value=1),
        present=Presentation(**present),
    )


def test_filter_page_matches_and_counts_over_the_whole_collection(mongo):
    from filters.mongo_service import build_mongo_filter_page

    runner, _client, source_db, _scratch_db = mongo
    page = build_mongo_filter_page(runner, _filter_spec(), database=source_db, collection="orders")

    assert page["kind"] == "filter"
    assert page["total_rows"] == 3
    assert page["summary"]["matched_rows"] == 2  # qty 2 and 5; qty 0 is not > 1
    assert page["summary"]["identity_resolved"] == "key:_id"
    # A nested field is flattened for display rather than shown as a document.
    assert "cust.city" in [c["name"] for c in page["columns"]]


def test_filter_page_matches_only_narrows_rows_and_count(mongo):
    from filters.mongo_service import build_mongo_filter_page

    runner, _client, source_db, _scratch_db = mongo
    page = build_mongo_filter_page(
        runner, _filter_spec(), database=source_db, collection="orders", matches_only=True
    )
    assert page["filtered_rows"] == 2
    assert len(page["rows"]) == 2


def test_filter_page_search_narrows_across_display_columns(mongo):
    from filters.mongo_service import build_mongo_filter_page

    runner, _client, source_db, _scratch_db = mongo
    page = build_mongo_filter_page(
        runner, _filter_spec(), database=source_db, collection="orders", search="Hull"
    )
    assert page["filtered_rows"] == 1
    assert len(page["rows"]) == 1


def test_filter_page_column_filter_is_a_literal_substring(mongo):
    """The grid's per-column box is a substring box, not a regex box — a regex
    metacharacter must match itself rather than being interpreted."""
    from filters.mongo_service import build_mongo_filter_page

    runner, _client, source_db, _scratch_db = mongo
    page = build_mongo_filter_page(
        runner, _filter_spec(), database=source_db, collection="orders", filters={"sku": "."}
    )
    assert page["filtered_rows"] == 0


def test_filter_page_highlights_fired_rules(mongo):
    from filters.mongo_service import build_mongo_filter_page
    from filters.spec import ComparePred, PresentRule

    runner, _client, source_db, _scratch_db = mongo
    spec = _filter_spec(
        rules=[
            PresentRule(
                id="hot",
                target="row",
                severity="danger",
                predicate=ComparePred(op="ge", column="qty", value=5),
            )
        ]
    )
    page = build_mongo_filter_page(runner, spec, database=source_db, collection="orders")
    assert page["summary"]["rule_counts"] == {"hot": 1}
    severities = [m.get("s") for m in page["row_meta"] if m]
    assert severities.count("danger") == 1


def test_filter_page_reads_a_scratch_working_copy(mongo):
    """The working copy is filtered through the same path as the source."""
    from filters.mongo_service import build_mongo_filter_page

    runner, _client, source_db, scratch_db = mongo
    _db, name = runner.create_temp_table_from_source("sess", source_db, "orders")
    page = build_mongo_filter_page(runner, _filter_spec(), database=scratch_db, collection=name)
    assert page["total_rows"] == 3
    assert page["summary"]["matched_rows"] == 2


# ---------------------------------------------------------------------------
# Export — a full scan through one cursor
# ---------------------------------------------------------------------------


def _adapter(url: str, database: str, collection: str, columns):
    from datasources.adapters.base import AdapterColumn
    from datasources.adapters.mongodb import MongoTableAdapter

    return MongoTableAdapter(
        url,
        database=database,
        collection=collection,
        columns=[AdapterColumn(name=n, db_type="object", kind="string") for n in columns],
    )


def test_export_streams_every_document(mongo):
    _runner, _client, source_db, _scratch_db = mongo
    adapter = _adapter(_url(), source_db, "orders", ["_id", "sku", "qty"])
    frames = list(adapter.stream_batches(batch_rows=2))
    assert sum(len(f) for f in frames) == 3
    assert len(frames) == 2  # 2 + 1, so batching really happened


def test_export_on_a_standalone_falls_back_without_failing(mongo):
    """A standalone mongod refuses a snapshot session. The export must still
    complete through the plain cursor rather than surfacing that as an error."""
    _runner, _client, source_db, _scratch_db = mongo
    adapter = _adapter(_url(), source_db, "orders", ["_id", "sku"])
    frames = list(adapter.stream_batches())
    assert sum(len(f) for f in frames) == 3


def test_export_honours_a_row_cap(mongo):
    _runner, _client, source_db, _scratch_db = mongo
    adapter = _adapter(_url(), source_db, "orders", ["_id", "sku"])
    assert sum(len(f) for f in adapter.stream_batches(max_rows=2)) == 2


def test_export_projects_only_the_requested_columns(mongo):
    _runner, _client, source_db, _scratch_db = mongo
    adapter = _adapter(_url(), source_db, "orders", ["_id", "sku", "qty"])
    frame = next(iter(adapter.stream_batches(columns=["sku"])))
    assert list(frame.columns) == ["sku"]


def test_export_orders_by_the_immutable_identity(mongo):
    """_id ordering is what removes the moving-document hazard from a scan that
    is not snapshot-isolated, so it is a property worth pinning."""
    _runner, _client, source_db, _scratch_db = mongo
    adapter = _adapter(_url(), source_db, "orders", ["_id", "sku"])
    ids = [v for frame in adapter.stream_batches(batch_rows=2) for v in frame["_id"]]
    assert ids == sorted(ids)
