"""
Parametrized Vega-Lite / Vega spec generators for linkage visuals.

The inspect candidate table is the shared data contract:

    source_row_id, candidate_row_id, score, strategy,
    source_value, candidate_value

Three builders compose Vega-Lite selections / concat (and, for edges,
lower-level Vega) into reusable JSON specs that slot into the inspect
panel and markdown ``vega`` / ``vega-lite`` fences.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .inspect import is_semantic, lexical_family

VL_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
VEGA_SCHEMA = "https://vega.github.io/schema/vega/v5.json"

KIND_SCORE_MATRIX = "score_matrix"
KIND_COORDINATED = "coordinated"
KIND_BIPARTITE = "bipartite"
VIZ_KINDS = (KIND_SCORE_MATRIX, KIND_COORDINATED, KIND_BIPARTITE)

HEATMAP_MAX_SOURCES = 40
HEATMAP_MAX_CANDIDATES = 40
COORDINATED_MAX_SOURCES = 30
COORDINATED_MAX_EDGES = 200
BIPARTITE_MAX_EDGES = 80
LABEL_MAX = 28

LIBRARY_VEGA_LITE = "vega-lite"
LIBRARY_VEGA = "vega"


class VizError(ValueError):
    """Invalid viz kind or arguments."""


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _wanted_strategy(strat_name: str, family: Optional[str]) -> bool:
    if not family:
        return True
    if family == "lexical":
        return lexical_family(strat_name)
    if family == "semantic":
        return is_semantic(strat_name)
    return strat_name == family


def _truncate(text: str, limit: int = LABEL_MAX) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _display_value(raw: Any, row_id: int) -> str:
    if raw is None or raw == "":
        return f"row {row_id}"
    return str(raw)


def _unique_labels(pairs: Sequence[Tuple[int, str]]) -> Dict[int, str]:
    """Stable labels; disambiguate when the same text maps to two ids."""
    counts: Dict[str, int] = {}
    for _, value in pairs:
        counts[value] = counts.get(value, 0) + 1
    labels: Dict[int, str] = {}
    for row_id, value in pairs:
        shown = _truncate(value)
        if counts.get(value, 0) > 1:
            shown = _truncate(f"{row_id}: {value}")
        labels[row_id] = shown or f"row {row_id}"
    return labels


def normalize_candidates(
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Filter, dedupe, and attach display labels.

    Keeps the highest score for each ``(source_row_id, candidate_row_id)``
    pair after applying the strategy family and score cutoff.
    """
    family = (strategy or "").strip().lower() or None
    cutoff = None if threshold is None else float(threshold)

    best: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for raw in candidates or []:
        strat_name = str(raw.get("strategy") or "")
        if not _wanted_strategy(strat_name, family):
            continue
        score = _as_float(raw.get("score"))
        if cutoff is not None and score < cutoff:
            continue
        src = _as_int(raw.get("source_row_id"))
        cand = _as_int(raw.get("candidate_row_id"))
        if src < 0 or cand < 0:
            continue
        key = (src, cand)
        prev = best.get(key)
        if prev is None or score > prev["score"]:
            best[key] = {
                "source_row_id": src,
                "candidate_row_id": cand,
                "score": round(score, 4),
                "strategy": strat_name,
                "source_value": raw.get("source_value"),
                "candidate_value": raw.get("candidate_value"),
            }

    rows = list(best.values())
    source_pairs = []
    seen_src = set()
    cand_pairs = []
    seen_cand = set()
    for row in sorted(rows, key=lambda r: (r["source_row_id"], r["candidate_row_id"])):
        sid = row["source_row_id"]
        cid = row["candidate_row_id"]
        if sid not in seen_src:
            seen_src.add(sid)
            source_pairs.append((sid, _display_value(row.get("source_value"), sid)))
        if cid not in seen_cand:
            seen_cand.add(cid)
            cand_pairs.append((cid, _display_value(row.get("candidate_value"), cid)))

    source_labels = _unique_labels(source_pairs)
    candidate_labels = _unique_labels(cand_pairs)
    for row in rows:
        row["source_label"] = source_labels[row["source_row_id"]]
        row["candidate_label"] = candidate_labels[row["candidate_row_id"]]
        row["edge_label"] = f"{row['source_label']} → {row['candidate_label']}"
    rows.sort(key=lambda r: (-r["score"], r["source_row_id"], r["candidate_row_id"]))
    return rows


