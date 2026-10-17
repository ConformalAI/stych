"""Serve one page of a filtered MongoDB collection, via aggregation.

The document counterpart to ``filters.sql_service``. It emits the same page
shape — rows, ``cell_meta``, ``row_meta``, summary — so the grid does not know
or care which engine produced it.

The whole select stage runs inside the customer's cluster: predicates compile to
``$expr`` (``filters.predicate_mongo``), rules become boolean fields via
``$addFields``, and the summary is one ``$group`` over the full collection. Only
one page of documents ever crosses the wire.

Two things differ from the SQL service, both from the data model:

- **Row identity is ``_id``**, guaranteed by MongoDB. There is no key detection,
  no stamped row id, and no keyless degradation.
- **Columns are sampled, never declared.** The display set comes from flattening
  a sample, so a field appearing only in unsampled documents is absent. Callers
  are told this rather than left to assume a complete schema.
"""

from __future__ import annotations

import logging
import re
from math import ceil
from typing import Any, Dict, List, Optional, Sequence

from filters.predicate_mongo import compile_match
from filters.presentation import PresentationBuilder
from filters.spec import FilterSpec

logger = logging.getLogger(__name__)

#: Documents sampled to infer the display columns.
SCHEMA_SAMPLE = 1_000
#: Field names used for the flags this module adds. Prefixed so they cannot
#: collide with a customer field.
MATCH_FIELD = "__stych_m"
RULE_FIELD = "__stych_r"
GROUP_FIELD = "__stych_g"


def _rule_field(index: int) -> str:
    return f"{RULE_FIELD}{index}"


