"""
Aggregation pipelines for the MongoDB diff.

Split from ``api.mongo_diff_service`` so the service reads as "what does the
grid get?" and this reads as "what does the server compute?".

Change detection is a set comparison of each document's top-level fields
(``$setEquals`` over ``$objectToArray``) rather than a per-column comparison.
That covers fields missing from the sampled schema, which a column-wise
comparison would silently skip; the cost is that reordering a nested
subdocument's fields reads as a change. Flagging too much beats missing an edit.

``$lookup`` cannot cross databases, so both collections must live in one — which
is what the pinned-base design already guarantees.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

#: Internal fields these pipelines attach to documents in flight.
BASE = "__stych_b"
ADDED = "__stych_added"
REMOVED = "__stych_removed"
CHANGED = "__stych_changed"
INTERNAL = (BASE, ADDED, REMOVED, CHANGED)


def _own_fields(root: Any = "$$ROOT") -> Dict[str, Any]:
    """A document's top-level fields as a set, excluding identity and internals.

    An absent side yields the empty set rather than an error: ``$objectToArray``
    rejects null, and the side is legitimately absent for an added or removed
    document.
    """
    return {
        "$filter": {
            "input": {"$objectToArray": {"$ifNull": [root, {}]}},
            "as": "f",
            "cond": {
                "$and": [
                    {"$ne": ["$$f.k", "_id"]},
                    {"$eq": [{"$indexOfCP": ["$$f.k", "__stych_"]}, -1]},
                ]
            },
        }
    }


def _changed_expr(base_fields: Any, target_doc: Any) -> Dict[str, Any]:
    """True when the two documents' field sets differ."""
    return {"$eq": [{"$setEquals": [base_fields, _own_fields(target_doc)]}, False]}


def build_summary_pipeline(base_collection: str, target_collection: str) -> List[Dict[str, Any]]:
    """Removed/changed/unchanged counts, from one pass over the base side."""
    return [
        {"$addFields": {"__bf": _own_fields()}},
        {
            "$lookup": {
                "from": target_collection,
                "localField": "_id",
                "foreignField": "_id",
                "as": "__t",
            }
        },
        {
            "$addFields": {
                "__missing": {"$eq": [{"$size": "$__t"}, 0]},
                "__diff": _changed_expr("$__bf", {"$first": "$__t"}),
            }
        },
        {
            "$group": {
                "_id": None,
                "base_total": {"$sum": 1},
                "rows_removed": {"$sum": {"$cond": ["$__missing", 1, 0]}},
                "rows_changed": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$__missing", False]}, "$__diff"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]


def build_added_pipeline(base_collection: str) -> List[Dict[str, Any]]:
    """Documents present in the target but not the base."""
    return [
        {
            "$lookup": {
                "from": base_collection,
                "localField": "_id",
                "foreignField": "_id",
                "as": "__b",
            }
        },
        {"$match": {"$expr": {"$eq": [{"$size": "$__b"}, 0]}}},
        {"$count": "n"},
    ]


def build_aligned_pipeline(
    base_collection: str,
    target_collection: str,
) -> List[Dict[str, Any]]:
    """Every document exactly once — target side, plus base-only "ghost" rows.

    Running from the target and unioning the base-only remainder is what makes
    a removed document visible at all: a plain join from either side alone would
    drop one of the two kinds.
    """
    return [
        {"$addFields": {"__tf": _own_fields()}},
        {
            "$lookup": {
                "from": base_collection,
                "localField": "_id",
                "foreignField": "_id",
                "as": BASE,
            }
        },
        {
            "$addFields": {
                ADDED: {"$eq": [{"$size": f"${BASE}"}, 0]},
                REMOVED: {"$literal": False},
                CHANGED: {
                    "$and": [
                        {"$gt": [{"$size": f"${BASE}"}, 0]},
                        {
                            "$eq": [
                                {"$setEquals": ["$__tf", _own_fields({"$first": f"${BASE}"})]},
                                False,
                            ]
                        },
                    ]
                },
            }
        },
        {"$project": {"__tf": 0}},
        {
            "$unionWith": {
                "coll": base_collection,
                "pipeline": [
                    {
                        "$lookup": {
                            "from": target_collection,
                            "localField": "_id",
                            "foreignField": "_id",
                            "as": "__t",
                        }
                    },
                    {"$match": {"$expr": {"$eq": [{"$size": "$__t"}, 0]}}},
                    {
                        "$addFields": {
                            ADDED: {"$literal": False},
                            REMOVED: {"$literal": True},
                            CHANGED: {"$literal": False},
                        }
                    },
                    {"$project": {"__t": 0}},
                ],
            }
        },
    ]


def build_cell_counts_pipeline(
    base_collection: str,
    target_collection: str,
    columns: Sequence[str],
) -> List[Dict[str, Any]]:
    """Count changed display cells over the complete aligned collection pair."""
    counts: Dict[str, Any] = {}
    for index, name in enumerate(columns):
        target_value = {"$ifNull": [f"${name}", None]}
        base_value = {"$ifNull": [{"$first": f"${BASE}.{name}"}, None]}
        changed = {"$or": [f"${ADDED}", f"${REMOVED}", {"$ne": [target_value, base_value]}]}
        counts[f"c{index}"] = {"$sum": {"$cond": [changed, 1, 0]}}
    return build_aligned_pipeline(base_collection, target_collection) + [
        {"$group": {"_id": None, **counts}}
    ]
