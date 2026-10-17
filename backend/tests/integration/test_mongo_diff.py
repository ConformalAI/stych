"""
Live-MongoDB checks for the document diff. Skipped unless a server is reachable:

    STYCH_TEST_MONGODB_URL=mongodb://localhost:27017 \
        pytest tests/integration/test_mongo_diff.py

These run against a real server on purpose: the diff leans on ``$lookup``,
``$unionWith`` and ``$setEquals`` over ``$objectToArray``, and mongomock
implements none of that faithfully enough to be evidence.
"""

from __future__ import annotations

import os
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")

from agents.mongo_agent import MongoScratchRunner  # noqa: E402
from api.mongo_diff_service import (  # noqa: E402
    MongoDiffError,
    build_mongo_diff_page,
    deep_flatten,
    resolve_diff_target,
)


def _url() -> str:
    url = os.getenv("STYCH_TEST_MONGODB_URL", "")
    if not url:
        pytest.skip("Set STYCH_TEST_MONGODB_URL for MongoDB integration tests")
    return url


@pytest.fixture()
def diff_pair():
    """A scratch database holding a pinned base and an edited working copy."""
    url = _url()
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=4000)
    try:
        client.server_info()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MongoDB not reachable at {url}: {exc}")

    scratch = f"stych_tmp_{uuid.uuid4().hex[:6]}"
    base = [
        {"_id": 1, "sku": "A", "qty": 2, "cust": {"city": "Leeds"}, "tags": ["x", "y"]},
        {"_id": 2, "sku": "B", "qty": None, "cust": {"city": "Hull"}, "tags": ["z"]},
        {"_id": 3, "sku": "C", "qty": 7, "cust": {"city": "York"}, "tags": []},
        {"_id": 4, "sku": "D", "qty": 1, "cust": {"city": "Ely"}, "tags": []},
    ]
    target = [
        {"_id": 1, "sku": "A", "qty": 2, "cust": {"city": "Leeds"}, "tags": ["x", "y"]},  # same
        {"_id": 2, "sku": "B", "qty": 40, "cust": {"city": "Hull"}, "tags": ["z"]},  # imputed
        {"_id": 3, "sku": "C", "qty": 7, "cust": {"city": "Ripon"}, "tags": []},  # nested change
        # _id 4 removed
        {"_id": 5, "sku": "E", "qty": 9, "cust": {"city": "Bath"}, "tags": []},  # added
    ]
    client[scratch]["stych_base"].insert_many(base)
    client[scratch]["stych_work"].insert_many(target)
    runner = MongoScratchRunner(
        {"connection_url": url, "database": scratch, "scratch_database": scratch}
    )
    try:
        yield runner, scratch
    finally:
        client.drop_database(scratch)
        client.close()


def _page(runner, scratch, **kwargs):
    return build_mongo_diff_page(
        runner,
        database=scratch,
        base_collection="stych_base",
        target_collection="stych_work",
        **kwargs,
    )


def test_summary_counts_every_kind_of_change(diff_pair):
    runner, scratch = diff_pair
    page = _page(runner, scratch)
    summary = page["summary"]
    assert summary["rows_added"] == 1  # _id 5
    assert summary["rows_removed"] == 1  # _id 4
    assert summary["rows_changed"] == 2  # _id 2 and 3
    assert summary["rows_unchanged"] == 1  # _id 1
    assert summary["alignment"] == "key:_id"


def test_every_document_appears_exactly_once(diff_pair):
    """A join from either side alone would drop one of added/removed."""
    runner, scratch = diff_pair
    page = _page(runner, scratch)
    assert page["total_rows"] == 5  # 4 base + 1 added
    assert len(page["rows"]) == 5


def test_a_nested_change_is_attributed_to_its_leaf_path(diff_pair):
    """The grid shows one cust.city column, but the diff says which leaf moved."""
    runner, scratch = diff_pair
    page = _page(runner, scratch)
    names = [c["name"] for c in page["columns"]]
    city = names.index("cust.city")
    changed = [
        meta[city] for meta in page["cell_meta"] if meta[city] and meta[city]["k"] != "added"
    ]
    assert any(cell["o"] == "York" for cell in changed)


