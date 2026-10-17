"""The boundary that keeps agent-authored pipelines off customer collections.

The MongoDB counterpart to the SQL write guard. Because a pipeline is a
structure rather than a string there is no quoting or comment trick to defend
against — but the write target can name any database in the cluster, and
several stages execute JavaScript inside the customer's server.
"""

from __future__ import annotations

import pytest

from agents.mongo_agent.pipeline_guard import (
    PipelineGuardError,
    assert_scratch_collection,
    validate_pipeline,
    write_targets,
)

SCRATCH_DB = "stych_temp"
PREFIX = "stych_"


def _check(pipeline, running_db="analytics"):
    validate_pipeline(pipeline, scratch_db=SCRATCH_DB, scratch_prefix=PREFIX, running_db=running_db)


def _blocked(pipeline, running_db="analytics") -> bool:
    try:
        _check(pipeline, running_db)
        return False
    except PipelineGuardError:
        return True


# -- writes that would reach the customer's own collections -----------------


@pytest.mark.parametrize(
    "pipeline",
    [
        # Writing back into the source database.
        [{"$out": "orders"}],
        [{"$out": {"db": "analytics", "coll": "orders"}}],
        [{"$merge": {"into": {"db": "analytics", "coll": "orders"}}}],
        [{"$merge": {"into": "orders"}}],
        # Another database entirely.
        [{"$out": {"db": "production", "coll": "stych_x"}}],
        # The scratch database, but a collection that is not ours: a customer
        # could have their own collections there.
        [{"$out": {"db": "stych_temp", "coll": "orders"}}],
        [{"$merge": {"into": {"db": "stych_temp", "coll": "important"}}}],
    ],
)
def test_writes_outside_the_scratch_namespace_are_refused(pipeline):
    assert _blocked(pipeline), f"guard allowed: {pipeline}"


def test_a_scratch_write_is_allowed():
    _check([{"$match": {"$expr": True}}, {"$out": {"db": "stych_temp", "coll": "stych_abc"}}])


def test_read_tools_can_refuse_even_scratch_writes():
    with pytest.raises(PipelineGuardError, match="read tool"):
        validate_pipeline(
            [{"$out": {"db": "stych_temp", "coll": "stych_abc"}}],
            scratch_db=SCRATCH_DB,
            scratch_prefix=PREFIX,
            running_db=SCRATCH_DB,
            allow_writes=False,
        )


def test_agent_reads_can_refuse_cross_collection_stages():
    with pytest.raises(PipelineGuardError, match="another collection"):
        validate_pipeline(
            [{"$lookup": {"from": "customers", "as": "c"}}],
            scratch_db=SCRATCH_DB,
            scratch_prefix=PREFIX,
            running_db="analytics",
            allow_other_collections=False,
        )


def test_a_bare_collection_name_resolves_against_the_running_database():
    """$out with a plain string writes to the database the pipeline runs on."""
    assert _blocked([{"$out": "stych_abc"}], running_db="analytics")
    _check([{"$out": "stych_abc"}], running_db="stych_temp")


# -- server-side code execution ---------------------------------------------


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$match": {"$where": "this.total > 100"}}],
        [{"$project": {"x": {"$function": {"body": "function(){}", "args": [], "lang": "js"}}}}],
        [
            {
                "$group": {
                    "_id": None,
                    "v": {"$accumulator": {"init": "function(){}", "lang": "js"}},
                }
            }
        ],
        # Buried several levels down rather than at stage level.
        [{"$match": {"$expr": {"$and": [{"$where": "true"}]}}}],
    ],
)
def test_javascript_execution_is_refused_anywhere_it_appears(pipeline):
    """Nothing Stych generates needs server-side JS, and an agent must not be
    able to reach it."""
    assert _blocked(pipeline), f"guard allowed code execution: {pipeline}"


# -- structure --------------------------------------------------------------


def test_a_write_must_be_the_final_stage():
    assert _blocked(
        [{"$out": {"db": "stych_temp", "coll": "stych_a"}}, {"$match": {"$expr": True}}]
    )


def test_malformed_pipelines_are_refused():
    assert _blocked("not a pipeline")
    assert _blocked([{"$match": {}, "$out": "stych_a"}])  # two operators in one stage
    assert _blocked(["nope"])


def test_read_only_pipelines_pass():
    _check([{"$match": {"$expr": True}}, {"$group": {"_id": "$region"}}, {"$sort": {"_id": 1}}])


def test_lookup_into_another_collection_is_a_read_and_allowed():
    _check(
        [{"$lookup": {"from": "customers", "localField": "cid", "foreignField": "_id", "as": "c"}}]
    )


# -- helpers ----------------------------------------------------------------


def test_write_targets_reports_what_a_pipeline_writes():
    assert write_targets([{"$out": {"db": "d", "coll": "c"}}]) == [("d", "c")]
    assert write_targets([{"$merge": {"into": "c"}}]) == [(None, "c")]
    assert write_targets([{"$match": {}}]) == []


def test_direct_write_paths_are_guarded_too():
    """updateMany and drop bypass the pipeline entirely."""
    assert assert_scratch_collection("stych_a", scratch_prefix=PREFIX) == "stych_a"
    with pytest.raises(PipelineGuardError):
        assert_scratch_collection("orders", scratch_prefix=PREFIX)