def build_pipeline(
    spec: FilterSpec,
    columns: Sequence[str],
    *,
    include_flags: bool = True,
    annotate_stages: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Stages that evaluate the spec's select predicate and present rules.

    Flags are computed as fields rather than filtered on, so one pass can serve
    both the summary counts and the page.
    """
    known = set(columns)
    added: Dict[str, Any] = {}
    if spec.select is not None:
        added[MATCH_FIELD] = compile_match(spec.select, known)
    else:
        added[MATCH_FIELD] = {"$literal": True}

    if include_flags:
        for index, rule in enumerate(spec.present.rules):
            # A rule with no predicate of its own fires wherever the filter's
            # select predicate matches.
            added[_rule_field(index)] = (
                compile_match(rule.predicate, known)
                if rule.predicate is not None
                else added[MATCH_FIELD]
            )

    # group_by names an annotation output, which the annotate stages above have
    # already projected as an ordinary field by the time this runs.
    group_by = spec.present.group_by
    if group_by and group_by in known:
        added[GROUP_FIELD] = {"$ifNull": [f"${group_by}", None]}

    return list(annotate_stages or []) + [{"$addFields": added}]


def _contains(column: str, needle: str) -> Dict[str, Any]:
    """Case-insensitive literal substring, matching the other engines' ILIKE.

    The needle is escaped: the grid's search box is a substring box, not a
    regular-expression box, and the SQL engines treat it that way too.
    """
    text = {"$convert": {"input": f"${column}", "to": "string", "onError": "", "onNull": ""}}
    return {"$regexMatch": {"input": text, "regex": re.escape(str(needle)), "options": "i"}}


def build_narrow_expr(
    spec: FilterSpec,
    columns: Sequence[str],
    *,
    matches_only: bool,
    enabled_rule_idx: Optional[Sequence[int]] = None,
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """The grid's narrowing, applied on top of the spec's own predicate.

    Mirrors ``sql_service.build_filter_where``: it narrows the page *and* the
    filtered count, so paging stays consistent with what the grid displays.
    """
    known = set(columns)
    terms: List[Any] = []
    if matches_only:
        terms.append(f"${MATCH_FIELD}")
    if enabled_rule_idx is not None:
        fired = [f"${_rule_field(i)}" for i in enabled_rule_idx if 0 <= i < len(spec.present.rules)]
        terms.append({"$or": fired} if fired else {"$literal": False})
    for name, needle in (filters or {}).items():
        if name in known and needle:
            terms.append(_contains(name, needle))
    if search and columns:
        terms.append({"$or": [_contains(c, search) for c in columns]})
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else {"$and": terms}


def build_count_pipeline(
    spec: FilterSpec,
    columns: Sequence[str],
    *,
    narrow: Optional[Dict[str, Any]],
    annotate_stages: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """How many documents the grid will page through, after narrowing."""
    pipeline = build_pipeline(spec, columns, annotate_stages=annotate_stages)
    if narrow is not None:
        pipeline.append({"$match": {"$expr": narrow}})
    return pipeline + [{"$count": "n"}]


def build_summary_pipeline(
    spec: FilterSpec,
    columns: Sequence[str],
    *,
    annotate_stages: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Counts over the whole collection: total, matched, per-rule, groups."""
    accumulators: Dict[str, Any] = {
        "_id": None,
        "total": {"$sum": 1},
        "matched": {"$sum": {"$cond": [f"${MATCH_FIELD}", 1, 0]}},
    }
    for index, _rule in enumerate(spec.present.rules):
        field = _rule_field(index)
        accumulators[field] = {"$sum": {"$cond": [f"${field}", 1, 0]}}
    if spec.present.group_by:
        accumulators["groups"] = {"$addToSet": f"${GROUP_FIELD}"}
    return build_pipeline(spec, columns, annotate_stages=annotate_stages) + [
        {"$group": accumulators}
    ]


def build_page_pipeline(
    spec: FilterSpec,
    columns: Sequence[str],
    *,
    offset: int,
    limit: int,
    narrow: Optional[Dict[str, Any]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    annotate_stages: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """One page of documents, with the flags the presentation stage needs."""
    pipeline = build_pipeline(spec, columns, annotate_stages=annotate_stages)
    if narrow is not None:
        pipeline.append({"$match": {"$expr": narrow}})

    group_by = spec.present.group_by
    if group_by and group_by in set(columns):
        # Grouping owns row order so members of a group render adjacently; a
        # user sort would interleave them, so it is ignored while active.
        # BSON sorts null low (ungrouped rows first) — pandas/SQL put them last.
        pipeline.append(
            {
                "$addFields": {
                    "__stych_g_null": {
                        "$cond": [
                            {"$eq": [{"$ifNull": [f"${GROUP_FIELD}", None]}, None]},
                            1,
                            0,
                        ]
                    }
                }
            }
        )
        pipeline.append({"$sort": {"__stych_g_null": 1, GROUP_FIELD: 1, "_id": 1}})
    elif sort_by and sort_by in set(columns):
        pipeline.append({"$sort": {sort_by: -1 if sort_dir == "desc" else 1, "_id": 1}})
    else:
        # _id gives every collection a deterministic order, so paging is stable
        # without asking the user for a key.
        pipeline.append({"$sort": {"_id": 1}})

    pipeline.append({"$skip": int(offset)})
    pipeline.append({"$limit": int(limit)})
    return pipeline


def build_mongo_filter_page(
    runner,
    spec: FilterSpec,
    *,
    database: str,
    collection: str,
    session_id: str = "",
    temp_generation: int = 0,
    page: int = 1,
    page_size: int = 100,
    matches_only: Optional[bool] = None,
    enabled_rules: Optional[List[str]] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """One page of a filtered collection, in the shared filter-page shape."""
    from api.preview_service import to_json_safe  # noqa: PLC0415
    from datasources.mongodb import flatten_documents  # noqa: PLC0415
    from filters.annotate_mongo import build_annotation_stages  # noqa: PLC0415

    schema = runner.get_table_columns(database, collection, sample=SCHEMA_SAMPLE)
    base_columns = [c["column_name"] for c in schema]
    display = [
        {"name": c["column_name"], "dtype": c["data_type"], "kind": _kind(c["data_type"])}
        for c in schema
    ]

    # Annotations are derived fields, so they must exist before the select
    # predicate is compiled — a predicate may reference one.
    annotate_stages, annotation_names, annotate_statuses, annotate_degraded = (
        build_annotation_stages(
            runner,
            spec,
            base_columns,
            database=database,
            collection=collection,
            session_id=session_id,
            temp_generation=temp_generation,
        )
    )
    columns = base_columns + annotation_names
    display += [
        {"name": name, "dtype": "object", "kind": "number", "annotation": True}
        for name in annotation_names
    ]

    summary_rows = runner.run_pipeline(
        collection,
        build_summary_pipeline(spec, columns, annotate_stages=annotate_stages),
        database=database,
    )
    stats = summary_rows[0] if summary_rows else {}
    total = int(stats.get("total") or 0)
    matched = int(stats.get("matched") or 0)
    rule_counts = {
        rule.id: int(stats.get(_rule_field(index)) or 0)
        for index, rule in enumerate(spec.present.rules)
    }
    group_values = stats.get("groups") or []
    grouping = bool(spec.present.group_by)

    effective_matches_only = (
        spec.present.matches_only if matches_only is None else bool(matches_only)
    )
    enabled_rule_idx = (
        None
        if enabled_rules is None
        else [i for i, rule in enumerate(spec.present.rules) if rule.id in enabled_rules]
    )
    narrow = build_narrow_expr(
        spec,
        columns,
        matches_only=effective_matches_only,
        enabled_rule_idx=enabled_rule_idx,
        search=search,
        filters=filters,
    )
    if narrow is None:
        filtered = total
    else:
        counted = runner.run_pipeline(
            collection,
            build_count_pipeline(spec, columns, narrow=narrow, annotate_stages=annotate_stages),
            database=database,
        )
        filtered = int(counted[0]["n"]) if counted else 0
    total_pages = max(1, ceil(filtered / page_size)) if filtered else 1
    page = min(max(1, page), total_pages)

    documents = runner.run_pipeline(
        collection,
        build_page_pipeline(
            spec,
            columns,
            offset=(page - 1) * page_size,
            limit=page_size,
            narrow=narrow,
            sort_by=sort_by,
            sort_dir=sort_dir,
            annotate_stages=annotate_stages,
        ),
        database=database,
    )

    flags = [
        {
            "matched": bool(doc.get(MATCH_FIELD)),
            "rules": [
                rule.id
                for index, rule in enumerate(spec.present.rules)
                if doc.get(_rule_field(index))
            ],
            "group": doc.get(GROUP_FIELD),
        }
        for doc in documents
    ]
    # Flags are Stych's, not the customer's, and must not reach the grid.
    stripped = [{k: v for k, v in doc.items() if not k.startswith("__stych_")} for doc in documents]
    frame = flatten_documents(stripped)
    names = [c["name"] for c in display]

    builder = PresentationBuilder(names, spec.present.rules)
    rows_out: List[List[Any]] = []
    for position, flag in enumerate(flags):
        record = frame.iloc[position] if position < len(frame) else None
        values = [
            to_json_safe(record[name]) if record is not None and name in frame.columns else None
            for name in names
        ]
        rows_out.append(values)
        builder.add_row(
            values,
            matched=flag["matched"],
            fired_rule_ids=flag["rules"],
            group_value=flag["group"],
        )

    return {
        "kind": "filter",
        "columns": display,
        "rows": rows_out,
        "cell_meta": builder.cell_meta,
        "row_meta": builder.row_meta,
        "total_rows": total,
        "filtered_rows": filtered,
        "page": page,
        "total_pages": total_pages,
        "summary": {
            "matched_rows": matched,
            "total_rows": total,
            "approx": False,
            "rule_counts": rule_counts,
            "group_count": len([g for g in group_values if g is not None]) if grouping else None,
            # MongoDB guarantees _id, so identity never degrades here.
            "identity_resolved": "key:_id",
            "degraded": annotate_degraded or "sampled_schema",
            "warning": (
                "Columns are inferred from a sample of documents; a field that appears "
                "only in unsampled documents is not shown."
            ),
            "annotations": annotate_statuses,
            "group_by": spec.present.group_by if grouping else None,
        },
    }


def _kind(dtype: str) -> str:
    text = str(dtype).lower()
    if "int" in text or "float" in text:
        return "number"
    if "bool" in text:
        return "bool"
    if "datetime" in text:
        return "date"
    return "string"