def test_filling_a_null_reads_as_imputed_not_as_a_value_change(diff_pair):
    runner, scratch = diff_pair
    page = _page(runner, scratch)
    names = [c["name"] for c in page["columns"]]
    qty = names.index("qty")
    kinds = {meta[qty]["k"] for meta in page["cell_meta"] if meta[qty]}
    assert "imputed" in kinds


def test_a_removed_document_carries_no_old_value(diff_pair):
    """The frontend reads a missing "o" on a removed cell as "this whole row is
    not present in the target"."""
    runner, scratch = diff_pair
    page = _page(runner, scratch)
    removed = [meta for meta in page["cell_meta"] if meta and meta[0] and meta[0]["k"] == "removed"]
    assert len(removed) == 1
    assert all("o" not in cell for cell in removed[0])


def test_changes_only_narrows_both_the_page_and_the_count(diff_pair):
    runner, scratch = diff_pair
    page = _page(runner, scratch, changes_only=True)
    assert page["filtered_rows"] == 4  # everything but the unchanged _id 1
    assert len(page["rows"]) == 4


@pytest.mark.parametrize(
    "kind,expected",
    [("added", 1), ("removed", 1), ("changed", 2)],
)
def test_change_kind_selects_one_kind(diff_pair, kind, expected):
    runner, scratch = diff_pair
    page = _page(runner, scratch, change_kind=kind)
    assert page["filtered_rows"] == expected


def test_search_narrows_across_display_columns(diff_pair):
    runner, scratch = diff_pair
    page = _page(runner, scratch, search="Ripon")
    assert page["filtered_rows"] == 1


def test_a_column_added_only_in_the_target_is_reported(diff_pair):
    runner, scratch = diff_pair
    runner._client()[scratch]["stych_work"].update_one({"_id": 5}, {"$set": {"channel": "web"}})
    page = _page(runner, scratch)
    assert "channel" in page["summary"]["columns_added"]
    assert page["summary"]["columns_removed"] == []


def test_columns_match_the_shared_preview_diff_contract(diff_pair):
    from api.schemas import PreviewDiffPage

    runner, scratch = diff_pair
    page = _page(runner, scratch)
    assert page["columns"]
    assert all(
        {"name", "dtype", "kind", "status", "note", "changed_cells"} <= set(column)
        for column in page["columns"]
    )
    changed = {column["name"]: column["changed_cells"] for column in page["columns"]}
    assert sum(changed.values()) == page["summary"]["cells_changed"]
    assert changed["sku"] == 2  # one added row and one removed row
    PreviewDiffPage.model_validate(page)


def test_a_change_to_an_unsampled_field_still_counts_as_a_change(diff_pair):
    """Whole-document comparison is what makes this true; a per-column diff over
    the sampled schema would silently miss it."""
    runner, scratch = diff_pair
    runner._client()[scratch]["stych_work"].update_one({"_id": 1}, {"$set": {"rare": 1}})
    page = _page(runner, scratch)
    assert page["summary"]["rows_changed"] == 3
    assert page["summary"]["rows_unchanged"] == 0


def test_deep_flatten_indexes_arrays_positionally():
    flat = deep_flatten({"tags": ["x", "y"], "cust": {"city": "Leeds"}})
    assert flat == {"tags.0": "x", "tags.1": "y", "cust.city": "Leeds"}


def test_diffing_against_the_live_collection_asks_for_a_pin_instead(diff_pair):
    """The live collection can move under a running diff, so "base" resolves to
    the pinned snapshot — and says so when none was taken."""
    _runner, scratch = diff_pair
    with pytest.raises(MongoDiffError, match="pinned snapshot"):
        resolve_diff_target({"source": "mongodb"}, "base", scratch)
