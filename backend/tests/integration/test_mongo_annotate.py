"""
Live-MongoDB checks for the filter annotate stage.

    STYCH_TEST_MONGODB_URL=mongodb://localhost:27017 \
        pytest tests/integration/test_mongo_annotate.py

``$setWindowFields`` semantics are the reason these run against a real server:
a stand-in that accepts the stage without implementing partition windows would
make a broken duplicate count look correct.
"""

from __future__ import annotations

import os
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")

from agents.mongo_agent import MongoScratchRunner  # noqa: E402
from filters.annotate_mongo import clear_registry  # noqa: E402
from filters.mongo_service import build_mongo_filter_page  # noqa: E402
from filters.spec import ComparePred, FilterSpec, Presentation  # noqa: E402


def _url() -> str:
    url = os.getenv("STYCH_TEST_MONGODB_URL", "")
    if not url:
        pytest.skip("Set STYCH_TEST_MONGODB_URL for MongoDB integration tests")
    return url


@pytest.fixture()
def customers():
    url = _url()
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=4000)
    try:
        client.server_info()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB not reachable at {url}: {exc}")

    database = f"stych_src_{uuid.uuid4().hex[:6]}"
    scratch = f"stych_tmp_{uuid.uuid4().hex[:6]}"
    client[database]["customers"].insert_many(
        [
            {"_id": 1, "email": "a@x.com", "name": "Acme Corp", "qty": 5},
            {"_id": 2, "email": "a@x.com", "name": "ACME Corporation", "qty": 6},
            {"_id": 3, "email": "b@x.com", "name": "Globex", "qty": 7},
            {"_id": 4, "email": None, "name": "Initech", "qty": 8},
            {"_id": 5, "email": None, "name": "Initech", "qty": 9},
        ]
    )
    runner = MongoScratchRunner(
        {"connection_url": url, "database": database, "scratch_database": scratch}
    )
    clear_registry()
    try:
        yield runner, database
    finally:
        clear_registry()
        client.drop_database(database)
        client.drop_database(scratch)
        client.close()


def _spec(annotate, **present) -> FilterSpec:
    return FilterSpec(
        id="f1",
        name="dupes",
        source={"kind": "sql", "id": "s1"},
        select=ComparePred(op="gt", column="qty", value=0),
        annotate=annotate,
        present=Presentation(**present),
    )


def _page(runner, database, spec, collection="customers", **kwargs):
    return build_mongo_filter_page(
        runner,
        spec,
        database=database,
        collection=collection,
        session_id="sess1",
        **kwargs,
    )


def _working_copy(runner, database):
    """A scratch copy of the source. Fuzzy grouping joins materialized groups
    on _id, and $lookup cannot cross databases, so both must be in scratch."""
    scratch, name = runner.create_temp_table_from_source("sess1", database, "customers")
    return scratch, name


def _column(page, name):
    index = [c["name"] for c in page["columns"]].index(name)
    return [row[index] for row in page["rows"]]


def _by_id(page, name):
    """Keyed by _id. The flattener stringifies _id for display, so the test
    reads it back the way the grid shows it."""
    return {int(k): v for k, v in zip(_column(page, "_id"), _column(page, name))}


def test_duplicate_values_counts_the_shared_group(customers):
    runner, database = customers
    spec = _spec([{"kind": "duplicate_values", "id": "dv", "columns": ["email"]}])
    page = _page(runner, database, spec)
    by_id = _by_id(page, "dv_count")
    assert by_id[1] == 2 and by_id[2] == 2  # a@x.com twice
    assert by_id[3] is None  # b@x.com is unique


def test_a_shared_missing_value_is_not_a_duplicate(customers):
    """Two rows with a null email share no value, so they are not a pair —
    matching the SQL engine's IS NOT NULL subquery."""
    runner, database = customers
    spec = _spec([{"kind": "duplicate_values", "id": "dv", "columns": ["email"]}])
    page = _page(runner, database, spec)
    by_id = _by_id(page, "dv_count")
    assert by_id[4] is None and by_id[5] is None


def test_duplicate_values_respects_min_count(customers):
    runner, database = customers
    spec = _spec([{"kind": "duplicate_values", "id": "dv", "columns": ["email"], "min_count": 3}])
    page = _page(runner, database, spec)
    assert all(value is None for value in _column(page, "dv_count"))