def _top_ids(rows: Sequence[Dict[str, Any]], field: str, limit: int) -> List[int]:
    best: Dict[int, float] = {}
    for row in rows:
        rid = int(row[field])
        score = float(row["score"])
        if score > best.get(rid, -1.0):
            best[rid] = score
    ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))
    return [rid for rid, _ in ranked[: max(int(limit), 0)]]


def _cap_matrix(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    source_ids = _top_ids(rows, "source_row_id", HEATMAP_MAX_SOURCES)
    candidate_ids = _top_ids(rows, "candidate_row_id", HEATMAP_MAX_CANDIDATES)
    keep_s = set(source_ids)
    keep_c = set(candidate_ids)
    capped = [
        row for row in rows if row["source_row_id"] in keep_s and row["candidate_row_id"] in keep_c
    ]
    truncated = (
        len(capped) < len(rows)
        or len(keep_s) < len({r["source_row_id"] for r in rows})
        or len(keep_c) < len({r["candidate_row_id"] for r in rows})
    )
    return capped, truncated


def _cap_coordinated(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    source_ids = set(_top_ids(rows, "source_row_id", COORDINATED_MAX_SOURCES))
    filtered = [row for row in rows if row["source_row_id"] in source_ids]
    truncated = len(filtered) < len(rows)
    if len(filtered) > COORDINATED_MAX_EDGES:
        filtered = filtered[:COORDINATED_MAX_EDGES]
        truncated = True
    return filtered, truncated


def _cap_bipartite(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    if len(rows) <= BIPARTITE_MAX_EDGES:
        return list(rows), False
    return list(rows[:BIPARTITE_MAX_EDGES]), True


def _theme(dark: bool) -> Dict[str, Any]:
    if dark:
        return {
            "background": "transparent",
            "foreground": "#e2e8f0",
            "muted": "#94a3b8",
            "grid": "#334155",
            "domain": "#475569",
            "edge": "#93c5fd",
            "node": "#60a5fa",
            "scheme": "blues",
        }
    return {
        "background": "transparent",
        "foreground": "#0f172a",
        "muted": "#64748b",
        "grid": "#e2e8f0",
        "domain": "#cbd5e1",
        "edge": "#2563eb",
        "node": "#1d4ed8",
        "scheme": "blues",
    }


def _vl_config(dark: bool) -> Dict[str, Any]:
    colors = _theme(dark)
    return {
        "background": colors["background"],
        "view": {"stroke": "transparent"},
        "axis": {
            "labelColor": colors["muted"],
            "titleColor": colors["foreground"],
            "gridColor": colors["grid"],
            "domainColor": colors["domain"],
            "tickColor": colors["domain"],
            "labelLimit": 120,
        },
        "legend": {
            "labelColor": colors["muted"],
            "titleColor": colors["foreground"],
        },
        "title": {"color": colors["foreground"], "fontSize": 13, "fontWeight": 600},
        "text": {"color": colors["foreground"]},
    }


def _spec_rows(
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str],
    threshold: Optional[float],
    prepared: bool,
) -> List[Dict[str, Any]]:
    """Normalize unless *prepared* rows already went through ``normalize_candidates``."""
    if prepared:
        return list(candidates or [])
    return normalize_candidates(candidates, strategy=strategy, threshold=threshold)


def score_matrix_spec(
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
    dark: bool = False,
    prepared: bool = False,
) -> Dict[str, Any]:
    """Source × candidate rect heatmap; cell color is similarity."""
    rows, truncated = _cap_matrix(
        _spec_rows(candidates, strategy=strategy, threshold=threshold, prepared=prepared)
    )
    n_src = len({r["source_row_id"] for r in rows})
    n_cand = len({r["candidate_row_id"] for r in rows})
    max_cell = 36
    cell = min(max_cell, 720 // max(n_cand, 1), 640 // max(n_src, 1))
    cell = max(cell, 14)
    width = max(160, min(720, max(n_cand, 1) * cell))
    height = max(140, min(640, max(n_src, 1) * cell))
    title = "Similarity matrix"
    if truncated:
        title += " (top sources × candidates)"
    if not rows:
        title = "Similarity matrix — no candidates above threshold"

    return {
        "$schema": VL_SCHEMA,
        "title": title,
        "width": width,
        "height": height,
        "data": {"values": rows},
        "mark": {"type": "rect", "stroke": _theme(dark)["grid"], "strokeWidth": 0.5},
        "encoding": {
            "x": {
                "field": "candidate_label",
                "type": "nominal",
                "sort": {"field": "candidate_row_id", "op": "min", "order": "ascending"},
                "title": "Candidates",
                "axis": {"labelAngle": -40, "labelFontSize": 10},
            },
            "y": {
                "field": "source_label",
                "type": "nominal",
                "sort": {"field": "source_row_id", "op": "min", "order": "ascending"},
                "title": "Sources",
                "axis": {"labelFontSize": 10},
            },
            "color": {
                "field": "score",
                "type": "quantitative",
                "scale": {"domain": [0, 1], "scheme": _theme(dark)["scheme"]},
                "legend": {"title": "Score"},
            },
            "tooltip": [
                {"field": "source_label", "type": "nominal", "title": "Source"},
                {"field": "candidate_label", "type": "nominal", "title": "Candidate"},
                {"field": "score", "type": "quantitative", "format": ".3f", "title": "Score"},
                {"field": "strategy", "type": "nominal", "title": "Strategy"},
                {"field": "source_row_id", "type": "quantitative", "title": "Source row"},
                {"field": "candidate_row_id", "type": "quantitative", "title": "Candidate row"},
            ],
        },
        "config": _vl_config(dark),
    }


def coordinated_tables_spec(
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
    dark: bool = False,
    prepared: bool = False,
) -> Dict[str, Any]:
    """
    Concatenated source list + candidate-score bars.

    A shared point selection on ``source_row_id`` highlights the matching
    rows in both views (pure Vega-Lite concat + param).
    """
    rows, truncated = _cap_coordinated(
        _spec_rows(candidates, strategy=strategy, threshold=threshold, prepared=prepared)
    )
    sources: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        sid = row["source_row_id"]
        prev = sources.get(sid)
        if prev is None or row["score"] > prev["max_score"]:
            sources[sid] = {
                "source_row_id": sid,
                "source_label": row["source_label"],
                "max_score": row["score"],
            }
    source_rows = sorted(sources.values(), key=lambda r: r["source_row_id"])
    n = max(len(source_rows), len(rows), 1)
    height = max(160, min(480, 18 * n))
    colors = _theme(dark)
    title = "Coordinated sources + scores"
    if truncated:
        title += " (capped)"
    if not rows:
        title = "Coordinated sources + scores — no candidates above threshold"

    picked = {
        "name": "picked_source",
        "select": {
            "type": "point",
            "fields": ["source_row_id"],
            "on": "click",
            "clear": "dblclick",
            "toggle": False,
        },
    }

    source_view: Dict[str, Any] = {
        "name": "sources_view",
        "title": "Sources",
        "width": 200,
        "height": height,
        "data": {"name": "sources"},
        "mark": {"type": "bar", "cursor": "pointer"},
        "encoding": {
            "y": {
                "field": "source_label",
                "type": "nominal",
                "sort": {"field": "source_row_id", "order": "ascending"},
                "title": None,
                "axis": {"labelFontSize": 11, "labelLimit": 140},
            },
            "x": {
                "field": "max_score",
                "type": "quantitative",
                "scale": {"domain": [0, 1]},
                "title": "Best score",
            },
            "color": {"value": colors["node"]},
            "opacity": {
                "condition": {"param": "picked_source", "value": 1},
                "value": 0.28,
            },
            "tooltip": [
                {"field": "source_label", "type": "nominal", "title": "Source"},
                {"field": "max_score", "type": "quantitative", "format": ".3f", "title": "Best"},
                {"field": "source_row_id", "type": "quantitative", "title": "Row"},
            ],
        },
    }

    score_view: Dict[str, Any] = {
        "name": "scores_view",
        "title": "Candidate scores",
        "width": 280,
        "height": height,
        "data": {"name": "edges"},
        "mark": {"type": "bar", "cursor": "pointer"},
        "encoding": {
            "y": {
                "field": "edge_label",
                "type": "nominal",
                "sort": {"field": "score", "order": "descending"},
                "title": None,
                "axis": {"labelFontSize": 10, "labelLimit": 160},
            },
            "x": {
                "field": "score",
                "type": "quantitative",
                "scale": {"domain": [0, 1]},
                "title": "Score",
            },
            "color": {
                "field": "score",
                "type": "quantitative",
                "scale": {"domain": [0, 1], "scheme": colors["scheme"]},
                "legend": None,
            },
            "opacity": {
                "condition": {"param": "picked_source", "value": 1},
                "value": 0.22,
            },
            "tooltip": [
                {"field": "source_label", "type": "nominal", "title": "Source"},
                {"field": "candidate_label", "type": "nominal", "title": "Candidate"},
                {"field": "score", "type": "quantitative", "format": ".3f"},
                {"field": "strategy", "type": "nominal"},
            ],
        },
    }

    return {
        "$schema": VL_SCHEMA,
        "title": title,
        "datasets": {"sources": source_rows, "edges": rows},
        "params": [{**picked, "views": ["sources_view", "scores_view"]}],
        "hconcat": [source_view, score_view],
        "spacing": 18,
        "config": _vl_config(dark),
    }


def bipartite_spec(
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
    dark: bool = False,
    prepared: bool = False,
) -> Dict[str, Any]:
    """
    Vega (not Lite) bipartite edge view.

    Dataset-1 entities on the left, dataset-2 candidates on the right,
    ``rule`` marks weighted by score. Positions are assigned with Vega
    band scales so the chart reflows with height.
    """
    rows, truncated = _cap_bipartite(
        _spec_rows(candidates, strategy=strategy, threshold=threshold, prepared=prepared)
    )
    colors = _theme(dark)
    n_left = len({r["source_row_id"] for r in rows})
    n_right = len({r["candidate_row_id"] for r in rows})
    height = max(220, min(560, 22 * max(n_left, n_right, 1)))
    title = "Bipartite matches"
    if truncated:
        title += " (top edges)"
    if not rows:
        title = "Bipartite matches — no candidates above threshold"

    return {
        "$schema": VEGA_SCHEMA,
        "description": title,
        "autosize": {"type": "fit", "contains": "padding"},
        "height": height,
        "padding": {"left": 8, "right": 8, "top": 36, "bottom": 12},
        "background": colors["background"],
        "title": {
            "text": title,
            "color": colors["foreground"],
            "fontSize": 13,
            "fontWeight": 600,
            "anchor": "start",
        },
        "signals": [
            {
                "name": "width",
                "init": (
                    "isFinite(containerSize()[0]) && containerSize()[0] > 0 "
                    "? containerSize()[0] : 520"
                ),
                "on": [
                    {
                        "events": "window:resize",
                        "update": "max(containerSize()[0], 280)",
                    }
                ],
            },
            {"name": "leftMargin", "value": 140},
            {"name": "leftX", "update": "leftMargin"},
            {"name": "rightX", "update": "width - leftMargin"},
            {
                "name": "picked_source",
                "value": None,
                "on": [
                    {"events": "@leftNodes:click", "update": "datum.source_row_id"},
                    {"events": "@rightNodes:click", "update": "null"},
                    {"events": "dblclick", "update": "null"},
                ],
            },
        ],
        "data": [
            {"name": "edges", "values": rows},
            {
                "name": "left",
                "source": "edges",
                "transform": [
                    {
                        "type": "aggregate",
                        "groupby": ["source_row_id", "source_label"],
                        "ops": ["max"],
                        "fields": ["score"],
                        "as": ["max_score"],
                    },
                    {"type": "collect", "sort": {"field": ["source_row_id"]}},
                    {
                        "type": "window",
                        "ops": ["rank"],
                        "as": ["rank"],
                    },
                ],
            },
            {
                "name": "right",
                "source": "edges",
                "transform": [
                    {
                        "type": "aggregate",
                        "groupby": ["candidate_row_id", "candidate_label"],
                        "ops": ["max"],
                        "fields": ["score"],
                        "as": ["max_score"],
                    },
                    {"type": "collect", "sort": {"field": ["candidate_row_id"]}},
                    {
                        "type": "window",
                        "ops": ["rank"],
                        "as": ["rank"],
                    },
                ],
            },
            {
                "name": "links",
                "source": "edges",
                "transform": [
                    {
                        "type": "lookup",
                        "from": "left",
                        "key": "source_row_id",
                        "fields": ["source_row_id"],
                        "values": ["rank"],
                        "as": ["lrank"],
                    },
                    {
                        "type": "lookup",
                        "from": "right",
                        "key": "candidate_row_id",
                        "fields": ["candidate_row_id"],
                        "values": ["rank"],
                        "as": ["rrank"],
                    },
                ],
            },
        ],
        "scales": [
            {
                "name": "yLeft",
                "type": "point",
                "domain": {"data": "left", "field": "rank"},
                "range": "height",
                "padding": 0.6,
            },
            {
                "name": "yRight",
                "type": "point",
                "domain": {"data": "right", "field": "rank"},
                "range": "height",
                "padding": 0.6,
            },
            {
                "name": "edgeWidth",
                "type": "linear",
                "domain": [0, 1],
                "range": [0.6, 4.5],
            },
            {
                "name": "edgeOpacity",
                "type": "linear",
                "domain": [0, 1],
                "range": [0.18, 0.85],
            },
        ],
        "marks": [
            {
                "type": "rule",
                "name": "linkMarks",
                "from": {"data": "links"},
                "encode": {
                    "update": {
                        "x": {"signal": "leftX"},
                        "x2": {"signal": "rightX"},
                        "y": {"scale": "yLeft", "field": "lrank"},
                        "y2": {"scale": "yRight", "field": "rrank"},
                        "stroke": {"value": colors["edge"]},
                        "strokeWidth": {"scale": "edgeWidth", "field": "score"},
                        "strokeOpacity": {
                            "signal": (
                                "picked_source == null || datum.source_row_id === picked_source "
                                "? scale('edgeOpacity', datum.score) : 0.06"
                            )
                        },
                        "tooltip": {
                            "signal": (
                                "{Source: datum.source_label, Candidate: datum.candidate_label, "
                                "Score: datum.score, Strategy: datum.strategy}"
                            )
                        },
                    }
                },
            },
            {
                "type": "symbol",
                "name": "leftNodes",
                "from": {"data": "left"},
                "encode": {
                    "enter": {"size": {"value": 70}, "shape": {"value": "circle"}},
                    "update": {
                        "x": {"signal": "leftX"},
                        "y": {"scale": "yLeft", "field": "rank"},
                        "fill": {"value": colors["node"]},
                        "cursor": {"value": "pointer"},
                        "opacity": {
                            "signal": (
                                "picked_source == null || datum.source_row_id === picked_source "
                                "? 1 : 0.25"
                            )
                        },
                        "tooltip": {"signal": "{Source: datum.source_label}"},
                    },
                },
            },
            {
                "type": "symbol",
                "name": "rightNodes",
                "from": {"data": "right"},
                "encode": {
                    "enter": {"size": {"value": 70}, "shape": {"value": "circle"}},
                    "update": {
                        "x": {"signal": "rightX"},
                        "y": {"scale": "yRight", "field": "rank"},
                        "fill": {"value": colors["node"]},
                        "cursor": {"value": "pointer"},
                        "opacity": {"value": 0.95},
                        "tooltip": {"signal": "{Candidate: datum.candidate_label}"},
                    },
                },
            },
            {
                "type": "text",
                "from": {"data": "left"},
                "encode": {
                    "update": {
                        "x": {"signal": "leftX - 12"},
                        "y": {"scale": "yLeft", "field": "rank"},
                        "align": {"value": "right"},
                        "baseline": {"value": "middle"},
                        "text": {"field": "source_label"},
                        "fill": {"value": colors["foreground"]},
                        "fontSize": {"value": 11},
                        "limit": {"value": 130},
                    }
                },
            },
            {
                "type": "text",
                "from": {"data": "right"},
                "encode": {
                    "update": {
                        "x": {"signal": "rightX + 12"},
                        "y": {"scale": "yRight", "field": "rank"},
                        "align": {"value": "left"},
                        "baseline": {"value": "middle"},
                        "text": {"field": "candidate_label"},
                        "fill": {"value": colors["foreground"]},
                        "fontSize": {"value": 11},
                        "limit": {"value": 130},
                    }
                },
            },
        ],
    }


def build_viz(
    kind: str,
    candidates: Iterable[Dict[str, Any]],
    *,
    strategy: Optional[str] = None,
    threshold: Optional[float] = None,
    dark: bool = False,
) -> Dict[str, Any]:
    """Dispatch a viz kind to its spec builder. Same candidate table for all three."""
    name = (kind or "").strip().lower()
    builders = {
        KIND_SCORE_MATRIX: (score_matrix_spec, LIBRARY_VEGA_LITE),
        KIND_COORDINATED: (coordinated_tables_spec, LIBRARY_VEGA_LITE),
        KIND_BIPARTITE: (bipartite_spec, LIBRARY_VEGA),
    }
    if name not in builders:
        raise VizError(f"Unknown viz kind {kind!r}. Expected one of: {', '.join(VIZ_KINDS)}")
    builder, library = builders[name]
    rows = normalize_candidates(candidates, strategy=strategy, threshold=threshold)
    spec = builder(rows, strategy=strategy, threshold=threshold, dark=dark, prepared=True)
    n_sources = len({r["source_row_id"] for r in rows})
    n_candidates = len({r["candidate_row_id"] for r in rows})
    return {
        "kind": name,
        "library": library,
        "spec": spec,
        "n_edges": len(rows),
        "n_sources": n_sources,
        "n_candidates": n_candidates,
        "strategy": (strategy or "").strip().lower() or None,
        "threshold": None if threshold is None else float(threshold),
    }