def test_duplicate_values_groups_on_several_columns(customers):
    runner, database = customers
    spec = _spec([{"kind": "duplicate_values", "id": "dv", "columns": ["name", "email"]}])
    page = _page(runner, database, spec)
    by_id = _by_id(page, "dv_count")
    # Same name but different emails, so no pair on the composite key.
    assert by_id[1] is None and by_id[2] is None


def test_annotation_outputs_become_filterable_columns(customers):
    """An annotation is a derived column, so a select predicate must be able to
    reference it — which means it has to exist before the predicate compiles."""
    runner, database = customers
    spec = FilterSpec(
        id="f2",
        name="only dupes",
        source={"kind": "sql", "id": "s1"},
        annotate=[{"kind": "duplicate_values", "id": "dv", "columns": ["email"]}],
        select=ComparePred(op="ge", column="dv_count", value=2),
        present=Presentation(),
    )
    page = _page(runner, database, spec)
    assert page["summary"]["matched_rows"] == 2


def test_fuzzy_duplicates_group_near_matches(customers):
    runner, database = customers
    scratch, name = _working_copy(runner, database)
    spec = _spec(
        [
            {
                "kind": "fuzzy_duplicates",
                "id": "fz",
                "on": "name",
                "strategy": "token_sort",
                "threshold": 0.4,
            }
        ]
    )
    page = _page(runner, scratch, spec, collection=name)
    groups = _by_id(page, "fz_group")
    # "Acme Corp" and "ACME Corporation" score 0.48 under token_sort.
    assert groups[1] is not None and groups[1] == groups[2]
    assert groups[3] is None


def test_fuzzy_duplicates_group_exact_repeats(customers):
    runner, database = customers
    scratch, name = _working_copy(runner, database)
    spec = _spec([{"kind": "fuzzy_duplicates", "id": "fz", "on": "name", "threshold": 0.95}])
    page = _page(runner, scratch, spec, collection=name)
    groups = _by_id(page, "fz_group")
    assert groups[4] is not None and groups[4] == groups[5]  # both "Initech"


def test_group_by_bands_duplicate_rows_adjacently(customers):
    """Grouping owns row order so a pair renders together."""
    runner, database = customers
    spec = _spec(
        [{"kind": "duplicate_values", "id": "dv", "columns": ["email"]}],
        group_by="dv_group",
    )
    page = _page(runner, database, spec)
    bands = [meta.get("g") if meta else None for meta in page["row_meta"]]
    assert any(b is not None for b in bands)


def test_a_step_over_an_unsampled_field_is_reported_not_silently_dropped(customers):
    runner, database = customers
    spec = _spec([{"kind": "duplicate_values", "id": "dv", "columns": ["nope"]}])
    page = _page(runner, database, spec)
    statuses = page["summary"]["annotations"]
    assert statuses[0]["status"] == "skipped"
    assert "nope" in statuses[0]["reason"]


def test_a_sql_step_is_refused_with_a_reason(customers):
    """A collection has no SQL to run. Skipping quietly would leave the filter
    looking like it worked."""
    runner, database = customers
    spec = _spec(
        [
            {
                "kind": "sql",
                "id": "s1",
                "sql": "SELECT 1",
                "key_column": "id",
                "outputs": [{"name": "x", "dtype": "number"}],
            }
        ]
    )
    page = _page(runner, database, spec)
    statuses = page["summary"]["annotations"]
    assert statuses[0]["status"] == "unsupported"
    assert "MongoDB" in statuses[0]["reason"]


def test_a_materialized_pass_is_reused_rather_than_recomputed(customers):
    runner, database = customers
    scratch, name = _working_copy(runner, database)
    spec = _spec([{"kind": "fuzzy_duplicates", "id": "fz", "on": "name", "threshold": 0.4}])
    _page(runner, scratch, spec, collection=name)
    before = set(runner._client()[scratch].list_collection_names())
    _page(runner, scratch, spec, collection=name)
    after = set(runner._client()[scratch].list_collection_names())
    assert before == after


def test_fuzzy_on_the_live_collection_asks_for_a_working_copy(customers):
    """$lookup cannot cross databases, so joining scratch groups against the
    live collection would yield empty groups — which reads as "no duplicates"."""
    runner, database = customers
    spec = _spec([{"kind": "fuzzy_duplicates", "id": "fz", "on": "name", "threshold": 0.4}])
    page = _page(runner, database, spec)
    status = page["summary"]["annotations"][0]
    assert status["status"] == "unsupported"
    assert "working copy" in status["reason"]
